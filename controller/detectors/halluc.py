"""Hallucination-incident detector and producer (CONTEXT C8), behind ``settings.ENABLE_HALLUC``.

Per workflow, each tick: (1) Day-0 validation of the live prompt, (2) one-sided two-proportion
test of the live prompt's flag rate against the last known-good prompt's, (3) fix
selection (revert to our own known-good prompt using its validation run; only if none, ask the
LLM for candidates and rank validated ones by the lowest failure upper bound), (4) verification
after the fix is applied (flag rate back to baseline, engagement inside the guardrail), (5) close.
The producer turns an open incident into a prompt-change DesiredChange (always via approval).
"""
from __future__ import annotations

import logging
from typing import Mapping

from .. import settings
from ..credits import call_with_credit_ledger
from ..client import OutcomeUnknown, SimUnavailable
from ..desired import DEFAULT_REGISTRY, ROUTE_APPROVAL, DesiredChange, PlanContext, Priority
from ..incidents import (can_spend, close_incident, daily_cap_ok, initial_credits, open_incident, open_incidents, pick_validation_n,
                         recover_validations, run_validation, update_incident, validation_cost, validation_for,
                         validations_of)
from ..stats import non_inferior, p_text, rate_greater_test, wilson_lower, wilson_upper
from . import halluc_data as hd
from .common import latest_config, load_state, note_once, once_alert, save_state

log = logging.getLogger(__name__)
KIND = "halluc"
_RECOVERED: set[str] = set()


def clock(minute: int) -> str:
    return f"Day {minute // 1440} {minute % 1440 // 60:02d}:{minute % 60:02d}"


def _rate(c: list) -> float:
    return c[1] / c[0] if c[0] else 0.0


def _wf_min_pass(wf: Mapping) -> float:
    return float(wf["governance"]["min_validation_pass_rate"])


def _max_engagement_drop(wf: Mapping) -> float:
    guard = (wf.get("objective") or {}).get("guardrail") or {}
    return float(guard.get("engagement_rate_max_drop", settings.ENGAGEMENT_VERIFY_MAX_DROP))


# ------------------------------------------------------------------ the statistical test
def _ceiling(wf: Mapping) -> float:
    """Highest flag rate the workflow's own governance accepts: a fix needs validation pass >= min_pass."""
    return 1.0 - _wf_min_pass(wf)


def _effective_ceiling(wf: Mapping, base: list | None) -> float:
    """Absolute flag-rate ceiling: the governance bar (1 - min_pass), but never below what the last known-good
    prompt's own measured flag rate (detector false positives included) plus its sampling margin says is healthy.
    With no trusted baseline (cold start) it is the governance bar alone."""
    floor = _ceiling(wf)
    if base is None or base[0] < settings.HALLUC_BASELINE_MIN_CONNECTS:
        return floor
    return max(floor, wilson_upper(base[1], base[0], settings.Z_ONE_SIDED_P001))


def segment_test(seg: list, base: list, alpha: float = settings.HALLUC_P) -> dict:
    """Is the prompt's flag rate above the baseline's? Exact Fisher for small counts, pooled z otherwise."""
    p = rate_greater_test(seg[1], seg[0], base[1], base[0])
    seg_adh = seg[2] / seg[0] if seg[0] else 0.0
    base_adh = base[2] / base[0] if base[0] else 0.0
    triggered = seg[0] >= settings.HALLUC_MIN_CONNECTS and p < alpha
    return {"n": seg[0], "flags": seg[1], "rate": _rate(seg), "rate_lb": round(wilson_lower(seg[1], seg[0], 3.09), 5),
            "base_n": base[0], "base_rate": _rate(base), "p": p, "adherence_drop": round(base_adh - seg_adh, 4),
            "triggered": triggered}


def ceiling_test(seg: list, ceiling: float) -> dict:
    """Baseline-free: the flag-rate lower bound (one-sided p<0.001) is above the governance ceiling."""
    lb = wilson_lower(seg[1], seg[0], settings.Z_ONE_SIDED_P001)
    return {"n": seg[0], "flags": seg[1], "rate": _rate(seg), "rate_lb": round(lb, 5), "base_n": 0,
            "base_rate": ceiling, "p": 0.0, "adherence_drop": 0.0,
            "triggered": seg[0] >= settings.HALLUC_MIN_CONNECTS and lb > ceiling}


