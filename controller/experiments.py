"""C6 + C7 driver: welcome_message and voice experiments on engagement-primary workflows.

Detector (each tick, before the planner) advances a per-(workflow, dim) state machine kept in ``detector_state``:

  pending -> running (entry applied) -> promoted (stage 1: winner 50% / old control 50%; stage 2: winner 90% /
  holdout 10% after >= 24 h at stage 1) -> closed (rollback / concluded)
  any phase -> interrupted by a supervisor edit of the dim -> a fresh pending experiment with the edit as control

Producer (pure function of that state): the single allocation the experiment currently wants for the dim
(``desired``). Entry, ramp, drop and both promotion stages are customer-facing and always go through approval (daily
bundle); only the risk-reducing harm-stop and rollback ask for auto-apply (stepped to max_shift when needed), which
the planner downgrades to the reserved approval slot when the policy does not allow it. Before any approved
ramp/drop/promotion is applied the evidence is re-checked (``experiments_veto``). Behind ``ENABLE_EXPERIMENTS``.
"""
from __future__ import annotations

import json
import logging
from typing import Mapping

from . import exp_alloc, exp_data, exp_decay, exp_entry, exp_interactions, exp_reconcile, exp_stats as es, exp_proposals as prop, exp_rules as er, exp_state as xs, settings
from .cohorts import VOCAB_META_KEY, FeatureVocab
from .desired import DEFAULT_REGISTRY, ROUTE_APPROVAL, ROUTE_AUTO, DesiredChange, PlanContext, Priority
from .detectors import did_plan
from .detectors.common import latest_config, note_once, once_alert

log = logging.getLogger(__name__)
DAY = settings.MINUTES_PER_DAY


UNMIRRORED_GRACE_MINUTES = 120  # a config version whose body is not mirrored yet blocks the watch for this long


def _auto_ok(wf: Mapping, dim: str) -> bool:
    """Auto-apply needs the policy on, the dim allowed and a positive max_shift (0 or missing leaves no step to take)."""
    pol = wf["governance"]["auto_apply"]
    return bool(pol.get("enabled")) and dim in pol.get("allowed_dims", []) and float(pol.get("max_shift") or 0) > 0


def _look_minute(wf: Mapping, tick: int = settings.TICK_MINUTES) -> int:
    """Minute of the day from which the daily look runs: the window end, or the day's last tick when it ends at 24."""
    return min(wf["dialling_window"][1] * 60, DAY - tick)


def _guard(wf: Mapping) -> float:
    return float(((wf.get("objective") or {}).get("guardrail") or {}).get("pickup_rate_max_drop", 0.02))


def _version_row(store, wid: str, version: int) -> tuple[str | None, int | None]:
    row = store.conn.execute("SELECT actor, ts_minute FROM config_versions WHERE workflow_id=? AND version=?",
                             (wid, version)).fetchone()
    return (row[0], row[1]) if row else (None, None)


# ------------------------------------------------------------------ state transitions
def _new_state(wf: Mapping, dim: str, cfg: tuple[int, dict], excluded=(), note: str = "", not_before: int = 0) -> dict:
    live = xs.live_dist(cfg[1], dim)
    return {"phase": xs.PENDING, "control": xs.top_option(live), "options": list(wf["options"][dim]),
            "excluded": sorted(excluded), "note": note, "not_before": not_before, "shares": {}, "arms": {},
            "family": [], "desired": None, "live": live, "live_cohorts": xs.cohort_overrides(cfg[1], dim), "seen_version": cfg[0], "start_minute": None,
            "scopes": {"all": {"arm_t": {}, "looks": 0}}, "ramped": [], "groups": None, "last_look_day": -1, "data_since": 0,
            "last_check": -10 ** 9, "history": []}


def _snapshot(st: dict) -> dict:
    """What a withheld ramp/drop/promotion restores (apply-time veto)."""
    return json.loads(json.dumps({k: st.get(k) for k in ("shares", "arms", "ramped", "untested", "family", "phase")}))


def _set_desired(st: dict, weights, kind: str, summary: str, rationale: str, *, severe=False, then=None,
                 cohort_weights=None, extra: Mapping | None = None, impact: float | None = None,
                 prev: Mapping | None = None) -> None:
    earlier = st.get("desired")
    severe = severe or bool(earlier and earlier.get("severe"))
    st["desired"] = {"weights": dict(weights), "cohort_weights": cohort_weights or {}, "kind": kind,
                     "summary": summary, "rationale": rationale, "severe": severe, "then": then,
                     "evidence": dict(extra or {}), "impact": impact, "prev": prev}


