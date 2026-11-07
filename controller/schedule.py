"""C2 + C4a driver: cohort partition, per-cohort call-time tables, Day-0 bundle, refresh, verify, rollback.

Detector (each tick, before the planner; heavy work once per sim day and per cohort set):
  1. learn a partition from history (``cohorts.select_partition``), or use the cohorts already live;
  2. fit hour multipliers per cohort (``schedule_model``), build the tables, estimate the pickup lift and the
     engagement-composition shift (guardrail dry run: BLOCK if the estimated drop exceeds the guardrail);
  3. after an apply, compare hour-mix-adjusted pickup/engagement before vs after; flag a rollback if it fell.
Producer (pure function of the persisted state): the Day-0 bundle (cohorts + tables, always an approval because
``cohorts`` is never auto-appliable), later refreshes (auto-apply where ``schedule`` is allowed), the rollback.
Supervisor edits are never overwritten: a refresh only replaces tables we applied ourselves.
Everything is behind ``settings.ENABLE_SCHEDULE``.
"""
from __future__ import annotations

import json
import logging
from typing import Mapping

from . import cube, schedule_model as sm, settings
from .cohorts import VOCAB_META_KEY, FeatureVocab, assign_cohort, select_partition
from .desired import DEFAULT_REGISTRY, ROUTE_APPROVAL, ROUTE_AUTO, DesiredChange, PlanContext, Priority
from .detectors.common import latest_config, load_state, once_alert, save_state

log = logging.getLogger(__name__)
DETECTOR = "schedule"
DAY = settings.MINUTES_PER_DAY
MONITORING, VERIFIED, ROLLBACK, ROLLED_BACK = "monitoring", "verified", "rollback", "rolled_back"
PARTITION_HISTORY = 7  # partition log entries kept in the persisted state
_SCHEDULE_KINDS = ("schedule_bundle", "schedule_refresh")
_FLOOR = "(cp.ts_minute - (((cp.ts_minute % 1440) + 1440) % 1440)) / 1440"
STRATA_SQL = ("SELECT l.cell, c.hour, c.attempt_no, c.day - " + _FLOOR + " AS cd, COUNT(*), SUM(c.connected),"
              " SUM(c.engaged) FROM calls c JOIN leads l ON l.lid=c.lid JOIN campaigns cp ON"
              " cp.campaign_id=l.campaign_id WHERE c.workflow_id=? AND c.day>=? AND c.day<=?"
              " GROUP BY l.cell, c.hour, c.attempt_no, cd")


def strata_rows(store, wid: str, day_lo: int, today: int) -> list[tuple]:
    """``STRATA_SQL`` over [day_lo, today], assembled from per-day parts (completed days cached).

    Equal to the single query: the group key contains no day-of-call component that spans days except ``cd``,
    which is part of the key, so per-day groups just add up. Sorted by key like SQLite's GROUP BY output.
    """
    merged: dict[tuple, list] = {}
    for d in range(day_lo, today + 1):
        if d < today:
            rows = _strata_cached(store, wid, d)
        else:
            rows = [list(r) for r in store.conn.execute(STRATA_SQL, (wid, d, d))]
        for cell, hour, att, cd, n, c, e in rows:
            acc = merged.setdefault((cell, hour, att, cd), [0, 0, 0])
            acc[0] += n
            acc[1] += c
            acc[2] += e or 0
    return [(*k, *v) for k, v in sorted(merged.items())]


def _strata_cached(store, wid: str, day: int) -> list:
    row = store.conn.execute("SELECT payload FROM exp_day_cache WHERE workflow_id=? AND day=? AND col='strata'",
                             (wid, day)).fetchone()
    if row is not None:
        return json.loads(row[0])
    rows = [list(r) for r in store.conn.execute(STRATA_SQL, (wid, day, day))]
    store.conn.execute("INSERT OR REPLACE INTO exp_day_cache(workflow_id, day, col, cells, payload) VALUES (?,?,?,?,?)",
                       (wid, day, "strata", "-", json.dumps(rows)))
    return rows


def clock(minute: int) -> str:
    return f"Day {minute // DAY} {minute % DAY // 60:02d}:{minute % 60:02d}"