def engagement_ok(seg: list, base: list, max_drop: float) -> tuple[bool, dict]:
    """Engagement not significantly below (baseline - guardrail margin)."""
    if not seg[0] or not base[0]:
        return True, {}
    e_new, e_base = seg[3] / seg[0], base[3] / base[0]
    se = (e_new * (1 - e_new) / seg[0] + e_base * (1 - e_base) / base[0]) ** 0.5
    ok = (e_new - e_base) + 1.645 * se >= -max_drop
    return ok, {"engagement": round(e_new, 4), "base_engagement": round(e_base, 4), "max_drop": max_drop}


def rank_by_failure_bound(runs: list[dict], min_pass: float) -> list[dict]:
    """Validated candidates by the lowest failure upper bound (not merely >= the approver's bar)."""
    ok = [r for r in runs if r["pass_rate"] >= min_pass]
    for r in ok:
        r["fail_ub"] = round(wilson_upper(round((1 - r["pass_rate"]) * r["n"]), r["n"]), 5)
    return sorted(ok, key=lambda r: (r["fail_ub"], r["prompt"]))


def _failures(run: Mapping) -> int:
    return round((1 - run["pass_rate"]) * run["n"])


# ------------------------------------------------------------------ Day-0 validation
def _day0_n(ctx: PlanContext, wid: str, pending: int) -> int | None:
    """Day-0 validation draws from the INCIDENT credit rule: only the real credit balance (minus what the
    other pending Day-0 runs need at the minimum size) and the per-workflow daily cap limit it."""
    floor = validation_cost(settings.HALLUC_VALIDATION_N_STEPS[-1]) * max(pending - 1, 0)
    n = pick_validation_n(ctx.client.credits_remaining(), floor)
    while n is not None and not daily_cap_ok(ctx.store, wid, ctx.now, validation_cost(n)):
        smaller = [k for k in settings.HALLUC_VALIDATION_N_STEPS if k < n]
        n = smaller[0] if smaller else None
    return n


def day0_validation(ctx: PlanContext) -> None:
    store = ctx.store
    if str(store.path) not in _RECOVERED:
        recover_validations(store)
        _RECOVERED.add(str(store.path))
    todo = [w for w in sorted(ctx.workflows) if store.get_job("halluc_day0", w) is None]
    for wid in todo:
        cfg = latest_config(store, wid)
        if cfg is None:
            continue
        live = cfg[1]["prompt_version"]
        if validation_for(store, wid, live) is not None:
            store.set_job("halluc_day0", wid, ctx.now)
            continue
        n = _day0_n(ctx, wid, len(todo))
        if n is None:
            once_alert(store, ctx.now, "warn", "day0_validation_skipped", "credits cannot cover a minimum validation", wid)
            continue
        store.set_job("halluc_day0", wid, ctx.now)  # one paid attempt, never a retry loop
        run_validation(ctx.client, store, wid, live, ctx.now, n)
    for wid in sorted(ctx.workflows):
        _confirm_day0(ctx, wid)


def _day0_verdict(runs: list[dict], min_pass: float) -> dict | None:
    """None when some validation passes the bar; else the pooled evidence and whether it is conclusive."""
    if not runs or max(r["pass_rate"] for r in runs) >= min_pass:
        return None
    n, fails = sum(r["n"] for r in runs), sum(_failures(r) for r in runs)
    upper_pass = 1.0 - wilson_lower(fails, n)
    return {"n": n, "flags": fails, "rate": fails / n, "rate_lb": round(wilson_lower(fails, n), 5),
            "definite": upper_pass < min_pass, "confirmed": len(runs) >= 2, "runs": len(runs)}


