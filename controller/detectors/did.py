"""Caller-ID health detector and producer (CONTEXT C5), behind ``settings.ENABLE_DID``.

Detector (each tick, before the planner): learn the fatigue curve (refit once per sim day), run the
step test (FLAGGED -> incident mode) and the peer comparison (WEAK BASE, passing), persist the
verdicts and write a ``decisions`` row with every input so ``why`` can explain it.

Producer (pure function of persisted state + data): the caller-ID allocation we currently want,
as a bounded step (auto-apply where the policy allows it, otherwise one approval).
"""
from __future__ import annotations

import logging
from typing import Mapping

from .. import settings
from ..configlib import dist
from ..stats import p_text
from ..desired import DEFAULT_REGISTRY, ROUTE_APPROVAL, ROUTE_AUTO, DesiredChange, PlanContext, Priority
from ..incidents import close_incident, open_incident, open_incidents
from . import did_data, did_eval, did_plan, did_solo
from .common import latest_config, load_state, note_once, once_alert, save_state
from .did_owner import clock, minimal_evacuation, supervisor_edit
from .did_plan import BAD, FLAGGED, REPROBE, SUSPECT, TESTED, UNTESTED, WEAK
from .fatigue_curve import FatigueCurve, fit_curve

log = logging.getLogger(__name__)
DETECTOR = "did"
GUARD_TOLERANCE = settings.DID_PROBE_BASE_STEP  # a non-safety move may cost at most this much expected DID health (0.5pp margin to the -2% line)
MEMO: dict[tuple[str, str], tuple[int, did_eval.Evaluation, dict]] = {}


# ------------------------------------------------------------------ persisted verdicts
def _state(store, wid: str) -> dict:
    st = load_state(store, DETECTOR, wid)
    for key in ("flagged", "weak", "tested", "reprobe", "pending", "weak_days"):
        st.setdefault(key, {})
    return st


def _curve(store, wid: str, series, now: int, st: dict) -> FatigueCurve:
    day = now // 1440
    if st.get("curve") and st.get("curve_day") == day:
        return FatigueCurve.from_json(st["curve"])
    curve = fit_curve(series.obs())
    st["curve"], st["curve_day"] = curve.to_json(), day
    store.add_decision(now, "did_curve_fit", f"fatigue curve refit: tau={curve.tau_hours:.0f}h"
                       f" fitted={curve.fitted} points={curve.n_obs}", wid, curve.params())
    return curve


def status_of(ev: did_eval.Evaluation, did: str, st: Mapping) -> str:
    if did in st["flagged"]:
        return FLAGGED
    if did in st["weak"]:
        return WEAK
    if did in st["tested"]:
        return TESTED
    first = ev.first_hour.get(did)
    reprobing = did in st["reprobe"]
    if (ev.total_dials.get(did, 0) < settings.DID_PASS_MIN_DIALS or first is None
            or ev.cur_hour - first < settings.DID_PASS_MIN_AGE_HOURS):
        return REPROBE if reprobing else UNTESTED
    peer = ev.peers.get(did)
    if peer is None and reprobing:
        return REPROBE  # a re-probed number must be positively compared with its peers before it earns traffic
    if peer is None or (peer.q >= settings.DID_PASS_RATIO and peer.z_weak > -settings.DID_PASS_Z):
        return TESTED
    return SUSPECT


def h_bar(ev: did_eval.Evaluation, alloc: Mapping[str, float]) -> float:
    """Traffic-weighted relative health. Weights are the OBSERVED trailing shares when known (the config
    share lags ``next_campaign_only`` and cohort overrides), else the config allocation."""
    weights = ev.shares if ev.share_dials >= settings.DID_SHARE_MIN_DIALS and ev.shares else alloc
    total = sum(weights.values()) or 1.0
    return sum(w * ev.curve.h(ev.loads_now.get(d, 0.0)) for d, w in weights.items()) / total