def _guard(wf: Mapping, key: str) -> float:
    guard = (wf.get("objective") or {}).get("guardrail") or {}
    return float(guard.get(key, settings.ENGAGEMENT_VERIFY_MAX_DROP))


def _auto_ok(wf: Mapping) -> bool:
    pol = wf["governance"]["auto_apply"]
    return bool(pol.get("enabled")) and "schedule" in pol.get("allowed_dims", [])


def _vocab(store) -> FeatureVocab:
    return FeatureVocab.load(store.get_meta(VOCAB_META_KEY))


def _ready(store, wid: str, now: int) -> bool:
    """History present, or (cold start) one full day of live data."""
    hist = store.conn.execute("SELECT COUNT(DISTINCT day) FROM cube WHERE workflow_id=? AND day<0", (wid,)).fetchone()[0]
    if hist >= settings.BASELINE_MIN_DAYS:
        return True
    live = store.conn.execute("SELECT COUNT(DISTINCT day) FROM cube WHERE workflow_id=? AND day<?", (wid, now // DAY)).fetchone()[0]
    return live >= settings.SCHEDULE_MIN_HISTORY_DAYS_COLD


def _tables_of(cfg: Mapping) -> dict:
    return {k: v for k, v in (cfg.get("schedule", {}).get("hour_weights") or {}).items()}


# ------------------------------------------------------------------ partition + plan
def _learn_partition(ctx: PlanContext, wid: str):
    hours = list(range(*ctx.workflows[wid]["dialling_window"]))
    rows = [(r.key[0], r.key[1], r.dials, r.connects) for r in cube.rollup(ctx.store, wid, by=("hour", "cell"))]
    return select_partition(rows, _vocab(ctx.store), hours)


def _build_partition(ctx: PlanContext, wid: str, st: dict) -> None:
    """(Re)learn the partition from the data so far; a changed result replaces the pending one."""
    part = _learn_partition(ctx, wid)
    new = part.as_dict() if part else None
    st["partition_day"] = ctx.now // DAY
    if st.get("partitions") and new == st.get("partition"):
        return
    st["partition"] = new
    log_ = st.setdefault("partitions", [])
    log_.append({"day": ctx.now // DAY, "minute": ctx.now, "partition": new})
    del log_[:-PARTITION_HISTORY]
    msg = (f"cohort partition by {'+'.join(part.features)}: {len(part.cohorts)} cohorts (n_min {part.n_min} dials,"
           f" interaction z {part.score:.1f})" if part else "no partition beats the pooled hour profile")
    ctx.store.add_decision(ctx.now, "cohort_partition", msg, wid, new or {"candidates": "none"})


def _plan(ctx: PlanContext, wid: str, st: dict, cfg: tuple[int, dict]) -> None:
    wf, store = ctx.workflows[wid], ctx.store
    live = cfg[1].get("cohorts") or []
    use = live or (st["partition"] or {}).get("cohorts") or []
    key = json.dumps([ctx.now // DAY, use], sort_keys=True)
    if st.get("plan_key") == key:
        return
    st["plan_key"] = key
    if not use:
        st["plan"] = None
        return
    vocab, day = _vocab(store), ctx.now // DAY
    hours = list(range(*wf["dialling_window"]))
    rows = strata_rows(store, wid, day - settings.SCHEDULE_HISTORY_DAYS, day)
    cache: dict[int, str] = {}

    def assign(cell: int) -> str:
        if cell not in cache:
            cache[cell] = assign_cohort(use, vocab.decode(cell))
        return cache[cell]

    if sum(r[4] for r in rows) < settings.COHORT_MIN_DIALS_PER_HOUR * len(hours):
        st["plan"] = None
        return
    model = sm.fit(rows, assign, hours)
    ids = [c["id"] for c in use]
    tabs = sm.tables(model, ids)
    cur = _tables_of(cfg[1])
    out = sm.outcome(model, tabs, cur)
    max_drop = _guard(wf, "engagement_rate_max_drop")
    blocked = out["engagement_drop"] > max_drop
    st["plan"] = {"day": day, "cohorts": use, "tables": tabs, "estimate": out, "blocked": blocked,
                  "foreign_cohorts": bool(live) and live != (st["partition"] or {}).get("cohorts"),
                  "max_drop": max_drop, "tau2": model.detail["tau2"], "n_dials": sum(r[4] for r in rows)}
    verdict = ("BLOCKED by the engagement guardrail" if blocked else
               "material" if out["rel_lift"] >= (settings.SCHEDULE_MIN_LIFT if live else settings.SCHEDULE_BUNDLE_MIN_LIFT)
               else "not material")
    store.add_decision(ctx.now, "schedule_plan",
                       f"per-cohort call times: est. pickup {out['pickup_old']:.3f} -> {out['pickup_new']:.3f}"
                       f" ({out['rel_lift']:+.1%}), est. engagement shift {-out['engagement_drop']:+.4f} "
                       f"(max drop {max_drop}): {verdict}", wid,
                       {"estimate": out, "cohorts": ids, "tau2": model.detail["tau2"], "tables": tabs,
                        "raw_multipliers": model.detail["raw"], "pooled": model.detail["pooled"],
                        "n_dials": st["plan"]["n_dials"], "current_tables": cur})
    if blocked:
        store.add_alert(ctx.now, "warn", "schedule_blocked", f"engagement-composition shift {out['engagement_drop']:.3f}"
                        f" exceeds the guardrail {max_drop}", wid)


# ------------------------------------------------------------------ apply detection + verification
def _applied_minute(store, wid: str, version: int, now: int) -> int:
    row = store.conn.execute("SELECT ts_minute FROM config_versions WHERE workflow_id=? AND version=?",
                             (wid, version)).fetchone()
    return int(row[0]) if row and row[0] is not None else now


def _our_proposal(store, wid: str, version: int) -> dict | None:
    """Body of the schedule proposal that produced config ``version`` (None: someone else's change)."""
    row = store.conn.execute(
        "SELECT p.body FROM config_versions c JOIN proposals p ON p.proposal_id=c.proposal_id"
        " WHERE c.workflow_id=? AND c.version=? AND p.kind IN (?,?)", (wid, version, *_SCHEDULE_KINDS)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def _detect_apply(ctx: PlanContext, wid: str, st: dict, cfg: tuple[int, dict]) -> None:
    """Our apply = live tables equal today's plan, or the live version came from our own schedule proposal
    (the plan is refit daily, so an apply a day or more after the filing no longer matches today's plan)."""
    plan, live = st.get("plan"), _tables_of(cfg[1])
    if not live or st.get("applied", {}).get("tables") == live:
        return
    body = None if plan and live == plan["tables"] else _our_proposal(ctx.store, wid, cfg[0])
    if not (plan and live == plan["tables"]) and body is None:
        return
    estimate = ((body or {}).get("request", {}).get("dry_run", {}).get("schedule_guardrail")
                or (plan or {}).get("estimate") or {})
    minute = _applied_minute(ctx.store, wid, cfg[0], ctx.now)
    st["applied"] = {"minute": minute, "version": cfg[0], "tables": live, "status": MONITORING,
                     "estimate": estimate, "prior_tables": _prior_tables(ctx.store, wid, cfg[0])}
    ctx.store.add_decision(ctx.now, "schedule_applied", f"per-cohort call times live since {clock(minute)}"
                           f" (config v{cfg[0]}); verifying hour-mix-adjusted pickup", wid,
                           {"version": cfg[0], "estimate": estimate})


def _period(ctx: PlanContext, wid: str, assign, lo_hour: int, hi_hour: int) -> dict:
    out: dict = {}
    for r in cube.rollup(ctx.store, wid, by=("day", "hour", "cell"), day_min=lo_hour // 24, day_max=hi_hour // 24):
        day, hour, cell = r.key
        if lo_hour <= day * 24 + hour < hi_hour:
            slot = out.setdefault((hour, assign(cell)), [0, 0, 0])
            slot[0] += r.dials
            slot[1] += r.connects
            slot[2] += r.engaged
    return out


def _verify(ctx: PlanContext, wid: str, st: dict, cfg: tuple[int, dict]) -> None:
    ap = st.get("applied")
    if not ap or ap["status"] != MONITORING:
        return
    wf = ctx.workflows[wid]
    vocab, cohorts = _vocab(ctx.store), cfg[1].get("cohorts") or []
    a_hour, now_hour = ap["minute"] // 60 + 1, ctx.now // 60
    after = _period(ctx, wid, lambda c: assign_cohort(cohorts, vocab.decode(c)), a_hour, now_hour + 1)
    if sum(v[0] for v in after.values()) < settings.SCHEDULE_VERIFY_MIN_DIALS:
        return
    before = _period(ctx, wid, lambda c: assign_cohort(cohorts, vocab.decode(c)), a_hour - 72, a_hour)
    res = sm.verify_change(before, after)
    if not res.get("usable"):
        return
    pk, en = res["pickup"], res["engagement"]
    bad_pickup = pk["drop"] >= _guard(wf, "pickup_rate_max_drop") and pk["p_drop"] < settings.SCHEDULE_VERIFY_P
    bad_eng = en["drop"] >= _guard(wf, "engagement_rate_max_drop") and en["p_drop"] < settings.SCHEDULE_VERIFY_P
    done = res["n_after"] >= settings.SCHEDULE_VERIFY_DONE_DIALS
    if bad_pickup or bad_eng:
        ap["status"] = ROLLBACK
        what = "pickup" if bad_pickup else "engagement"
        ctx.store.add_decision(ctx.now, "schedule_rollback", f"hour-mix-adjusted {what} fell beyond the guardrail"
                               " after the call-time change: rolling back (confounded before/after check)", wid, res)
    elif done:
        ap["status"] = VERIFIED
        ctx.store.add_decision(ctx.now, "schedule_verified", f"hour-mix-adjusted pickup {pk['before']:.3f} -> "
                               f"{pk['after_std']:.3f} (raw {res['raw_pickup_before']:.3f} -> {res['raw_pickup_after']:.3f}); "
                               "within the guardrail (no concurrent control: confounded)", wid, res)


def schedule_detector(ctx: PlanContext) -> None:
    if not settings.ENABLE_SCHEDULE:
        return
    for wid in sorted(ctx.workflows):
        try:
            _detect_workflow(ctx, wid)
        except Exception as exc:  # one workflow's failure must not stop the schedule lever for the others
            log.exception("schedule detection failed for %s", wid)
            once_alert(ctx.store, ctx.now, "error", "schedule_workflow_error",
                       f"schedule detector failed: {type(exc).__name__}: {exc}", wid)


def _detect_workflow(ctx: PlanContext, wid: str) -> None:
    cfg = latest_config(ctx.store, wid)
    if cfg is None or not _ready(ctx.store, wid, ctx.now):
        return
    st = load_state(ctx.store, DETECTOR, wid)
    before = json.dumps(st, sort_keys=True)
    if st.get("partition_day") != ctx.now // DAY and not cfg[1].get("cohorts"):
        _build_partition(ctx, wid, st)
    _plan(ctx, wid, st, cfg)
    _detect_apply(ctx, wid, st, cfg)
    _verify(ctx, wid, st, cfg)
    if st.get("applied", {}).get("status") == ROLLBACK and not _tables_of(cfg[1]):
        st["applied"]["status"] = ROLLED_BACK
        st["blocked_until"] = ctx.now + 3 * DAY
    if json.dumps(st, sort_keys=True) != before:
        save_state(ctx.store, DETECTOR, wid, st)


# ------------------------------------------------------------------ producer
def _impact(plan: Mapping) -> float:
    return max(0.0, min(1.0, settings.SCHEDULE_LIFT_HAIRCUT * plan["estimate"]["rel_lift"] / 0.10))


def _summary(plan: Mapping, bundle: bool) -> str:
    e = plan["estimate"]
    what = (f"Define {len(plan['cohorts'])} cohorts and per-cohort call-time tables" if bundle
            else "Refresh per-cohort call-time tables")
    return (f"{what}: est. pickup {e['pickup_old']:.3f} -> {e['pickup_new']:.3f} ({e['rel_lift']:+.1%}), est. engagement "
            f"shift {-e['engagement_drop']:+.3f} (guardrail {plan['max_drop']}); weights only re-rank leads within an hour")


BUSINESS_DEFAULT_ACTOR = "business_default"


def _tables_history(store, wid: str) -> list[tuple[int, str | None, dict]]:
    rows = store.conn.execute("SELECT version, actor, body FROM config_versions WHERE workflow_id=? AND body IS NOT NULL"
                              " ORDER BY version", (wid,)).fetchall()
    return [(v, actor, _tables_of(json.loads(body))) for v, actor, body in rows if body]


def _tables_editor(store, wid: str) -> str | None:
    """Actor of the newest config version that changed the live hour tables (the first version's if none did)."""
    editor, prev = None, None
    for _, actor, tables in _tables_history(store, wid):
        if prev is None or tables != prev:
            editor = actor
        prev = tables
    return editor


def _prior_tables(store, wid: str, version: int) -> dict:
    """The hour tables live just before ``version`` (the baseline a rollback restores)."""
    before = [t for v, _, t in _tables_history(store, wid) if v < version]
    return before[-1] if before else {}


def _owned(st: Mapping, cfg: Mapping, store=None, wid: str | None = None) -> bool:
    """Tables we applied, no tables, or the business default's tables may be refreshed; a supervisor's schedule
    (or anyone's change after ours) is never overwritten."""
    live = _tables_of(cfg)
    if not live or live == (st.get("applied") or {}).get("tables"):
        return True
    return store is not None and _tables_editor(store, wid) in (None, BUSINESS_DEFAULT_ACTOR)


def _rollback_patch(ap: Mapping) -> dict:
    """Drop the tables we applied; where they replaced a business-default table, put that one back."""
    prior = ap.get("prior_tables") or {}
    return {k: prior.get(k) for k in ap["tables"]}


def schedule_producer(ctx: PlanContext, wid: str) -> list[DesiredChange]:
    if not settings.ENABLE_SCHEDULE:
        return []
    cfg = latest_config(ctx.store, wid)
    st = load_state(ctx.store, DETECTOR, wid)
    if cfg is None or not st:
        return []
    wf = ctx.workflows[wid]
    ap = st.get("applied") or {}
    if ap.get("status") == ROLLBACK and _tables_of(cfg[1]) == ap.get("tables"):
        summary = "Roll back per-cohort call times: hour-mix-adjusted pickup/engagement fell beyond the guardrail"
        return [DesiredChange(wid, "schedule_rollback", Priority.SEVERE,
                              {"hour_weights": _rollback_patch(ap)},
                              ROUTE_AUTO if _auto_ok(wf) else ROUTE_APPROVAL,
                              {"rationale": summary, "impact_override": 0.5}, summary)]
    plan = st.get("plan")
    bundle = not cfg[1].get("cohorts")
    floor = settings.SCHEDULE_BUNDLE_MIN_LIFT if bundle else settings.SCHEDULE_MIN_LIFT
    if (not plan or plan["blocked"] or plan["estimate"]["rel_lift"] < floor
            or ctx.now < st.get("blocked_until", 0) or not _owned(st, cfg[1], ctx.store, wid)):
        return []
    evidence = {"rationale": _summary(plan, not cfg[1].get("cohorts")), "impact_override": _impact(plan),
                "dry_run_extra": {"schedule_guardrail": {
                    "method": "recent dials re-weighted by the new tables (capacity per hour fixed, cohort shares "
                              "proportional to volume x weight); engagement of connected leads re-composed",
                    **plan["estimate"], "max_drop": plan["max_drop"], "blocked": plan["blocked"]}}}
    if not cfg[1].get("cohorts"):  # Day-0 bundle: cohorts + tables, always an approval
        patch = {"cohorts": plan["cohorts"], "hour_weights": plan["tables"]}
        return [DesiredChange(wid, "schedule_bundle", Priority.ROUTINE, patch, ROUTE_APPROVAL, evidence,
                              _summary(plan, True))]
    route = ROUTE_AUTO if _auto_ok(wf) else ROUTE_APPROVAL
    return [DesiredChange(wid, "schedule_refresh", Priority.ROUTINE, {"hour_weights": plan["tables"]}, route,
                          evidence, _summary(plan, False))]


def register(registry=DEFAULT_REGISTRY) -> None:
    registry.register_detector(schedule_detector)
    registry.register_producer(schedule_producer)