def _plan_entry(ctx: PlanContext, st: dict, wid: str, dim: str, cfg: tuple[int, dict]) -> None:
    shares, incumbents, untested = exp_alloc.entry_plan(st["live"], st["control"], st["options"], st["excluded"])
    new = [a for a in shares if a not in incumbents]
    if new and not _entry_powered(ctx, st, wid, dim, cfg, new):
        for a in new:
            shares.pop(a)
        untested = untested + new
    if not shares:
        return
    st["shares"], st["family"], st["incumbents"] = shares, sorted(shares), incumbents
    st["arms"] = {a: {"state": "active"} for a in shares}
    st["untested"] = untested
    weights = exp_alloc.weights(st["control"], shares)
    if xs.same_dist(weights, st["live"]):  # nothing to change (incumbents only): the experiment is simply running
        st["phase"], st["applied"] = xs.RUNNING, {"kind": "experiment_entry", "minute": ctx.now}
        ctx.store.add_decision(ctx.now, "experiment_entry", f"{dim}: the live split {prop.weights_text(weights)} is the entry; "
                               "incumbents are analysed as arms, no change proposed", wid,
                               {"dim": dim, "control": st["control"], "shares": shares, "weights": weights, "no_change": True})
        return
    summary, rationale, impact = prop.entry(dim, st["control"], weights, st["note"])
    _set_desired(st, weights, "experiment_entry", summary, rationale, then=xs.RUNNING, impact=impact)
    ctx.store.add_decision(ctx.now, "experiment_entry", summary, wid,
                           {"dim": dim, "control": st["control"], "shares": shares, "weights": weights,
                            "incumbents": incumbents})


def _entry_powered(ctx, st, wid, dim, cfg, new: list) -> bool:
    """False (and re-check tomorrow) when a new challenger cannot be projected to reach a test over the rest of the run."""
    proj = exp_entry.projected_arm_connects(ctx, wid, dim, cfg[1], settings.EXP_ENTRY_SHARE)
    if proj is None or proj >= exp_entry.MIN_PROJECTED_ARM_CONNECTS:
        return True
    st["not_before"] = ctx.now + DAY
    ctx.store.add_decision(ctx.now, "experiment_entry_skipped", f"{dim}: no entry, a challenger at {settings.EXP_ENTRY_SHARE:.1%} is "
                           f"projected to get {proj:.0f} connects over the rest of the run (< {exp_entry.MIN_PROJECTED_ARM_CONNECTS} "
                           "needed to be tested); re-checking daily", wid,
                           {"dim": dim, "projected_connects": round(proj, 1), "needed": exp_entry.MIN_PROJECTED_ARM_CONNECTS,
                            "challengers": new})
    return False


def _on_applied(ctx: PlanContext, st: dict, wid: str, dim: str, minute: int) -> None:
    d = st["desired"]
    st["desired"] = None
    st["applied"] = {"kind": d["kind"], "minute": minute}
    if d["then"] == xs.RUNNING:
        st["phase"] = xs.RUNNING
    elif d["then"] == xs.PROMOTED:
        st["phase"] = xs.PROMOTED
        stage = 2 if d["kind"] == "experiment_stage2" else 1
        promo = st["promo"]
        promo.update(stage=stage, stage_minute=minute, seq={"start": minute, "ts": []})
        if stage == 1:
            promo["minute"] = minute
    elif d["then"] == xs.CLOSED:
        st["phase"] = xs.CLOSED
    ctx.store.add_decision(ctx.now, "experiment_applied", f"{d['kind']} for {dim} is live ({xs.clock(minute)}): "
                           f"{prop.weights_text(d['weights'])}", wid, {"kind": d["kind"], "phase": st["phase"]})


def _below_new_control(ctx: PlanContext, st: dict, wid: str, dim: str, new_control: str, already: list) -> list[str]:
    """Arms that ran before a supervisor edit and whose own data point BELOW the new control are not re-entered.

    Their pre-edit data stays valid evidence (they ran concurrently with the new control's share), so re-exposing
    customers to an arm that was already behind it would be an expected-harmful entry. Arms with no data stay eligible.
    """
    if st["start_minute"] is None:
        return []
    data = exp_data.load_arms(ctx.store, wid, dim, st["start_minute"], ctx.now, _arm_cells(ctx, st, wid, dim))
    ctl = data.get(new_control)
    out = []
    for arm in st["options"]:
        d = data.get(arm)
        if arm == new_control or arm in already or d is None or ctl is None:
            continue
        c = er.pair_contrast(d, ctl)
        if c is not None and c.n_a >= settings.EXP_RAMP_MIN_CONNECTS and c.lift < 0:
            out.append(arm)
    return sorted(out)


def _interrupt(ctx: PlanContext, st: dict, wid: str, dim: str, cfg, version: int, minute: int, dist: Mapping) -> dict:
    new_control = xs.top_option(dist)
    bad = [a for a, v in st["arms"].items() if v["state"] in ("dropped", "harm_stopped")]
    bad += _below_new_control(ctx, st, wid, dim, new_control, bad)
    note = (f"A client supervisor set {dim} to {new_control} at {xs.clock(minute)} (config v{version}); treating it as "
            f"the new control (never re-imposing {st['control']}). ")
    fresh = _new_state(ctx.workflows[wid], dim, cfg, st["excluded"] + bad, note, minute + settings.EXP_SUPERVISOR_COOLDOWN_MINUTES)
    fresh["history"] = st["history"] + [{k: st[k] for k in ("phase", "control", "arms", "shares", "scopes", "promo", "applied")
                                           if k in st} | {"interrupted_minute": minute, "interrupted_version": version}]
    fresh["control"], fresh["live"] = new_control, dict(dist)
    fresh["data_since"] = minute  # a re-entered arm restarts its sample-size floor on post-edit data only
    ctx.store.add_decision(ctx.now, "experiment_interrupted", f"supervisor edit of {dim} at {xs.clock(minute)} (v{version}): "
                           f"experiment interrupted, {new_control} is the new control; pre-edit data stays valid for arms that "
                           "ran; nothing will re-impose the old allocation", wid,
                           {"dim": dim, "version": version, "new_control": new_control, "old_control": st["control"],
                            "dist": dict(dist), "pre_edit_arms": st["arms"]})
    return fresh