def _confirm_day0(ctx: PlanContext, wid: str) -> None:
    """A single borderline-failing validation is re-run once (incident credit rule) before it can open anything."""
    cfg = latest_config(ctx.store, wid)
    if cfg is None or open_incidents(ctx.store, wid, KIND):
        return
    live = cfg[1]["prompt_version"]
    verdict = _day0_verdict(validations_of(ctx.store, wid, live), _wf_min_pass(ctx.workflows[wid]))
    key = f"{wid}:{live}"
    if verdict is None or verdict["definite"] or verdict["confirmed"] or ctx.store.get_job("halluc_confirm", key) is not None:
        return
    n = _day0_n(ctx, wid, 1)
    if n is None:
        return
    ctx.store.set_job("halluc_confirm", key, ctx.now)
    run_validation(ctx.client, ctx.store, wid, live, ctx.now, n)


# ------------------------------------------------------------------ fix selection
def _spend_ok(ctx, wid: str, body: dict, cost: float) -> bool:
    return (can_spend(ctx.client.credits_remaining(), cost, len(ctx.workflows), incident_spent=body["credits_spent"],
                     credits_initial=initial_credits(ctx.store))
            and daily_cap_ok(ctx.store, wid, ctx.now, cost))


def _borderline(run: Mapping, min_pass: float) -> bool:
    """Stored validation below the bar by no more than ~2 standard errors of its own pass rate."""
    if run["pass_rate"] >= min_pass:
        return False
    se = (max(run["pass_rate"] * (1 - run["pass_rate"]), 1e-4) / max(run["n"], 1)) ** 0.5
    return min_pass - run["pass_rate"] <= 2 * se and run["n"] < settings.HALLUC_VALIDATION_N


def _validated(ctx, wid: str, prompt: str, body: dict, min_pass: float) -> dict | None:
    """A passing validation of ``prompt`` (existing, or one paid run inside the incident budget)."""
    run = validation_for(ctx.store, wid, prompt)
    if run is not None and 0.0 < min_pass and _borderline(run, min_pass) and prompt not in body.setdefault("revalidated", []):
        body["revalidated"].append(prompt)  # once: a known-good prompt just below the bar gets a larger sample
        n = max(settings.HALLUC_VALIDATION_N, run["n"])
        cost = validation_cost(n)
        if _spend_ok(ctx, wid, body, cost):
            raw = run_validation(ctx.client, ctx.store, wid, prompt, ctx.now, n)
            body["credits_spent"] += cost
            if raw is not None:
                run = {"run_id": raw["validation_run_id"], "n": raw["n"], "pass_rate": raw["pass_rate"]}
    if run is None:
        cost = validation_cost(settings.HALLUC_VALIDATION_N)
        if not _spend_ok(ctx, wid, body, cost):
            return None
        raw = run_validation(ctx.client, ctx.store, wid, prompt, ctx.now)
        body["credits_spent"] += cost
        if raw is None:
            return None
        run = {"run_id": raw["validation_run_id"], "n": raw["n"], "pass_rate": raw["pass_rate"]}
    return run if run["pass_rate"] >= min_pass else None


def _candidate(prompt: str, run: dict, source: str) -> dict:
    return {"prompt": prompt, "validation_run_id": run["run_id"], "pass_rate": run["pass_rate"], "n": run["n"],
            "source": source}


def _revert_is_clearly_better(counts: list, live_rate_lb: float | None) -> bool:
    """A revert target's historical flag-rate upper bound must lie below the live prompt's lower bound."""
    if live_rate_lb is None:
        return True
    return wilson_upper(counts[1], counts[0]) < live_rate_lb


def _scan_known_good(ctx, wid: str, body: dict, st: Mapping, live: str, tried: set, min_pass: float) -> None:
    """Validate (at most one) known-good prompt that is in the workflow's catalog and not yet tried."""
    options = (ctx.workflows[wid].get("options") or {}).get("prompt_version")
    lb = body["stats"].get("rate_lb")
    for prompt, counts in hd.known_good_candidates(st, live, ctx.now):
        if prompt in tried or (options is not None and prompt not in options):
            continue
        if any(c["prompt"] == prompt for c in body["candidates"]):
            continue
        if not _revert_is_clearly_better(counts, lb):
            if prompt not in body.setdefault("rejected_reverts", []):
                body["rejected_reverts"].append(prompt)
            continue
        run = _validated(ctx, wid, prompt, body, min_pass)
        if run is not None:
            cand = _candidate(prompt, run, "known_good")
            cand.update({"flag_rate": round(_rate(counts), 5), "flag_n": counts[0]})
            body["candidates"].append(cand)
            return


