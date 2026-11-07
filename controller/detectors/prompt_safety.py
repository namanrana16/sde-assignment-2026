"""A2: Day-0 sibling prompt safety check, and the guarded switch it can trigger.

After the live prompt is validated (halluc Day 0), each other catalogue prompt (at most ``SIBLING_MAX``, catalogue
order, only while the incident credit reserve of every workflow stays intact) is validated at modest n. A sibling is
"clearly safer" when the upper bound of its failure rate lies below the lower bound of the live prompt's. Then a
"safety improvement" prompt change is proposed through approval with the sibling's validation run as evidence.
After apply the new prompt must be flag-non-inferior and engagement-non-inferior (guardrail) on production data,
else a rollback to the old prompt (which has validation evidence) is proposed. Summaries are never read.
"""
from __future__ import annotations

import logging
from typing import Mapping

from .. import settings
from ..desired import DEFAULT_REGISTRY, ROUTE_APPROVAL, DesiredChange, PlanContext, Priority
from ..incidents import can_spend, initial_credits, open_incidents, run_validation, validation_cost, validations_of
from ..stats import non_inferior, rate_greater_test, wilson_lower, wilson_upper
from . import halluc_data as hd
from .common import latest_config, load_state, save_state
from .halluc import KIND as HALLUC_KIND
from .halluc import _ceiling, _max_engagement_drop, engagement_ok

log = logging.getLogger(__name__)
DETECTOR = "prompt_safety"
JOB = "sibling_check"
RECOMMENDED, APPLIED, VERIFIED, ROLLBACK, ROLLED_BACK, DROPPED = (
    "recommended", "applied", "verified", "rollback", "rolled_back", "dropped")


def pooled(runs: list[Mapping]) -> tuple[int, int]:
    """(failures, n) over validation runs."""
    n = sum(r["n"] for r in runs)
    return sum(round((1 - r["pass_rate"]) * r["n"]) for r in runs), n


def compare(live_runs: list[Mapping], sib_runs: list[Mapping], ceiling: float | None = None) -> dict:
    """Failure-rate bounds of the live prompt and a sibling; ``clear`` iff sibling upper < live lower.

    ``material`` (when a ``ceiling`` is given): the live prompt's failure lower bound is at least
    ``SIBLING_MIN_LIVE_FRACTION`` of the workflow's own tolerance (1 - min validation pass rate). A bound-separated
    but immaterial gap is validation noise between two acceptable prompts, and a switch can cost engagement.
    """
    lf, ln = pooled(live_runs)
    sf, sn = pooled(sib_runs)
    z = settings.SIBLING_BOUND_Z
    live_lb, sib_ub = wilson_lower(lf, ln, z), wilson_upper(sf, sn, z)
    return {"live_fail": lf, "live_n": ln, "live_rate": lf / ln if ln else None, "live_lb": round(live_lb, 5),
            "sib_fail": sf, "sib_n": sn, "sib_rate": sf / sn if sn else None, "sib_ub": round(sib_ub, 5),
            "clear": bool(ln and sn and sib_ub < live_lb),
            "material": ceiling is None or live_lb >= settings.SIBLING_MIN_LIVE_FRACTION * ceiling,
            "promising": bool(ln and sn and sf / sn < live_lb)}


def _afford(ctx: PlanContext, cost: float) -> bool:
    return can_spend(ctx.client.credits_remaining(), cost, len(ctx.workflows),
                     credits_initial=initial_credits(ctx.store))


def _best_run(runs: list[Mapping], min_pass: float) -> Mapping | None:
    ok = [r for r in runs if r["pass_rate"] >= min_pass]
    return max(ok, key=lambda r: (r["n"], r["run_id"])) if ok else None


def _validate_sibling(ctx: PlanContext, wid: str, prompt: str, out: dict) -> list[dict] | None:
    runs = validations_of(ctx.store, wid, prompt)
    if runs:
        return runs
    cost = validation_cost(settings.SIBLING_N)
    if not _afford(ctx, cost):
        out["checked"][prompt] = {"skipped": "credits: incident reserve would be breached"}
        return None
    if run_validation(ctx.client, ctx.store, wid, prompt, ctx.now, settings.SIBLING_N) is None:
        return None
    out["credits"] += cost
    return validations_of(ctx.store, wid, prompt)