def _pause_for_edit(ctx, st: dict, wid: str, dim: str, version: int, minute: int, dist: Mapping) -> None:
    """A supervisor edit of a not-yet-exposed dim: drop our pending entry and wait out the cooldown before re-planning."""
    st["not_before"] = max(st["not_before"], minute + settings.EXP_SUPERVISOR_COOLDOWN_MINUTES)
    dropped = st["desired"] is not None
    st["desired"] = None
    if st["phase"] == xs.PENDING:
        st["control"] = xs.top_option(dist)
    ctx.store.add_decision(ctx.now, "experiment_supervisor_edit_pending", f"supervisor edit of {dim} at {xs.clock(minute)} "
                           f"(v{version}) while the experiment is {st['phase']}: {'pending proposal dropped, ' if dropped else ''}"
                           f"no entry before {xs.clock(st['not_before'])}", wid,
                           {"dim": dim, "version": version, "dist": dict(dist), "not_before": st["not_before"]})


def _watch(ctx: PlanContext, st: dict, wid: str, dim: str, cfg: tuple[int, dict]) -> dict:
    """Applied detection and supervisor-edit detection from the mirrored config versions."""
    rows = ctx.store.conn.execute("SELECT version, actor, ts_minute, body FROM config_versions WHERE workflow_id=?"
                                  " AND version>? ORDER BY version", (wid, st["seen_version"])).fetchall()
    for version, actor, ts, body in rows:
        if not body:
            if ts is not None and ctx.now - ts < UNMIRRORED_GRACE_MINUTES:
                break  # mirrored soon: do not look past it, a supervisor edit may hide in it
            ctx.store.add_decision(ctx.now, "experiment_watch_skipped", f"{dim}: config v{version} has no mirrored body "
                                   f"after {UNMIRRORED_GRACE_MINUTES} min; it cannot be examined for a supervisor edit", wid,
                                   {"dim": dim, "version": version})
            st["seen_version"] = version
            continue
        body_cfg = json.loads(body)
        dist, over = xs.live_dist(body_cfg, dim), xs.cohort_overrides(body_cfg, dim)
        minute = ts if ts is not None else ctx.now
        if actor != "supervisor" and st["desired"] and xs.desired_matches(body_cfg, dim, st["desired"]):
            _on_applied(ctx, st, wid, dim, minute)  # our change landed: later versions are judged against it
            st["live"], st["live_cohorts"], st["seen_version"] = dist, over, version
            continue
        exposed = st["phase"] in (xs.RUNNING, xs.PROMOTED)
        edited = not xs.same_dist(dist, st["live"]) or not xs.same_overrides(over, st.get("live_cohorts") or {})
        if actor == "supervisor" and exposed and edited:
            return _interrupt(ctx, st, wid, dim, cfg, version, minute, dist)
        if actor == "supervisor" and edited:
            _pause_for_edit(ctx, st, wid, dim, version, minute, dist)
        st["live"], st["live_cohorts"], st["seen_version"] = dist, over, version
    live_dist = xs.live_dist(cfg[1], dim)
    if st["desired"] and xs.desired_matches(cfg[1], dim, st["desired"]):
        _on_applied(ctx, st, wid, dim, _version_row(ctx.store, wid, cfg[0])[1] or ctx.now)
    elif st["phase"] == xs.PENDING and xs.top_option(live_dist) != st["control"]:
        if st["desired"] or st["history"]:  # production changed under a pending proposal: re-plan from the new control
            st["note"] = (f"The live {dim} control is now {xs.top_option(live_dist)} (changed at config v{cfg[0]}); "
                          f"treating it as the control. ")
        st["control"], st["live"], st["desired"] = xs.top_option(live_dist), live_dist, None
    return st


def _reconcile(ctx: PlanContext, st: dict, wid: str, dim: str, cfg: tuple[int, dict]) -> None:
    """Re-raise a change that never landed (the live config is the truth; see exp_reconcile)."""
    lost = exp_reconcile.lost_change(st, dim, xs.live_dist(cfg[1], dim))
    if lost is None:
        return
    kind = "experiment_harm_stop" if lost["harm"] else "experiment_update"
    _set_desired(st, lost["weights"], kind, lost["summary"], lost["rationale"], severe=lost["severe"],
                 then=xs.RUNNING if st["shares"] else xs.CLOSED, prev=lost["prev"],
                 extra={"ramp": lost["ramp"], "drop": lost["drop"], "reconciled": True},
                 impact=1.0 if lost["severe"] else None)
    ctx.store.add_decision(ctx.now, "experiment_reconcile", lost["summary"], wid,
                           {"dim": dim, "live": st["live"], "weights": lost["weights"], "drop": lost["drop"], "ramp": lost["ramp"]})