def _bar_unreachable(ctx: PlanContext, wid: str, body: Mapping, min_pass: float) -> bool:
    """A full catalog pass: every catalog prompt already has a validation run below the bar and none is worth a
    larger re-sample (so no purchase can still find a passing prompt). A prompt without a run (a new one) reopens it."""
    options = (ctx.workflows[wid].get("options") or {}).get("prompt_version")
    if not options or min_pass <= 0.0:
        return False
    revalidated = body.get("revalidated", [])
    for prompt in options:
        run = validation_for(ctx.store, wid, prompt)
        if run is None or run["pass_rate"] >= min_pass:
            return False
        if _borderline(run, min_pass) and prompt not in revalidated:
            return False
    return True


def _tick_minutes(ctx: PlanContext) -> int:
    """The run's tick: smallest step the loop has taken (idle steps are longer); the default before any tick."""
    row = ctx.store.conn.execute("SELECT MIN(step_minutes) FROM metrics WHERE step_minutes > 0").fetchone()
    return int(row[0]) if row and row[0] else settings.TICK_MINUTES


def prepare_fix(ctx: PlanContext, wid: str, body: dict, st: Mapping, live: str, persist=None) -> None:
    """Fill ``body['candidates']`` / ``body['target']``: revert first; LLM only if none exists."""
    min_pass = _wf_min_pass(ctx.workflows[wid])
    tried = set(body["tried"])
    if not any(c["prompt"] not in tried and c["prompt"] != live for c in body["candidates"])             and _bar_unreachable(ctx, wid, body, min_pass):
        body["target"] = None
        if note_once(ctx.store, f"bar_unreachable:{wid}", ctx.now):
            ctx.store.add_decision(ctx.now, "prompt_bar_unreachable",
                                   f"every catalog prompt validated below the {min_pass:.3f} bar; no more validations"
                                   " or suggestions are bought for this workflow", wid, {"min_pass": min_pass})
        once_alert(ctx.store, ctx.now, "error", "prompt_bar_unreachable",
                   f"no catalog prompt reaches the {min_pass:.3f} validation bar; incident stays open", wid)
        return
    has_untried = any(c["prompt"] not in tried and c["prompt"] != live for c in body["candidates"])
    if not has_untried and (not body["reverts_checked"] or body.get("scanned_tried", 0) < len(tried)):
        body["reverts_checked"], body["scanned_tried"] = True, len(tried)
        _scan_known_good(ctx, wid, body, st, live, tried, min_pass)
    if body.get("pending_suggestions"):  # a crash left paid suggestions unvalidated: finish them, never re-pay
        _validate_suggestions(ctx, wid, body, live, min_pass)
    if not any(c["prompt"] not in tried and c["prompt"] != live for c in body["candidates"]) \
            and not body["suggested"] and _has_untried_option(ctx.workflows[wid], live, tried):
        _suggest(ctx, wid, body, live, min_pass, persist)
    options = [c for c in body["candidates"] if c["prompt"] not in tried and c["prompt"] != live]
    body["target"] = options[0]["prompt"] if options else None
    if body["target"] is None:
        once_alert(ctx.store, ctx.now, "error", "no_prompt_fix", "no validated fix available; incident stays open", wid)


def _has_untried_option(wf: Mapping, live: str, tried) -> bool:
    """suggest-fixes only returns the workflow's own other prompt options, so with none left it buys nothing."""
    options = (wf.get("options") or {}).get("prompt_version")
    return options is None or any(p != live and p not in tried for p in options)