def _check_workflow(ctx: PlanContext, wid: str, live: str) -> dict:
    wf = ctx.workflows[wid]
    min_pass = float(wf["governance"]["min_validation_pass_rate"])
    live_runs = validations_of(ctx.store, wid, live)
    out: dict = {"phase": None, "live": live, "checked": {}, "credits": 0.0}
    if not live_runs:
        return out
    clear: list[tuple[float, str, dict]] = []
    ceiling, live_topped = _ceiling(wf), False
    for prompt in [p for p in wf["options"]["prompt_version"] if p != live][:settings.SIBLING_MAX]:
        runs = _validate_sibling(ctx, wid, prompt, out)
        if runs is None:
            continue
        cmp_ = compare(live_runs, runs, ceiling)
        topup = validation_cost(settings.SIBLING_TOPUP_N)
        if not cmp_["clear"] and cmp_["promising"] and len(runs) == 1 and _afford(ctx, topup):
            run_validation(ctx.client, ctx.store, wid, prompt, ctx.now, settings.SIBLING_TOPUP_N)
            out["credits"] += topup
            runs = validations_of(ctx.store, wid, prompt)
            cmp_ = compare(live_runs, runs, ceiling)
        if cmp_["clear"] and not cmp_["material"] and not live_topped and _afford(ctx, topup):
            run_validation(ctx.client, ctx.store, wid, live, ctx.now, settings.SIBLING_TOPUP_N)  # one second look at the live prompt
            live_topped = True
            out["credits"] += topup
            live_runs = validations_of(ctx.store, wid, live)
            cmp_ = compare(live_runs, runs, ceiling)
        out["checked"][prompt] = cmp_
        best = _best_run(runs, min_pass)
        if cmp_["clear"] and cmp_["material"] and best is not None:
            info = {**cmp_, "validation_run_id": best["run_id"], "pass_rate": best["pass_rate"]}
            clear.append((cmp_["sib_ub"], prompt, info))
    if clear:
        _, prompt, info = sorted(clear, key=lambda t: (t[0], t[1]))[0]
        live_best = _best_run(live_runs, min_pass)
        out.update(phase=RECOMMENDED, candidate=prompt, evidence=info,
                   old_run_id=live_best["run_id"] if live_best else None)
    return out


def _ready(ctx: PlanContext) -> bool:
    """All workflows' live prompts have had their Day-0 validation attempt (so credits are known)."""
    return all(ctx.store.get_job("halluc_day0", w) is not None for w in ctx.workflows)


def _verify(ctx: PlanContext, wid: str, st: dict, live: str) -> None:
    wf = ctx.workflows[wid]
    hst = hd.update(ctx.store, wid)
    new = hd.counts_since(hst, live, st["applied_minute"])
    if new[0] < settings.HALLUC_VERIFY_MIN_CONNECTS:
        return
    old = hd.total_counts(hst, st["old"])
    margin = _ceiling(wf) * settings.HALLUC_FLAG_MARGIN_FRACTION
    flags_ok = non_inferior(new[1], new[0], old[1], old[0], margin)
    p_worse = rate_greater_test(new[1], new[0], old[1], old[0])
    eng_ok, eng = engagement_ok(new, old, _max_engagement_drop(wf))
    info = {"prompt": live, "old": st["old"], "n": new[0], "flag_rate": round(new[1] / new[0], 5),
            "old_flag_rate": round(old[1] / old[0], 5) if old[0] else None, "flags_ok": flags_ok,
            "engagement_ok": eng_ok, "p_worse": p_worse, **eng}
    waiting = (eng_ok and not flags_ok and p_worse >= settings.HALLUC_VERIFY_P
               and new[0] < settings.HALLUC_VERIFY_MAX_CONNECTS)
    if waiting:
        return
    if flags_ok and eng_ok:
        st["phase"] = VERIFIED
        ctx.store.add_decision(ctx.now, "prompt_safety_verified",
                               f"{live}: flags non-inferior to {st['old']}, engagement ok", wid, info)
    else:
        st["phase"] = ROLLBACK
        ctx.store.add_decision(ctx.now, "prompt_safety_rollback",
                               f"{live} did not verify against {st['old']}: rolling back", wid, info)