# ------------------------------------------------------------------ detector
def _evaluate(ctx: PlanContext, wid: str) -> tuple[did_eval.Evaluation, dict] | None:
    """Evaluation at ``ctx.now`` (memoised per tick), plus the mutable verdict state."""
    key = (str(ctx.store.path), wid)
    memo = MEMO.get(key)
    if memo and memo[0] == ctx.now:
        return memo[1], memo[2]
    options = ctx.workflows[wid]["options"]["caller_id"]
    series = did_data.load_series(ctx.store, wid, ctx.now)
    if not any(series.total_dials(d) for d in options):
        return None
    st = _state(ctx.store, wid)
    curve = _curve(ctx.store, wid, series, ctx.now, st)
    since = {d: v["minute"] // 60 for d, v in st["reprobe"].items()}
    ev = did_eval.evaluate(series, curve, ctx.now // 60, options, list(st["flagged"]) + list(st["reprobe"]),
                           list(st["flagged"]) + list(st["weak"]), since)
    _add_solo_steps(ctx, wid, ev, series, curve, options, st)
    MEMO[key] = (ctx.now, ev, st)
    return ev, st


def _add_solo_steps(ctx, wid: str, ev, series, curve, options, st: dict) -> None:
    """A1: a DID with no peer step test (no peers with enough dials) is tested against its own history."""
    cur = ctx.now // 60
    tested = {r.did for r in ev.steps}
    recent, _ = series.window(range(cur - 24 + 1, cur + 1))
    solo = [d for d in options if recent.get(d, 0) >= settings.DID_FLAG_MIN_DIALS
            and d not in tested and d not in st["flagged"] and d not in st["weak"]]
    ev.steps = list(ev.steps) + did_solo.peerless_steps(ctx.store, wid, series, curve, cur, solo)


def _flag(ctx: PlanContext, wid: str, ev, st: dict, step: did_eval.StepResult) -> None:
    q_est = step.test.q_window / max(step.test.q_ref, 1e-9)
    st["flagged"][step.did] = {"minute": ctx.now, "drop": round(step.test.drop, 4), "p": step.test.p,
                               "q_est": round(1.0 - step.test.drop, 4), "n": step.n_window}
    open_incident(ctx.store, wid, "did_flagged", ctx.now,
                  {"did": step.did, "step": step.as_dict(), "q_est": round(q_est, 4)})
    ref = "its own history (hour/cell/fatigue adjusted)" if step.solo else "peers"
    msg = (f"FLAGGED {step.did}: pickup ratio vs {ref} fell {step.test.drop:.0%} ({p_text(step.test.p)}, "
           f"{step.n_window} dials in {step.window_hours}h vs {step.n_ref} in the reference); incident mode")
    ctx.store.add_decision(ctx.now, "did_flagged", msg, wid, {"step": step.as_dict(),
                                                              "curve": ev.curve.params()})
    ctx.store.add_alert(ctx.now, "warn", "did_flagged", msg, wid)


def _mark_weak_and_tested(ctx: PlanContext, wid: str, ev, st: dict, options) -> None:
    for did, res in ev.peers.items():
        if did in st["flagged"] or did in st["weak"]:
            continue
        days = st.setdefault("weak_days", {}).setdefault(did, [])
        if not res.weak():
            days.clear()  # persistence must be unbroken: a healthy evaluation resets the candidate
        else:
            day = ctx.now // 1440
            if day not in days:
                days.append(day)
                ctx.store.add_decision(ctx.now, "did_weak_candidate",
                                       f"{did} looks weak (pickup {res.q:.2f}x expected, z={res.z_weak:.1f}, n={res.n}); "
                                       f"day {len(days)} of {settings.DID_WEAK_PERSIST_DAYS} needed before acting", wid,
                                       res.as_dict())
            if len(days) >= settings.DID_WEAK_PERSIST_DAYS:
                st["weak"][did] = {"minute": ctx.now, "q": round(res.q, 4), "z": round(res.z_weak, 3)}
                ctx.store.add_decision(ctx.now, "did_weak_base",
                                       f"WEAK BASE {did}: pickup {res.q:.2f}x expected at comparable load "
                                       f"(z={res.z_weak:.1f}, n={res.n}) on {len(days)} days", wid, res.as_dict())
    for did in options:
        if did not in st["tested"] and status_of(ev, did, st) == TESTED:
            st["tested"][did] = ctx.now
            st["reprobe"].pop(did, None)
            ctx.store.add_decision(ctx.now, "did_tested", f"{did} passes: not worse than peers at its load",
                                   wid, {"did": did, "peer": ev.peers[did].as_dict() if did in ev.peers else None,
                                         "dials": ev.total_dials.get(did)})


def _close_evacuated(ctx: PlanContext, wid: str, ev, st: dict) -> None:
    """An evacuation closes when the flagged DID's OBSERVED trailing share is ~0 (the config share says what
    we asked for; under next_campaign_only old campaigns keep their DID until their cadence ends)."""
    cfg = latest_config(ctx.store, wid)
    if cfg is None or ev.share_dials < settings.DID_SHARE_MIN_DIALS:
        return
    for inc in open_incidents(ctx.store, wid, "did_flagged"):
        did = inc["body"]["did"]
        if ev.shares.get(did, 0.0) < settings.DID_EVAC_CLOSE_SHARE and _config_share(cfg[1], did) <= 1e-9:
            close_incident(ctx.store, inc["id"], ctx.now, f"{did} evacuated: observed share {ev.shares.get(did, 0.0):.2%}"
                           f" over the trailing window, config v{cfg[0]} share 0 in every key")


def _config_share(cfg: Mapping, did: str) -> float:
    """Largest configured share of ``did`` over every allocation key that sets caller_id."""
    return max((dist(cfg, key, "caller_id").get(did, 0.0) for key in _keys(cfg)), default=0.0)


def _expire_flags(ctx: PlanContext, wid: str, ev, st: dict) -> None:
    """A flag is not permanent: after the cooldown (and once evacuated) the DID is re-probed at the re-probe cap."""
    open_dids = {i["body"]["did"] for i in open_incidents(ctx.store, wid, "did_flagged")}
    for did, f in sorted(st["flagged"].items()):
        if ctx.now - f["minute"] < settings.DID_FLAG_COOLDOWN_MINUTES or did in open_dids:
            continue
        del st["flagged"][did]
        MEMO.pop((str(ctx.store.path), wid), None)  # this tick's evaluation predates the re-probe window: redo it
        st["tested"].pop(did, None)
        st["reprobe"][did] = {"minute": ctx.now, "flagged_minute": f["minute"]}
        ctx.store.add_decision(ctx.now, "did_flag_expired", f"flag on {did} expired after the cooldown; re-probing it at the"
                               " re-probe cap", wid, {"did": did, "flagged": f})


def _persisted(ctx: PlanContext, st: dict, cand) -> bool:
    """The flag condition must hold on DID_FLAG_PERSIST_EVALS consecutive evaluations (one noisy look is not enough)."""
    pend = st["pending"]
    for d in [d for d in pend if cand is None or d != cand.did]:
        del pend[d]  # a clean evaluation (or a different DID) restarts the count
    if cand is None:
        return False
    e = pend.get(cand.did)
    if e and ctx.now - e["last"] <= settings.DID_FLAG_PERSIST_GAP_MINUTES:
        e["count"] += 1
    else:
        e = {"count": 1, "first": ctx.now}
    e["last"] = ctx.now
    pend[cand.did] = e
    if e["count"] >= settings.DID_FLAG_PERSIST_EVALS:
        del pend[cand.did]
        return True
    return False


def did_detector(ctx: PlanContext) -> None:
    if not settings.ENABLE_DID:
        return
    for wid in sorted(ctx.workflows):
        try:
            _detect_workflow(ctx, wid)
        except Exception as exc:  # one workflow's failure must not blind every other workflow's detection
            log.exception("did detection failed for %s", wid)
            once_alert(ctx.store, ctx.now, "error", "did_workflow_error",
                       f"did detector failed: {type(exc).__name__}: {exc}", wid)


def _detect_workflow(ctx: PlanContext, wid: str) -> None:
    options = ctx.workflows[wid]["options"]["caller_id"]
    if len(options) < 2:
        return
    got = _evaluate(ctx, wid)
    if got is None:
        return
    ev, st = got
    active = sum(ev.window_dials.values()) > 0 and ctx.now % 1440 // 60 in range(*ctx.workflows[wid]["dialling_window"])
    level = settings.DID_FLAG_P if ev.curve.fitted else settings.DID_FLAG_P_UNFITTED  # flat curve: stricter
    flagged = did_eval.flagged_step(ev.steps, level) if active else None
    if flagged is not None and flagged.did in st["flagged"]:
        flagged = None
    if active and _persisted(ctx, st, flagged):
        _flag(ctx, wid, ev, st, flagged)
    _mark_weak_and_tested(ctx, wid, ev, st, options)
    _close_evacuated(ctx, wid, ev, st)
    _expire_flags(ctx, wid, ev, st)
    save_state(ctx.store, DETECTOR, wid, st)
    if active:
        _log_evaluation(ctx, wid, ev, st, options)


def _log_evaluation(ctx, wid: str, ev, st: dict, options) -> None:
    status = {d: status_of(ev, d, st) for d in options}
    best = min(ev.steps, key=lambda r: r.test.p, default=None)
    inputs = {"status": status, "curve": ev.curve.params(), "loads": {d: round(v, 1) for d, v in ev.loads_now.items()},
              "window_dials": ev.window_dials, "peers": {d: r.as_dict() for d, r in ev.peers.items()},
              "best_step": best.as_dict() if best else None, "flagged": st["flagged"], "weak": st["weak"]}
    ctx.store.add_decision(ctx.now, "did_eval", "caller-ID evaluation: " + ", ".join(
        f"{d}={s}" for d, s in status.items()), wid, inputs)


# ------------------------------------------------------------------ producer
def _auto_limit(wf: Mapping) -> float | None:
    pol = wf["governance"]["auto_apply"]
    if pol.get("enabled") and "caller_id" in pol.get("allowed_dims", []):
        return float(pol["max_shift"])
    return None


def _keys(cfg: Mapping) -> list[str]:
    return ["default"] + [c["id"] for c in cfg["cohorts"]
                          if "caller_id" in (cfg["allocations"].get(c["id"]) or {})]


def did_producer(ctx: PlanContext, wid: str) -> list[DesiredChange]:
    if not settings.ENABLE_DID:
        return []
    wf = ctx.workflows[wid]
    options = wf["options"]["caller_id"]
    cfg = latest_config(ctx.store, wid)
    if len(options) < 2 or cfg is None:
        return []
    got = _evaluate(ctx, wid)
    if got is None:
        return []
    ev, st = got
    status = {d: status_of(ev, d, st) for d in options}
    limit = _auto_limit(wf)
    patch: dict = {}
    notes: list[str] = []
    moves: list[tuple[dict, dict]] = []  # (current, step) per allocation key, for the safety impact
    owned: list[tuple[str, dict, dict, dict]] = []  # (key, supervisor edit, current, evacuation step)
    for key in _keys(cfg[1]):
        cur = dist(cfg[1], key, "caller_id")
        sup = supervisor_edit(ctx.store, wid, key)
        if sup:  # B1: a supervisor's allocation is owned; the only exception is evacuating a FLAGGED DID (approval)
            step = _hold_owned(ctx, wid, key, cur, sup, status, st)
            if step:
                owned.append((key, sup, dict(cur), step))
            continue
        target = did_plan.target_allocation(cur, status, h_bar(ev, cur))
        step = did_plan.plan_step(cur, target, limit, status)
        if limit is not None and _stalled_evacuation(cur, step, st):
            limit, step = None, did_plan.plan_step(cur, target, None, status)  # a tiny max_shift must not strand a flagged DID: approval
        if not _move_not_harmful(ctx, wid, key, ev, st, status, cfg[1], cur, step):
            continue
        floor = settings.DID_MIN_TV if limit is not None else settings.DID_MIN_TV_APPROVAL
        evacuating = any(cur.get(d, 0.0) > 1e-9 and step.get(d, 0.0) < cur.get(d, 0.0) for d in st["flagged"])
        if did_plan.tv(cur, step) < floor and not (set(step) - set(cur)) and not evacuating:  # a new DID / a flagged DID is always worth it
            continue
        patch.setdefault("allocations", {}).setdefault(key, {})["caller_id"] = step
        moves.append((dict(cur), dict(step)))
        notes.append(f"{key}: " + ", ".join(f"{d} {cur.get(d, 0):.1%}->{step.get(d, 0):.1%}"
                                            for d in sorted(set(cur) | set(step))))
    out = [_change(wid, patch, status, st, ev, notes, limit, cfg[1], moves)] if patch else []
    return out + ([_owned_change(wid, owned, st)] if owned else [])


def _stalled_evacuation(cur: Mapping, step: Mapping, st: Mapping) -> bool:
    """A flagged DID still holds share but the auto-bounded step is too small to be applied (below the auto floor)."""
    holds = any(cur.get(d, 0.0) > 1e-9 for d in st["flagged"])
    return holds and did_plan.tv(cur, step) < settings.DID_MIN_TV


def _hold_owned(ctx: PlanContext, wid: str, key: str, cur: Mapping, sup: Mapping, status: Mapping, st: Mapping) -> dict | None:
    """Log the hold; return the minimal flagged-DID evacuation step for a supervisor-owned key, if one is needed."""
    bad = {d for d in st["flagged"] if cur.get(d, 0.0) > 1e-9}
    step = minimal_evacuation(cur, bad, [d for d, s in status.items() if s == TESTED]) if bad else None
    if step is not None and (set(step) & bad or not step):
        step = None  # nowhere safe to move the traffic: leave it for the supervisor
    if note_once(ctx.store, f"did_owned:{wid}:{key}:{sorted(bad)}", ctx.now):
        what = (f"; only evacuating FLAGGED {', '.join(sorted(bad))} (via approval)" if step else "; no probe, spread or rebalance")
        ctx.store.add_decision(ctx.now, "did_supervisor_hold",
                               f"caller_id allocation of '{key}' was set by a supervisor at config v{sup['version']} "
                               f"({clock(sup['minute'])}); treating it as owned{what}", wid,
                               {"key": key, "supervisor_edit": dict(sup), "flagged": sorted(bad)})
    return step


def _owned_change(wid: str, owned: list, st: Mapping) -> DesiredChange:
    patch: dict = {}
    parts, edits = [], {}
    for key, sup, cur, step in owned:
        patch.setdefault("allocations", {}).setdefault(key, {})["caller_id"] = step
        bad = sorted(d for d in st["flagged"] if cur.get(d, 0.0) > 1e-9)
        q = min(st["flagged"][d]["q_est"] for d in bad)
        edits[key] = dict(sup)
        parts.append(f"'{key}': move {', '.join(bad)} (pickup ratio vs peers x{q:.2f}, FLAGGED) onto "
                     + ", ".join(f"{d} {cur.get(d, 0):.1%}->{step.get(d, 0):.1%}" for d in sorted(step))
                     + f"; this allocation was set by a supervisor at config v{sup['version']} ({clock(sup['minute'])}),"
                     " so only the flagged number is removed and the rest of their split is kept proportionally")
    summary = "Safety evacuation of a flagged caller ID from a supervisor-set allocation (needs approval): " + " | ".join(parts)
    first = next(iter(edits.values()))
    deficits = {d: 1.0 - f["q_est"] for d, f in st["flagged"].items()}
    evidence = {"rationale": summary, "flag_evacuation": True, "supervisor_edit": first, "supervisor_edits": edits,
                "impact_override": did_plan.evacuation_impact([(c, s) for _, _, c, s in owned], deficits)}
    return DesiredChange(wid, "did_evacuation", Priority.INCIDENT, patch, ROUTE_APPROVAL, evidence, summary)


def _quality(ev, st, status: Mapping | None, dids) -> dict[str, float]:
    """Observed pickup ratio per DID; a DID with no comparison is 1.0 if already tested, else the conservative prior."""
    out = {}
    for d in dids:
        if d in ev.peers:
            out[d] = ev.peers[d].q
        elif d in st["tested"] or (status or {}).get(d) == TESTED:
            out[d] = 1.0
        else:
            out[d] = settings.DID_H_FLOOR
    return out


def _rebalance_not_harmful(ev, st, cfg: Mapping, cur: Mapping, step: Mapping, status: Mapping | None = None) -> bool:
    """Every non-safety move (spread, ramp after a probe, weak rebalance) must be expected not to lower DID health.

    Expected health uses each DID's OBSERVED pickup ratio at comparable load and the fatigue curve at the load it
    would carry after the move; a verdict that came from noise cannot justify moving load onto busier numbers.
    A weak-base rebalance must gain; any other move may cost at most ``GUARD_TOLERANCE`` (the probe budget).
    Safety evacuation of a flagged DID is exempt: its pickup drop is measured, not assumed.
    """
    if any(_config_share(cfg, d) > 1e-9 for d in st["flagged"]):
        return True
    quality = _quality(ev, st, status, set(cur) | set(step))
    gain = did_plan.expected_health(ev, step, quality) - did_plan.expected_health(ev, cur, quality)
    if any(cur.get(d, 0.0) > 1e-9 for d in st["weak"]):
        return gain >= settings.DID_REBALANCE_MIN_HEALTH_GAIN - 1e-12
    return gain >= -GUARD_TOLERANCE - 1e-9


def _move_not_harmful(ctx, wid: str, key: str, ev, st, status, cfg: Mapping, cur: Mapping, step: Mapping) -> bool:
    if _rebalance_not_harmful(ev, st, cfg, cur, step, status):
        return True
    if note_once(ctx.store, f"did_guard:{wid}:{key}", ctx.now):
        quality = _quality(ev, st, status, set(cur) | set(step))
        gain = did_plan.expected_health(ev, step, quality) - did_plan.expected_health(ev, cur, quality)
        moved = ", ".join(f"{d} {cur.get(d, 0):.1%}->{step.get(d, 0):.1%}" for d in sorted(set(cur) | set(step)))
        ctx.store.add_decision(ctx.now, "did_move_blocked",
                               f"not proposing the '{key}' caller-ID move ({moved}): expected DID health would change by "
                               f"{gain:+.1%} (limit -{GUARD_TOLERANCE:.1%}; weak rebalances must gain)", wid,
                               {"key": key, "current": dict(cur), "step": dict(step), "expected_gain": round(gain, 4),
                                "quality": {d: round(q, 3) for d, q in quality.items()}})
    return False


def _change(wid, patch, status, st, ev, notes, limit, cfg, moves=()) -> DesiredChange:
    flagged_live = [d for d in st["flagged"] if _config_share(cfg, d) > 1e-9]  # in ANY allocation key
    bad_live = [d for d in st["weak"] if _config_share(cfg, d) > 1e-9]
    status_txt = ", ".join(f"{d}={s}" for d, s in status.items())
    evidence = {"rationale": f"caller-ID load spreading; statuses: {status_txt}; " + "; ".join(notes)}
    route = ROUTE_AUTO if limit is not None else ROUTE_APPROVAL
    if flagged_live:
        worst = min(st["flagged"][d]["q_est"] for d in flagged_live)
        evidence["flag_evacuation"] = worst < settings.DID_EVAC_FAST_RATIO
        evidence["impact_override"] = did_plan.evacuation_impact(
            moves, {d: 1.0 - st["flagged"][d]["q_est"] for d in flagged_live})
        summary = (f"Evacuate flagged caller ID {', '.join(flagged_live)} (pickup ratio vs peers x{worst:.2f}): "
                   + "; ".join(notes))
        return DesiredChange(wid, "did_evacuation", Priority.INCIDENT, patch, route, evidence, summary)
    kind = "did_rebalance" if bad_live else ("did_probe" if (UNTESTED in status.values() or REPROBE in status.values()) else "did_spread")
    summary = {"did_rebalance": "Move traffic off weak-base caller ID", "did_probe": "Probe/spread caller IDs within "
               "the safe probe cap", "did_spread": "Spread calls evenly over healthy caller IDs"}[kind]
    return DesiredChange(wid, kind, Priority.ROUTINE, patch, route, evidence,
                         f"{summary} (h-bar {h_bar(ev, dist(cfg, 'default', 'caller_id')):.2f}): " + "; ".join(notes))


def register(registry=DEFAULT_REGISTRY) -> None:
    registry.register_detector(did_detector)
    registry.register_producer(did_producer)