def _suggest(ctx: PlanContext, wid: str, body: dict, live: str, min_pass: float, persist=None) -> None:
    if not _spend_ok(ctx, wid, body, settings.SUGGEST_FIXES_COST):
        once_alert(ctx.store, ctx.now, "error", "suggest_fixes_unaffordable", "incident credit budget exhausted", wid)
        return
    s = body["stats"]
    problem = (f"Hallucination flag rate {s.get('rate', 0):.1%} on prompt {live} "
               f"(baseline {s.get('base_rate', 0):.1%}, n={s.get('n', 0)}); need a safe replacement")
    body["suggested"] = True
    body["credits_spent"] += settings.SUGGEST_FIXES_COST
    if persist:
        persist()  # a crash during the paid call must not pay again on restart
    try:
        resp = call_with_credit_ledger(ctx.client, ctx.store, wid, ctx.now, "suggest-fixes",
                                       ctx.client.suggest_fixes, wid, problem)
    except (OutcomeUnknown, SimUnavailable):
        return
    body["pending_suggestions"] = [sug["prompt_version"] for sug in resp.get("suggestions", [])]
    if persist:
        persist()  # the paid answer is durable before any validation is bought
    _validate_suggestions(ctx, wid, body, live, min_pass)
    if persist:
        persist()


def _validate_suggestions(ctx: PlanContext, wid: str, body: dict, live: str, min_pass: float) -> None:
    runs, still_pending = [], []
    for prompt in body.get("pending_suggestions", []):
        if prompt == live or prompt in body["tried"] or any(c["prompt"] == prompt for c in body["candidates"]):
            continue  # nothing to validate: drop
        run = _validated(ctx, wid, prompt, body, 0.0)
        if run is None:
            still_pending.append(prompt)  # unaffordable right now (cap / credits): keep the paid suggestion
        else:
            runs.append({"prompt": prompt, **run})
    body["pending_suggestions"] = still_pending
    for r in rank_by_failure_bound(runs, min_pass):
        body["candidates"].append(_candidate(r["prompt"], {"run_id": r["run_id"], "pass_rate": r["pass_rate"],
                                                           "n": r["n"]}, "llm") | {"fail_ub": r["fail_ub"]})


# ------------------------------------------------------------------ detection / incident management
def _edit_of_live_prompt(store, wid: str) -> dict:
    row = store.conn.execute("SELECT version, actor, ts_minute FROM config_versions WHERE workflow_id=?"
                             " AND changed_dims LIKE '%prompt_version%' ORDER BY version DESC LIMIT 1",
                             (wid,)).fetchone()
    return {"version": row[0], "actor": row[1], "minute": row[2]} if row else {}


def _baseline(st: Mapping, live: str, now: int) -> tuple[list | None, str | None, str]:
    """(counts, prompt, source): the first prompt's frozen baseline, else the newest known-good."""
    if live == st["first_prompt"] and st["base_frozen"]:
        return st["base"], live, "history" if st["base"][0] else "cold_start"
    cands = hd.known_good_candidates(st, live, now)
    return (cands[0][1], cands[0][0], "known_good") if cands else (None, None, "")


def _new_body(live: str, base_prompt: str | None, test: dict, edit: dict, kind: str) -> dict:
    return {"kind": kind, "bad_prompt": live, "baseline_prompt": base_prompt, "stats": test, "edit": edit,
            "candidates": [], "tried": [], "target": None, "reverts_checked": False, "suggested": False,
            "credits_spent": 0.0, "holds": True, "recheck_fail": 0}


def _open(ctx, wid: str, st: dict, live: str, base_prompt, test: dict, kind: str) -> None:
    edit = _edit_of_live_prompt(ctx.store, wid)
    body = _new_body(live, base_prompt, test, edit, kind)
    hd.mark_bad(st, live, ctx.now, kind)
    hd.save(ctx.store, wid, st)
    inc_id = open_incident(ctx.store, wid, KIND, ctx.now, body)
    ctx.store.add_alert(ctx.now, "warn", "halluc_incident",
                        f"{kind}: {live} flags {test['rate']:.1%} vs {test['base_rate']:.1%} (n={test['n']}, {p_text(test['p'])})", wid)
    prepare_fix(ctx, wid, body, st, live, lambda: update_incident(ctx.store, inc_id, body))
    update_incident(ctx.store, inc_id, body)