def _advance(ctx: PlanContext, wid: str, st: dict, live: str) -> bool:
    """Move the post-recommendation lifecycle; True when the state changed."""
    before = dict(st)
    phase = st.get("phase")
    if phase == RECOMMENDED:
        if live == st["candidate"]:
            st.update(phase=APPLIED, old=st["live"], applied_minute=ctx.now)
            ctx.store.add_decision(ctx.now, "prompt_safety_applied",
                                   f"safety switch {st['live']} -> {live} is live; verifying", wid)
        elif live != st["live"]:
            st["phase"] = DROPPED  # someone else changed the prompt: the recommendation is void
    elif phase == APPLIED:
        if live != st["candidate"]:
            st["phase"] = DROPPED
        else:
            _verify(ctx, wid, st, live)
    elif phase == ROLLBACK and live == st["old"]:
        st["phase"] = ROLLED_BACK
        hst = hd.update(ctx.store, wid)
        hd.mark_bad(hst, st["candidate"], ctx.now, "safety_switch_failed_verification")
        hd.save(ctx.store, wid, hst)
    return st != before


def sibling_detector(ctx: PlanContext) -> None:
    if not settings.ENABLE_HALLUC or not _ready(ctx):
        return
    for wid in sorted(ctx.workflows):
        cfg = latest_config(ctx.store, wid)
        if cfg is None:
            continue
        live = cfg[1]["prompt_version"]
        st = load_state(ctx.store, DETECTOR, wid)
        if ctx.store.get_job(JOB, wid) is None:
            if open_incidents(ctx.store, wid, HALLUC_KIND):
                continue  # the incident machinery owns prompt changes right now
            ctx.store.set_job(JOB, wid, ctx.now)  # one paid attempt, never a retry loop
            st = _check_workflow(ctx, wid, live)
            verdict = f"{st['candidate']} is clearly safer" if st["phase"] else "no sibling clearly safer"
            ctx.store.add_decision(ctx.now, "sibling_check", f"sibling prompt check for live {live}: {verdict}"
                                   f" ({st['credits']:.0f} credits)", wid, st)
            save_state(ctx.store, DETECTOR, wid, st)
        elif _advance(ctx, wid, st, live):
            save_state(ctx.store, DETECTOR, wid, st)


def sibling_producer(ctx: PlanContext, wid: str) -> list[DesiredChange]:
    st = load_state(ctx.store, DETECTOR, wid)
    cfg = latest_config(ctx.store, wid)
    if cfg is None or not st or open_incidents(ctx.store, wid, HALLUC_KIND):
        return []
    live, phase = cfg[1]["prompt_version"], st.get("phase")
    ceiling = max(_ceiling(ctx.workflows[wid]), 1e-9)
    if phase == RECOMMENDED and live == st["live"]:
        e = st["evidence"]
        summary = (f"Safety improvement: switch prompt {live} -> {st['candidate']}: validation failure rate "
                   f"{e['sib_rate']:.1%} (upper bound {e['sib_ub']:.1%}, n={e['sib_n']}) vs {e['live_rate']:.1%} "
                   f"(lower bound {e['live_lb']:.1%}, n={e['live_n']}); validation {e['validation_run_id']} pass "
                   f"{e['pass_rate']:.3f}. Flags and engagement are verified after apply; an engagement drop "
                   "triggers a rollback")
        evidence = {"validation_run_id": e["validation_run_id"], "rationale": summary,
                    "impact_override": min(1.0, max(e["live_rate"] - e["sib_rate"], 0.0) / ceiling)}
        return [DesiredChange(wid, "prompt_safety", Priority.ROUTINE, {"prompt_version": st["candidate"]},
                              ROUTE_APPROVAL, evidence, summary)]
    if phase == ROLLBACK and live == st["candidate"] and st.get("old_run_id"):
        summary = (f"Roll back prompt {live} -> {st['old']}: the safety switch did not verify "
                   f"(engagement/flags guardrail); validation {st['old_run_id']}")
        return [DesiredChange(wid, "prompt_rollback", Priority.SEVERE, {"prompt_version": st["old"]},
                              ROUTE_APPROVAL, {"validation_run_id": st["old_run_id"], "rationale": summary,
                                               "impact_override": 0.5}, summary)]
    return []


def register(registry=DEFAULT_REGISTRY) -> None:
    registry.register_detector(sibling_detector)
    registry.register_producer(sibling_producer)