# ------------------------------------------------------------------ looks
def _groups(ctx: PlanContext, st: dict, wid: str, cfg: tuple[int, dict]) -> list[dict]:
    sig = xs.cohort_signature(cfg[1].get("cohorts") or [])
    if st["groups"] is None or st["groups"].get("sig") != sig:
        vocab = FeatureVocab.load(ctx.store.get_meta(VOCAB_META_KEY))
        groups = xs.analysis_groups(ctx.store, vocab, wid, cfg[1].get("cohorts") or [], ctx.now)
        old = {g["name"]: g for g in (st["groups"] or {}).get("groups", [])}
        st["groups"] = {"sig": sig, "groups": groups}
        for g in groups:
            before = old.get(g["name"])
            if before is not None and (before["cohorts"], before["cells"]) != (g["cohorts"], g["cells"]):
                st["scopes"].pop(g["name"], None)  # a redefined group is a new scope: no inherited looks / arm_t
            st["scopes"].setdefault(g["name"], {"arm_t": {}, "looks": 0})
        ctx.store.add_decision(ctx.now, "experiment_groups", f"pre-registered analysis groups: "
                               f"{[(g['name'], g['cohorts'], g['share']) for g in groups] or 'none'}", wid,
                               {"groups": [{k: v for k, v in g.items() if k != "cells"} for g in groups]})
    return st["groups"]["groups"]


def _arm_cells(ctx: PlanContext, st: dict, wid: str, dim: str, base=None):
    """Cell filter for the arm contrast: cells of cohorts with their OWN allocation for the dim are not served by the
    experiment's default split (control-only calls would confound control), so they are left out.

    Encoded for ``exp_data``: ``None`` (all cells) becomes negative codes ``-1 - cell`` ("all but these"); an explicit
    cell list (analysis group) just loses those cells.
    """
    cfg = latest_config(ctx.store, wid)
    if cfg is None or not xs.cohort_overrides(cfg[1], dim):
        return base
    key = xs.cohort_signature(cfg[1].get("cohorts") or []) + json.dumps(sorted(xs.cohort_overrides(cfg[1], dim)))
    cache = st.get("override_cells")
    if not cache or cache["key"] != key:
        vocab = FeatureVocab.load(ctx.store.get_meta(VOCAB_META_KEY))
        cache = st["override_cells"] = {"key": key, "cells": xs.override_cells(ctx.store, vocab, wid, cfg[1], dim)}
    excluded = set(cache["cells"])
    if not excluded:
        return base
    return [-1 - c for c in sorted(excluded)] if base is None else sorted(set(base) - excluded)


def _start(ctx: PlanContext, st: dict, wid: str, dim: str) -> int | None:
    if st["start_minute"] is None:
        since = max((st.get("applied") or {}).get("minute", 0), st.get("data_since", 0))
        st["start_minute"] = exp_data.first_exposure_minute(ctx.store, wid, dim, st["family"], since)
    return st["start_minute"]


def _scope_look(ctx, st, wid, dim, wf, name, alpha, cells, active, exposure_days, start, report: Mapping | None = None):
    data = exp_data.load_arms(ctx.store, wid, dim, start, ctx.now, cells)
    sc = st["scopes"][name]
    out = er.daily_look(data, st["control"], active, len(st["family"]), alpha, arm_t=sc["arm_t"],
                        looks_taken=sc["looks"], ramped=st["ramped"], exposure_days=exposure_days, max_drop=_guard(wf))
    if out.took_look:
        sc["looks"], sc["arm_t"] = out.look_index, out.arm_t
    ctx.store.add_decision(ctx.now, "experiment_look", f"{dim} {name}: " + "; ".join(
        f"{a} lift {r['lift']:+.1%} z {r['z']:.2f}" + (f" bound {r['boundary']:.2f}" if r.get("boundary") else "")
        for a, r in sorted(out.records.items()) if r["ok"]) + (f" [look {out.look_index}]" if out.took_look else ""), wid,
        {"dim": dim, "scope": name, "control": st["control"], "alpha": alpha, "took_look": out.took_look,
         "look_index": out.look_index, "records": out.records, "drop": out.drop, "ramp": out.ramp,
         "winner": out.winner, "family": st["family"], "exposure_days": round(exposure_days, 2), **(report or {})})
    return out