def _production_spike(ctx, wid: str, st: Mapping, live: str) -> tuple[dict, str | None, str] | None:
    """Spike test on the live prompt's own rolling bucket: relative to a baseline, else/also absolute."""
    win = hd.window_counts(st, live, ctx.now)
    base, base_prompt, source = _baseline(st, live, ctx.now)
    ceiling = _effective_ceiling(ctx.workflows[wid], base)
    test = None
    if base is not None:
        alpha = settings.HALLUC_P_DRIFT if base_prompt == live else settings.HALLUC_P
        test = segment_test(win, base, alpha)
        test["baseline_source"] = source
    ceil = ceiling_test(win, ceiling)
    if ctx.now % 60 < _tick_minutes(ctx) and test is not None:  # hourly audit row
        ctx.store.add_decision(ctx.now, "halluc_eval", f"{live}: flags {test['rate']:.2%} vs {test['base_rate']:.2%}"
                               f" (n={test['n']}, {p_text(test['p'])}, ceiling {ceiling:.1%})", wid,
                               {**test, "prompt": live, "ceiling": ceiling})
    if test is not None and test["triggered"]:
        return test, base_prompt, "prompt_flag_spike"
    if ceil["triggered"]:
        ceil["baseline_source"] = "governance_ceiling"
        return ceil, None, "prompt_flag_ceiling"
    return None


def _detect_day0(ctx, wid: str, st: dict, live: str) -> bool:
    """Open a Day-0 incident only on conclusive evidence; else record 'inconclusive' and monitor."""
    verdict = _day0_verdict(validations_of(ctx.store, wid, live), _wf_min_pass(ctx.workflows[wid]))
    if verdict is None:
        return False
    corroborated = _production_spike(ctx, wid, st, live) is not None
    if not (verdict["definite"] or verdict["confirmed"] or corroborated):
        if st["day0"].get(live) != "inconclusive":
            st["day0"][live] = "inconclusive"
            hd.save(ctx.store, wid, st)
            ctx.store.add_decision(ctx.now, "day0_inconclusive", f"{live}: one borderline validation failure "
                                   f"({verdict['flags']}/{verdict['n']}); confirming and monitoring", wid, verdict)
        return False
    test = {**verdict, "base_n": 0, "base_rate": 0.0, "p": 0.0, "adherence_drop": 0.0, "triggered": True,
            "validation_pass_rate": 1 - verdict["rate"],
            "evidence": "definite" if verdict["definite"] else "confirmed" if verdict["confirmed"] else "production"}
    _open(ctx, wid, st, live, None, test, "bad_prompt_day0")
    return True


_ABSOLUTE_KINDS = ("bad_prompt_day0", "prompt_flag_ceiling")


def _reopen_known_bad(ctx, wid: str, st: dict, live: str) -> None:
    """A prompt we blacklisted is live again with no incident open (e.g. closed as superseded): reopen for it."""
    reason = st["bad"][live]["reason"]
    base, base_prompt, source = _baseline(st, live, ctx.now)
    counts = hd.window_counts(st, live, ctx.now)
    if counts[0] < settings.HALLUC_MIN_CONNECTS:
        counts = hd.total_counts(st, live)
    if reason in _ABSOLUTE_KINDS or base is None:
        test, base_prompt = ceiling_test(counts, _effective_ceiling(ctx.workflows[wid], base)), None
        kind = reason if reason in _ABSOLUTE_KINDS else "prompt_flag_ceiling"
    else:
        test, kind = segment_test(counts, base), "prompt_flag_spike"
        test["baseline_source"] = source
    test = {**test, "triggered": True, "reopened": True}
    _open(ctx, wid, st, live, base_prompt, test, kind)


def _detect(ctx: PlanContext, wid: str, st: dict, live: str) -> None:
    if ctx.now - st.get("cleared", {}).get(live, -10**9) < settings.HALLUC_REOPEN_COOLDOWN_MINUTES:
        return
    if hd.is_bad(st, live, ctx.now):
        _reopen_known_bad(ctx, wid, st, live)
        return
    if _detect_day0(ctx, wid, st, live):
        return
    spike = _production_spike(ctx, wid, st, live)
    if spike is not None:
        test, base_prompt, kind = spike
        _open(ctx, wid, st, live, base_prompt, test, kind)


def _recheck(ctx, wid: str, inc: dict, st: dict, live: str) -> None:
    """While the bad prompt is still live: does the evidence still hold? Repeated failures close a false alarm."""
    body = inc["body"]
    if body["kind"] == "bad_prompt_day0":
        return
    spike = _production_spike(ctx, wid, st, live)
    win_n = hd.window_counts(st, live, ctx.now)[0]
    if spike is not None:
        body["holds"], body["recheck_fail"] = True, 0
    elif win_n >= settings.HALLUC_MIN_CONNECTS:
        body["holds"], body["recheck_fail"] = False, body.get("recheck_fail", 0) + 1
    else:
        return
    if body["recheck_fail"] >= settings.HALLUC_RECHECK_FAILS:
        hd.lift_bad(st, live)
        st.setdefault("cleared", {})[live] = ctx.now
        hd.save(ctx.store, wid, st)
        ctx.store.add_decision(ctx.now, "halluc_false_alarm", f"{live}: spike no longer holds on {win_n} connects;"
                               " incident closed, prompt blacklist lifted", wid, {"prompt": live, "n": win_n})
        close_incident(ctx.store, inc["id"], ctx.now, f"false alarm: {live} no longer exceeds its baseline")
        return
    update_incident(ctx.store, inc["id"], body)


def _verify(ctx: PlanContext, wid: str, inc: dict, st: dict, live: str) -> None:
    """Non-inferiority of the fix's flag rate at adequate n, plus the engagement guardrail."""
    body = inc["body"]
    new = hd.counts_since(st, live, inc["opened_minute"])
    if new[0] < settings.HALLUC_VERIFY_MIN_CONNECTS:
        return
    wf = ctx.workflows[wid]
    margin = _ceiling(wf) * settings.HALLUC_FLAG_MARGIN_FRACTION
    base_prompt = body.get("baseline_prompt")
    eng_ok, eng, p = True, {}, 0.0
    if base_prompt is None:  # absolute incident: the fix must sit under the governance ceiling
        flags_ok = wilson_upper(new[1], new[0], 1.645) <= _effective_ceiling(wf, _baseline(st, live, ctx.now)[0])
        bad_counts = hd.total_counts(st, body["bad_prompt"])
        if bad_counts[0] >= settings.HALLUC_BASELINE_MIN_CONNECTS:  # too few connects: engagement stays unchecked
            eng_ok, eng = engagement_ok(new, bad_counts, _max_engagement_drop(wf))
    else:
        base_counts = st["base"] if (base_prompt == st["first_prompt"] and st["base_frozen"]) \
            else hd.total_counts(st, base_prompt)
        flags_ok = non_inferior(new[1], new[0], base_counts[1], base_counts[0], margin)
        p = rate_greater_test(new[1], new[0], base_counts[1], base_counts[0])
        eng_ok, eng = engagement_ok(new, base_counts, _max_engagement_drop(wf))
    info = {"prompt": live, "n": new[0], "flag_rate": round(_rate(new), 5), "p": p, "margin": round(margin, 5),
            "flags_ok": flags_ok, "engagement_ok": eng_ok, "engagement_compared": bool(eng), **eng}
    significantly_worse = base_prompt is not None and p < settings.HALLUC_VERIFY_P
    if eng_ok and not flags_ok and not significantly_worse and new[0] < settings.HALLUC_VERIFY_MAX_CONNECTS:
        return  # inconclusive, not failed: keep collecting until non-inferiority is resolvable or clearly worse
    if flags_ok and eng_ok:
        ctx.store.add_decision(ctx.now, "incident_verified", f"{live}: flags {info['flag_rate']:.2%}, engagement ok", wid, info)
        close_incident(ctx.store, inc["id"], ctx.now,
                       f"fixed by {live}; flag rate non-inferior to baseline, engagement inside guardrail")
        return
    body["tried"].append(live)
    hd.mark_bad(st, live, ctx.now, "verify_failed")
    hd.save(ctx.store, wid, st)
    ctx.store.add_decision(ctx.now, "incident_verify_failed", f"{live} did not verify; trying next candidate", wid, info)
    prepare_fix(ctx, wid, body, st, live, lambda: update_incident(ctx.store, inc["id"], body))
    update_incident(ctx.store, inc["id"], body)