def _daily_look(ctx: PlanContext, st: dict, wid: str, dim: str, wf: Mapping, cfg: tuple[int, dict]) -> None:
    start = _start(ctx, st, wid, dim)
    st["last_look_day"] = ctx.now // DAY
    snap = _snapshot(st)
    active = sorted(a for a, v in st["arms"].items() if v["state"] == "active")
    if start is None or not active:
        return
    days = (ctx.now - start) / DAY
    report = {"interactions": exp_interactions.safe_table(ctx.store, wid, start, ctx.now)}  # report-only, never read below
    main = _scope_look(ctx, st, wid, dim, wf, "all", settings.EXP_ALPHA_WORKFLOW, _arm_cells(ctx, st, wid, dim), active, days, start,
                       report)
    winner, scope, rec, cells = main.winner, "workflow", main.records.get(main.winner or ""), None
    for g in _groups(ctx, st, wid, cfg):
        out = _scope_look(ctx, st, wid, dim, wf, g["name"], settings.EXP_ALPHA_GROUP, _arm_cells(ctx, st, wid, dim, g["cells"]),
                          active, days, start)
        if winner is None and out.winner:
            winner, scope, rec, cells = out.winner, f"analysis group {g['name']} ({'+'.join(g['cohorts'])})", out.records[out.winner], g
    if winner:
        _promote(ctx, st, wid, dim, winner, scope, rec, cells, snap)
        return
    ended = st["scopes"]["all"]["looks"] >= len(settings.EXP_LOOK_FRACTIONS)
    if ended and not st.get("exhausted"):  # no winner after the last planned look: stop learning, do not revert
        st["exhausted"] = True
        ctx.store.add_decision(ctx.now, "experiment_exhausted", f"{dim}: all {len(settings.EXP_LOOK_FRACTIONS)} planned looks "
                               "used without a winner; no promotion will be made. A surviving challenger stays only while its "
                               "point estimate is >= control; otherwise it reverts (when that is not expected-harmful)", wid, {"dim": dim, "shares": st["shares"]})
    if ended:  # a surviving challenger needs a non-negative point estimate; a negative one reverts (if not harmful)
        reverts = [a for a in er.end_of_experiment_reverts(main.records, st["shares"], st["control"]) if a not in main.drop]
        if reverts:
            main.drop.extend(reverts)
            ctx.store.add_decision(ctx.now, "experiment_revert", f"{dim}: after the last look {reverts} have a point estimate "
                                   "below control and reverting them is not expected-harmful on the observed arm rates: "
                                   "back to control in the next bundle", wid, {"dim": dim, "arms": reverts})
    shares = exp_alloc.next_shares(st["shares"], main.ramp, main.drop, [] if ended else st.get("untested", []),
                                   st.get("incumbents") or None)
    for a in main.drop:
        st["arms"][a]["state"] = "dropped"
    st["ramped"] = sorted(set(st["ramped"]) | set(main.ramp))
    for a in shares:
        st["arms"].setdefault(a, {"state": "active"})
        if a not in st["family"]:
            st["family"] = sorted(st["family"] + [a])
    st["untested"] = [o for o in st.get("untested", []) if o not in shares]
    if not shares:
        _conclude(ctx, st, wid, dim, "no challenger left")
    elif shares != st["shares"]:
        weights = exp_alloc.weights(st["control"], shares)
        summary, rationale = prop.update(dim, weights, main.ramp, main.drop, main.records, st["note"])
        _set_desired(st, weights, "experiment_update", summary, rationale, prev=snap,
                     extra={"records": main.records, "ramp": list(main.ramp), "drop": list(main.drop)})
    st["shares"] = shares


def _conclude(ctx, st, wid, dim, why: str) -> None:
    for a, v in st["arms"].items():
        if v["state"] == "active":
            v["state"] = "dropped"
    st["shares"] = {}
    summary = f"End {dim} experiment: {why}; all traffic back to control {st['control']}"
    _set_desired(st, {st["control"]: 1.0}, "experiment_conclude", summary, summary, then=xs.CLOSED)
    ctx.store.add_decision(ctx.now, "experiment_conclude", summary, wid, {"dim": dim, "control": st["control"]})


def _promote(ctx, st, wid, dim, winner, scope, rec, group, snap) -> None:
    """Stage 1: winner 50% / old control 50% (concurrent contrast, full power for the decay test)."""
    st["pre_promo"] = {"shares": snap["shares"], "arms": snap["arms"]}
    stage1 = exp_alloc.promotion_weights(winner, st["control"], 1)
    weights, cohort_weights = stage1, {}
    if group:
        cohort_weights = {cid: stage1 for cid in group["cohorts"]}
        weights = {st["control"]: 1.0}  # challengers elsewhere have no evidence: back to control
    summary, rationale = prop.promotion(dim, scope, winner, st["control"], rec, stage1)
    st["promo"] = {"winner": winner, "old_control": st["control"], "scope": scope, "cells": group["cells"] if group else None,
                   "cohorts": group["cohorts"] if group else None, "decided_minute": ctx.now, "stage": 0}
    for a, v in st["arms"].items():
        v["state"] = "winner" if a == winner else ("dropped" if v["state"] == "active" else v["state"])
    st["shares"] = {}
    _set_desired(st, weights, "experiment_promote", summary, rationale, cohort_weights=cohort_weights, then=xs.PROMOTED,
                 extra={"record": rec, "scope": scope}, prev=snap)
    ctx.store.add_decision(ctx.now, "experiment_promote", summary, wid, {"dim": dim, "record": rec, "scope": scope, "stage": 1})


def _harm_check(ctx, st, wid, dim, wf, cfg) -> None:
    """Severe harm-stop. Its weights are the LIVE split minus the stopped arms (their share to control): ``st["shares"]``
    may already hold a planned, not-yet-approved ramp, and a harm-stop (possibly auto-applied) must not carry it live."""
    start = _start(ctx, st, wid, dim)
    st["last_check"] = ctx.now
    active = sorted(a for a, v in st["arms"].items() if v["state"] == "active")
    if start is None or not active:
        return
    data = exp_data.load_arms(ctx.store, wid, dim, start, ctx.now, _arm_cells(ctx, st, wid, dim))
    rows = er.summarise(data, st["control"], active)
    stops = {a: m for a, m in er.harm_stops(rows).items() if m == "now"}
    if not stops:
        return
    pending = st["desired"]
    if pending and pending["kind"] == "experiment_update" and pending.get("prev"):
        for k in ("arms", "ramped", "untested", "family"):  # the planned update is superseded by the harm-stop
            st[k] = json.loads(json.dumps(pending["prev"][k]))
        ctx.store.add_decision(ctx.now, "experiment_update_superseded", f"{dim}: the pending (unapproved) update is superseded "
                               "by a harm-stop; the next daily look re-plans from the live split", wid,
                               {"dim": dim, "weights": pending["weights"]})
    live = xs.live_dist(cfg[1], dim)
    shares = {a: round(w, 6) for a, w in live.items() if a != st["control"] and a not in stops and w > xs.TOL}
    for a in stops:
        st["arms"][a]["state"] = "harm_stopped"
    st["shares"] = shares
    arm = sorted(stops)[0]
    summary, rationale = prop.harm_stop(dim, arm, rows[arm])
    weights = exp_alloc.weights(st["control"], shares)
    _set_desired(st, weights, "experiment_harm_stop", summary, rationale, severe=True,
                 then=xs.RUNNING if shares else xs.CLOSED, extra={"records": rows}, impact=1.0)
    ctx.store.add_decision(ctx.now, "experiment_harm_stop", summary, wid, {"dim": dim, "records": rows, "stopped": sorted(stops)})