def _manage(ctx: PlanContext, wid: str, inc: dict, st: dict, live: str) -> None:
    body = inc["body"]
    if live == body["bad_prompt"]:
        _recheck(ctx, wid, inc, st, live)
        if body["target"] is None and (not (body["suggested"] and body["reverts_checked"])
                                       or body.get("pending_suggestions")):
            prepare_fix(ctx, wid, body, st, live, lambda: update_incident(ctx.store, inc["id"], body))
            update_incident(ctx.store, inc["id"], body)
    elif live == body["target"]:
        _verify(ctx, wid, inc, st, live)
    elif live in body["tried"]:
        if body["target"] is None and body.get("pending_suggestions"):  # unaffordable paid suggestions: finish them
            prepare_fix(ctx, wid, body, st, live, lambda: update_incident(ctx.store, inc["id"], body))
            update_incident(ctx.store, inc["id"], body)
        # else a failed fix is still live: waiting for the next candidate to be applied
    else:
        close_incident(ctx.store, inc["id"], ctx.now, f"superseded: prompt is now {live}, not {body['bad_prompt']}")


def _evaluate(ctx: PlanContext, wid: str, live: str) -> None:
    st = hd.update(ctx.store, wid)
    incs = open_incidents(ctx.store, wid, KIND)
    if incs:
        _manage(ctx, wid, incs[0], st, live)
    else:
        _detect(ctx, wid, st, live)


def halluc_detector(ctx: PlanContext) -> None:
    if not settings.ENABLE_HALLUC:
        return
    day0_validation(ctx)
    for wid in sorted(ctx.workflows):
        cfg = latest_config(ctx.store, wid)
        if cfg is None:
            continue
        try:
            _evaluate(ctx, wid, cfg[1]["prompt_version"])
        except Exception as exc:  # one workflow's failure must not starve the others
            log.exception("halluc evaluation failed for %s", wid)
            once_alert(ctx.store, ctx.now, "error", "halluc_workflow_error",
                       f"halluc detector failed: {type(exc).__name__}: {exc}", wid)


# ------------------------------------------------------------------ producer
def _describe_edit(edit: Mapping) -> str:
    if not edit:
        return "live prompt"
    who = edit.get("actor") or "unknown actor"
    return f"set by {who} {clock(edit['minute'])}" if edit.get("minute") is not None else f"set by {who}"


def halluc_producer(ctx: PlanContext, wid: str) -> list[DesiredChange]:
    if not settings.ENABLE_HALLUC:
        return []
    cfg = latest_config(ctx.store, wid)
    if cfg is None:
        return []
    out = []
    for inc in open_incidents(ctx.store, wid, KIND):
        body = inc["body"]
        target = body.get("target")
        if not target or target == cfg[1]["prompt_version"]:
            continue
        if not body.get("holds", True):  # the detector re-ran the spike test this tick and it no longer holds
            continue
        cand = next(c for c in body["candidates"] if c["prompt"] == target)
        s = body["stats"]
        live = cfg[1]["prompt_version"]
        base = f"{s['base_rate']:.1%} baseline ({body['baseline_prompt']})" if body.get("baseline_prompt") else "failed Day-0 validation"
        summary = (f"Revert {live} ({_describe_edit(body['edit'])}) to {target}" if cand["source"] == "known_good"
                   else f"Replace {live} ({_describe_edit(body['edit'])}) with {target}")
        summary += (f": flags {s['rate']:.1%} vs {base} (n={s['n']}, {p_text(s['p'])}); "
                    f"validation {cand['validation_run_id']} pass {cand['pass_rate']:.3f}")
        excess = max(s["rate"] - s["base_rate"], 0.0)
        evidence = {"validation_run_id": cand["validation_run_id"], "impact_override": min(1.0, excess / max(_ceiling(ctx.workflows[wid]), 1e-9)),
                    "rationale": summary + f". Fix source: {cand['source']}; engagement is verified after apply."}
        out.append(DesiredChange(wid, "halluc_fix", Priority.INCIDENT, {"prompt_version": target},
                                 ROUTE_APPROVAL, evidence, summary))
    return out


def register(registry=DEFAULT_REGISTRY) -> None:
    registry.register_detector(halluc_detector)
    registry.register_producer(halluc_producer)