# ------------------------------------------------------------------ promoted: decay, rollback, stage 2
def _decay_status(ctx, st, wid, dim, promo, since, stage_min, cells) -> tuple[dict, bool, str]:
    """CUSUM (secondary), holdout view, and the sequential winner-vs-control test (primary) at this tick."""
    ref, pooled = exp_decay.reference_rates(ctx.store, wid, dim, promo["old_control"], since // DAY - 1, cells)
    blocks = exp_decay.winner_blocks(ctx.store, wid, dim, promo["winner"], since, ref, pooled, cells)
    level = exp_decay.pre_promotion_level(ctx.store, wid, dim, promo["winner"], st["start_minute"] or 0, since, ref, pooled, cells)
    cus = exp_decay.cusum(blocks, level)
    held = exp_decay.holdout_view(ctx.store, wid, dim, promo["winner"], promo["old_control"], stage_min, ctx.now, cells)
    seq_state = promo.setdefault("seq", {"start": stage_min, "ts": []})
    data = exp_data.load_arms(ctx.store, wid, dim, seq_state["start"], ctx.now, cells)
    c = er.pair_contrast(data.get(promo["winner"], er.EMPTY), data.get(promo["old_control"], er.EMPTY))
    seq = exp_decay.sequential_alarm(c, seq_state["ts"])
    roll, why = exp_decay.decide(cus, held, seq)
    seq_state["ts"] = seq["ts"]
    if why.startswith("hold") and note_once(ctx.store, f"rollback_held:{wid}:{dim}", ctx.now):
        ctx.store.add_decision(ctx.now, "rollback_held", f"{dim} rollback held: {why}", wid, {"cusum": cus, "holdout": held})
    if seq["epoch_done"] and not roll:  # alpha for this epoch is spent: a fresh epoch starts now
        promo["seq"] = {"start": ctx.now, "ts": []}
    return {"cusum": cus, "holdout": held, "sequential": {k: v for k, v in seq.items() if k != "ts"}, "rollback": roll}, roll, why


def _plan_stage2(ctx, st, wid, dim, promo, stage_min, cells) -> None:
    data = exp_data.load_arms(ctx.store, wid, dim, stage_min, ctx.now, cells)
    w, h = data.get(promo["winner"], er.EMPTY), data.get(promo["old_control"], er.EMPTY)
    ok, why = exp_decay.stage2_ready(ctx.now - stage_min, er.pair_contrast(w, h), er.pair_contrast(w, h, recent=True))
    if not ok:
        return
    weights = exp_alloc.promotion_weights(promo["winner"], promo["old_control"], 2)
    cohort_weights = {cid: weights for cid in (promo.get("cohorts") or [])}
    default = {promo["old_control"]: 1.0} if cohort_weights else weights
    summary, rationale = prop.stage2(dim, promo["winner"], promo["old_control"], why, weights)
    _set_desired(st, default, "experiment_stage2", summary, rationale, cohort_weights=cohort_weights, then=xs.PROMOTED,
                 extra={"why": why}, impact=0.3)
    ctx.store.add_decision(ctx.now, "experiment_stage2", summary, wid, {"dim": dim, "why": why})


def _decay_check(ctx, st, wid, dim) -> None:
    st["last_check"] = ctx.now
    promo = st.get("promo") or {}
    since = promo.get("minute")
    pending = st["desired"]
    if since is None or (pending and pending["kind"] != "experiment_stage2"):
        return
    cells = promo.get("cells")
    stage_min = promo.get("stage_minute", since)
    status, roll, why = _decay_status(ctx, st, wid, dim, promo, since, stage_min, cells)
    cus, held = status["cusum"], status["holdout"]
    if json.dumps(status, sort_keys=True) != json.dumps(st.get("decay_status"), sort_keys=True) and (
            roll or ctx.now // DAY != st.get("decay_note_day")):
        st["decay_note_day"] = ctx.now // DAY
        ctx.store.add_decision(ctx.now, "experiment_decay_check", f"{dim} {promo['winner']} (stage {promo.get('stage')}): "
                               f"sequential z {status['sequential']['z']} (info {status['sequential']['t']}), CUSUM S {cus['S']}, "
                               f"holdout recent lift {held.get('recent_lift')}", wid, status)
    st["decay_status"] = status
    if roll:
        summary, rationale = prop.rollback(dim, promo["winner"], promo["old_control"], why)
        cohort_weights = {cid: {promo["old_control"]: 1.0} for cid in (promo.get("cohorts") or [])}
        _set_desired(st, {promo["old_control"]: 1.0}, "experiment_rollback", summary, rationale, severe=True,
                     cohort_weights=cohort_weights, then=xs.CLOSED, extra=status, impact=0.5)
        ctx.store.add_decision(ctx.now, "experiment_rollback", summary, wid, status)
    elif promo.get("stage") == 1 and not pending:
        _plan_stage2(ctx, st, wid, dim, promo, stage_min, cells)


# ------------------------------------------------------------------ detector / producer
def _step(ctx: PlanContext, wid: str, dim: str, wf: Mapping) -> None:
    cfg = latest_config(ctx.store, wid)
    if cfg is None:
        return
    st = xs.load(ctx.store, wid, dim) or _new_state(wf, dim, cfg)
    before = json.dumps(st, sort_keys=True)
    st = _watch(ctx, st, wid, dim, cfg)
    _reconcile(ctx, st, wid, dim, cfg)
    pending_promotion = bool(st["desired"] and st["desired"]["kind"] == "experiment_promote")
    if st["phase"] == xs.PENDING:
        if ctx.now >= st["not_before"] and not st["desired"]:
            _plan_entry(ctx, st, wid, dim, cfg)
    elif st["phase"] == xs.RUNNING and not pending_promotion:
        if ctx.now % DAY >= _look_minute(wf, exp_entry.sim_params(ctx)[0]) and st["last_look_day"] < ctx.now // DAY:
            _daily_look(ctx, st, wid, dim, wf, cfg)
        elif ctx.now - st["last_check"] >= settings.EXP_HARM_CHECK_MINUTES:
            _harm_check(ctx, st, wid, dim, wf, cfg)
    elif st["phase"] == xs.PROMOTED and ctx.now - st["last_check"] >= settings.EXP_HARM_CHECK_MINUTES:
        _decay_check(ctx, st, wid, dim)
    if json.dumps(st, sort_keys=True) != before:
        xs.save(ctx.store, wid, dim, st)


def experiments_detector(ctx: PlanContext) -> None:
    if not settings.ENABLE_EXPERIMENTS:
        return
    for wid in sorted(ctx.workflows):
        try:
            for dim in xs.DIMS:
                if xs.eligible(ctx.workflows[wid], dim):
                    _step(ctx, wid, dim, ctx.workflows[wid])
        except Exception as exc:  # one workflow's failure must not starve the others
            log.exception("experiments detector failed for %s", wid)
            once_alert(ctx.store, ctx.now, "error", "experiments_workflow_error",
                       f"experiments detector failed: {type(exc).__name__}: {exc}", wid)


def _auto_limit(wf: Mapping, dim: str) -> float | None:
    pol = wf["governance"]["auto_apply"]
    return float(pol["max_shift"]) if _auto_ok({"governance": {"auto_apply": pol}}, dim) else None


def stepped_patch(cfg: Mapping, dim: str, desired: Mapping, limit: float | None) -> dict:
    """The patch towards ``desired``, one auto-apply step (TV <= max_shift x safety per allocation key) at a time.

    A rollback from a 50/50 stage with a 0.2 max_shift therefore walks back over several hourly steps; each tick
    re-derives the next step from the live config until the target is live.
    """
    patch = xs.desired_patch(dim, desired)
    if limit is None:
        return patch
    for key, alloc in patch["allocations"].items():
        cur = xs.live_dist(cfg, dim, key)
        alloc[dim] = did_plan.plan_step(cur, alloc[dim], limit, {})
    return patch


def _negligible_step(cfg: Mapping, dim: str, full: Mapping, step: Mapping) -> bool:
    """True when the auto-bounded step moves less than DID_MIN_TV although the target is further away."""
    moved = remaining = 0.0
    for key, alloc in full["allocations"].items():
        cur = xs.live_dist(cfg, dim, key)
        moved = max(moved, did_plan.tv(cur, step["allocations"][key][dim]))
        remaining = max(remaining, did_plan.tv(cur, alloc[dim]))
    return moved < settings.DID_MIN_TV and remaining > moved + 1e-9


def experiments_producer(ctx: PlanContext, wid: str) -> list[DesiredChange]:
    if not settings.ENABLE_EXPERIMENTS:
        return []
    wf, out = ctx.workflows[wid], []
    cfg = latest_config(ctx.store, wid)
    for dim in xs.DIMS:
        if not xs.eligible(wf, dim):
            continue
        d = (xs.load(ctx.store, wid, dim) or {}).get("desired")
        if not d:
            continue
        auto = d["severe"] and _auto_ok(wf, dim)  # entry/ramp/drop/promote are customer-facing: always approval
        evidence = {"rationale": d["rationale"], "dry_run_extra": {"experiment": {"dim": dim, "kind": d["kind"], **d["evidence"]}}}
        if d["impact"] is not None:
            evidence["impact_override"] = d["impact"]
        patch = xs.desired_patch(dim, d)
        if auto and cfg:
            step = stepped_patch(cfg[1], dim, d, _auto_limit(wf, dim))
            if _negligible_step(cfg[1], dim, patch, step):  # the bounded step would barely move: ask for the whole change
                auto = False
            else:
                patch = step
        out.append(DesiredChange(wid, d["kind"], Priority.SEVERE if d["severe"] else Priority.ROUTINE,
                                 patch, ROUTE_AUTO if auto else ROUTE_APPROVAL, evidence, d["summary"]))
    return out


# ------------------------------------------------------------------ apply-time veto
PROMOTE_KINDS = ("experiment_promote", "experiment_stage2")
RISK_KINDS = ("experiment_harm_stop", "experiment_rollback")


def _recent_contrast(ctx, st, wid, dim, promo):
    lo = max(st.get("start_minute") or 0, ctx.now - settings.EXP_RECENT_MINUTES)
    data = exp_data.load_arms(ctx.store, wid, dim, lo, ctx.now, promo.get("cells"))
    return er.pair_contrast(data.get(promo["winner"], er.EMPTY), data.get(promo["old_control"], er.EMPTY), recent=True)


def _veto_promotion(ctx, st, wid, dim, d) -> str | None:
    """Both promotion stages need the recent window's one-sided 80% lower bound of the lift above 0."""
    promo = st["promo"]
    ok, txt = exp_decay.veto_lower_bound(_recent_contrast(ctx, st, wid, dim, promo))
    if ok:
        return None
    return f"{dim}: before applying {d['kind']}, the winner {promo['winner']}'s {txt}: it may be decaying"


def _veto_update(ctx, st, wid, dim, d) -> str | None:
    """Ramp: no cumulative z at the drop bound and no bad recent window; drop: still at the drop bound."""
    ev = d["evidence"]
    ramp, drop = ev.get("ramp") or [], ev.get("drop") or []
    if not (ramp or drop) or st.get("start_minute") is None:
        return None
    data = exp_data.load_arms(ctx.store, wid, dim, st["start_minute"], ctx.now, _arm_cells(ctx, st, wid, dim))
    rows = er.summarise(data, st["control"], list(ramp) + list(drop))
    bad = []
    for a in ramp:
        if er.ramp_vetoed(rows[a]):
            bad.append(f"ramp {a} (z {rows[a]['z']:.2f}, recent z {rows[a].get('recent_z', float('nan')):.2f})")
    for a in drop:
        if er.drop_vetoed(rows[a], a in st["ramped"]):
            bad.append(f"drop {a} (z {rows[a]['z']:.2f}, recent z {rows[a].get('recent_z', float('nan')):.2f}: no longer at the drop bound)")
    return f"{dim}: apply-time re-check of the look failed: " + "; ".join(bad) if bad else None


def _withhold(ctx, st, wid, dim, d, reason: str) -> None:
    """Never call /apply; restore the pre-decision state so the next look re-plans from current data."""
    if d["kind"] == "experiment_stage2":
        st["promo"]["stage2_withheld"] = ctx.now
    elif d.get("prev"):
        for k, v in d["prev"].items():
            st[k] = v
        if d["kind"] == "experiment_promote":
            st.pop("promo", None)
            st.setdefault("withdrawn", []).append({"winner": d["evidence"].get("record", {}).get("winner"), "minute": ctx.now})
    st["desired"] = None
    xs.save(ctx.store, wid, dim, st)
    ctx.store.add_decision(ctx.now, "experiment_apply_withheld", reason, wid, {"dim": dim, "kind": d["kind"]})


def experiments_veto(ctx: PlanContext, wid: str, kind: str) -> str | None:
    """Apply-time re-check of an approved ramp, drop, or promotion stage: the evidence is hours old when applied.

    Withheld means /apply is never called (so no lapse is logged); the decision is recorded and the experiment
    re-plans at its next look. Risk-reducing harm-stops and rollbacks are never withheld.
    """
    if not settings.ENABLE_EXPERIMENTS or not kind.startswith("experiment"):
        return None
    reasons, wanted = [], False
    for dim in xs.DIMS:
        st = xs.load(ctx.store, wid, dim)
        d = st.get("desired") if st else None
        if not d:
            continue
        reason = None
        if d["kind"] in PROMOTE_KINDS and st.get("promo"):
            reason = _veto_promotion(ctx, st, wid, dim, d)
        elif d["kind"] == "experiment_update":
            reason = _veto_update(ctx, st, wid, dim, d)
        if reason and kind in RISK_KINDS:  # approved as a bundle with a risk-reducing change: never withhold that
            ctx.store.add_decision(ctx.now, "experiment_stale_part_applied", f"{reason}; applied anyway because the bundle "
                                   f"carries {kind}; the next look re-evaluates from the live config", wid, {"dim": dim, "kind": d["kind"]})
        elif reason:
            _withhold(ctx, st, wid, dim, d, reason)
            reasons.append(reason)
        elif d["kind"] == kind:
            wanted = True
    if reasons:
        return "; ".join(reasons)
    if kind in PROMOTE_KINDS and not wanted:
        return f"{kind} is no longer wanted (superseded by a newer experiment decision)"
    return None


def register(registry=DEFAULT_REGISTRY) -> None:
    registry.register_detector(experiments_detector)
    registry.register_producer(experiments_producer)
    registry.register_veto(experiments_veto)
