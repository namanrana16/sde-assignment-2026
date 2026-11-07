"""Alert rules (C14), evaluated once per tick; no pager integration, rows in ``alerts`` are the output.

PAGE (someone must act now): ingest stalled; an approved change within 30 sim min of its ``apply_by`` and not applied;
credits below the incident reserve; repeated simulator 5xx; any fatigue rejection or 403 (our accounting is wrong);
a cohort self-check mismatch.
DIGEST (read in the morning): judgement rejections; starved experiments; a weak-base DID; unattributed KPI drift.

Dedupe: a *condition* alert (``cond:`` key) fires once when it becomes true and again only after it has cleared; an
*event* alert (``once:`` key) fires once per event. Keys live in ``detector_state`` so a restart does not re-alert.
Every message says what to look at and which command to run.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Mapping

from . import cube, incidents, metrics, settings, view_data
from .audit import fmt_minute
from .store import Store

log = logging.getLogger(__name__)
DAY = settings.MINUTES_PER_DAY
DETECTOR = "alerts"
PAGE, DIGEST = "page", "digest"
APPLY_BY_WARN_MIN = 30
STALL_SIM_MIN = 180  # no event for this long while the same hours had calls the day before
STALL_MIN_EXPECTED = 20  # calls the same window had yesterday, for 'silence' to be abnormal
STALL_WALL_S = 600.0  # one tick's wall lag above this
SERVER_ERRORS_PER_HOUR = 3
STARVE_MIN_CONNECTS = 100
STARVE_AFTER_DAYS = 2
DRIFT_Z = 3.0
DRIFT_MIN_BASE_N = 30
# The approver's own hygiene reasons (sim/world.py approval decision): any of these means OUR request was malformed or
# mis-accounted, so it pages. Everything else (the "judgement call" decline, or no reason) is a digest. Never both.
FATIGUE_REASON = "fatigue"
HYGIENE_REASONS = (FATIGUE_REASON, "must all be non-empty", "must be a non-negative integer", "must be in [0,1]",
                   "prompt changes need evidence", "validation pass rate")
STARVE_MARKER = "eval:starved"
RULE_FAILED_CODE = "observability_rule_failed"
WHY = 'python -m controller why --workflow {wid} --at "{at}"'


@dataclass(frozen=True)
class Alert:
    key: str  # "cond:..." clears when the condition goes away; "once:..." never repeats
    severity: str
    code: str
    workflow_id: str | None
    message: str


def _is_hygiene(payload: str | None) -> bool:
    text = (payload or "").lower()
    return any(w in text for w in HYGIENE_REASONS)


def _rows(store: Store, sql: str, args: tuple = ()) -> list[tuple]:
    return store.conn.execute(sql, args).fetchall()


# ------------------------------------------------------------------ PAGE rules
def _cube_workflows(store: Store) -> list[str]:
    """Distinct workflow ids of the cube by skipping along its primary key (a DISTINCT would read every row)."""
    out: list[str] = []
    while True:
        r = _rows(store, "SELECT workflow_id FROM cube WHERE workflow_id>? ORDER BY workflow_id LIMIT 1", (out[-1] if out else "",))
        if not r:
            return out
        out.append(r[0][0])


def _expected_calls(store: Store, lo: int, hi: int) -> int:
    """Calls in sim minutes [lo, hi) from the hourly ``cube`` (hours that overlap the window count whole)."""
    first, last = lo // 60, (hi - 1) // 60
    total = 0
    for wid in _cube_workflows(store):
        total += _rows(store, "SELECT COALESCE(SUM(dials),0) FROM cube WHERE workflow_id=? AND day>=? AND day<=?"
                              " AND day*24+hour>=? AND day*24+hour<=?", (wid, first // 24, last // 24, first, last))[0][0]
    return total


def ingest_stalled(store: Store, now: int, wall_lag_s: float = 0.0) -> list[Alert]:
    newest = view_data.newest_call_minute(store)
    if newest is None:
        return []
    lag = now - newest
    expected = _expected_calls(store, now - STALL_SIM_MIN - DAY, now - DAY) if lag > STALL_SIM_MIN else 0
    if lag > STALL_SIM_MIN and expected >= STALL_MIN_EXPECTED:
        msg = (f"INGEST STALLED: newest stored call is {lag} sim min old ({fmt_minute(newest)}) although the same "
               f"{STALL_SIM_MIN} min yesterday had {expected} calls. Look: `python -m controller status` (lag, events ingested) and the "
               f"simulator's /sim/state last_event_id; if the sim is healthy restart the controller (docs/RUNBOOK.md).")
        return [Alert("cond:ingest_stalled", PAGE, "ingest_stalled", None, msg)]
    if wall_lag_s > STALL_WALL_S:
        return [Alert("cond:ingest_stalled", PAGE, "ingest_stalled", None,
                      f"INGEST STALLED: a tick took {wall_lag_s:.0f}s from advance to decisions done. "
                      "Look: `python -m controller status`; check the simulator and the controller log.")]
    return []


def apply_by_near(store: Store, now: int) -> list[Alert]:
    out = []
    for pid, wid, apply_by in _rows(store, "SELECT proposal_id, workflow_id, apply_by_minute FROM proposals WHERE state='APPROVED'"
                                    " AND apply_by_minute IS NOT NULL AND apply_by_minute-?<=?", (now, APPLY_BY_WARN_MIN)):
        left = apply_by - now
        if left < 0:  # past the deadline is a different state: page once as lapse risk, never "-5 sim min left"
            out.append(Alert(f"once:apply_by_passed:{pid}", PAGE, "apply_by_passed", wid,
                             f"APPROVED change {pid} is still not applied and its apply_by ({fmt_minute(apply_by)}) has "
                             f"passed {-left} sim min ago: it has lapsed or is about to. Look: `python -m controller view "
                             f"--workflow {wid}` and `python -m controller why --workflow {wid} --at \"{fmt_minute(now)}\"`."))
            continue
        out.append(Alert(f"cond:apply_by:{pid}", PAGE, "apply_by_near", wid,
                         f"APPROVED change {pid} is not applied and has {left} sim min left (apply_by {fmt_minute(apply_by)}); "
                         f"it lapses if missed. Look: `python -m controller view --workflow {wid}` (what we need) and "
                         f"decisions kind apply_withheld / apply_intent via `python -m controller why --workflow {wid} "
                         f"--from \"Day {now // DAY}\" --to \"Day {now // DAY}\"`."))
    return out


def credits_low(store: Store, credits: float | None) -> list[Alert]:
    if credits is None:
        return []
    n = max(1, len(_rows(store, "SELECT 1 FROM meta WHERE key LIKE 'workflow_policy:%'")))
    reserve = incidents.reserve_per_workflow(incidents.initial_credits(store), n) * n
    if credits >= reserve:
        return []
    return [Alert("cond:credits_low", PAGE, "credits_low", None,
                  f"CREDITS BELOW INCIDENT RESERVE: {credits:.1f} left, reserve for {n} workflows is {reserve:.0f}. A "
                  "hallucination incident may be unaffordable. Look: `python -m controller status` (burn) and decisions "
                  "kind validation_run / credit_outcome_unknown; stop routine validation spend.")]


def repeated_5xx(store: Store, now: int) -> list[Alert]:
    try:
        cur = _rows(store, "SELECT c5xx FROM metrics_ext ORDER BY id DESC LIMIT 1")
        old = _rows(store, "SELECT c5xx FROM metrics_ext WHERE sim_minute<=? ORDER BY id DESC LIMIT 1", (now - 60,)) or \
            _rows(store, "SELECT c5xx FROM metrics_ext ORDER BY id LIMIT 1")
    except Exception:  # noqa: BLE001 - table missing on a foreign DB
        return []
    if not cur or not old or cur[0][0] - old[0][0] < SERVER_ERRORS_PER_HOUR:
        return []
    return [Alert("cond:sim_5xx", PAGE, "sim_5xx", None,
                  f"REPEATED SIMULATOR 5xx: {cur[0][0] - old[0][0]} server errors in the last sim hour. The controller "
                  "retries reads but never advance/apply blindly. Look: `python -m controller status` (error counts) and "
                  "the controller log; check the simulator process.")]


def accounting_bugs(store: Store, now: int) -> list[Alert]:
    """A fatigue rejection or a 403 means the controller's own accounting/pre-checks are wrong."""
    out = []
    for approval_id, wid, payload in _rows(store, "SELECT approval_id, workflow_id, payload FROM proposal_events WHERE event='rejected'"):
        if not _is_hygiene(payload):
            continue
        look = f"`python -m controller why --workflow {wid} --at \"{fmt_minute(now)}\"`"
        if FATIGUE_REASON in (payload or "").lower():
            out.append(Alert(f"once:fatigue:{approval_id}", PAGE, "fatigue_rejection", wid,
                             f"FATIGUE REJECTION on approval {approval_id} ({payload}): our request-accounting is wrong. Look: "
                             f"{look} and the 24h ledger in governance."))
        else:
            out.append(Alert(f"once:hygiene:{approval_id}", PAGE, "hygiene_rejection", wid,
                             f"HYGIENE REJECTION on approval {approval_id} ({payload}): the request was malformed or lacked "
                             f"evidence the approver requires, so our local pre-check is wrong. Look: {look}."))
    total = json.loads(store.get_meta("client_error_totals") or "{}").get("403", 0)
    if total:
        out.append(Alert(f"once:403:{total}", PAGE, "http_403", None,
                         f"403 FROM THE SIMULATOR (total {total}): an auto-apply was sent that policy does not permit, i.e. a "
                         "local pre-check bug. Look: `python -m controller status` and alerts auto_apply_refused / "
                         "proposal_invalid_locally in the DB."))
    return out


def cohort_mismatch(store: Store, now: int) -> list[Alert]:
    out = []
    for wid, day, n in _rows(store, "SELECT workflow_id, sim_minute/? , COUNT(*) FROM alerts WHERE code LIKE 'cohort_mismatch%'"
                             " AND severity NOT IN ('page','digest') AND (sim_minute IS NULL OR sim_minute>=?) GROUP BY 1, 2",
                             (DAY, now - DAY)):
        out.append(Alert(f"once:cohort:{wid}:{day}", PAGE, "cohort_mismatch", wid,
                         f"COHORT SELF-CHECK MISMATCH: {n} alert rows on day {day}; the cohort the sim assigned differs "
                         "from our own assignment, so per-cohort analysis is unreliable. Look: "
                         f"`python -m controller view --workflow {wid}`; stop cohort-based changes until resolved."))
    return out


# ------------------------------------------------------------------ DIGEST rules
def judgement_rejections(store: Store, now: int) -> list[Alert]:
    out = []
    for pid, wid, kind, aid, payload in _rows(
            store, "SELECT p.proposal_id, p.workflow_id, p.kind, p.approval_id, "
                   "(SELECT e.payload FROM proposal_events e WHERE e.approval_id=p.approval_id AND e.event='rejected' LIMIT 1)"
                   " FROM proposals p WHERE p.state='REJECTED'"):
        if _is_hygiene(payload):
            continue  # a hygiene rejection is a controller bug, not a judgement call: handled as a page
        out.append(Alert(f"once:rejected:{pid}", DIGEST, "judgement_rejection", wid,
                         f"The approver rejected {kind} ({pid}): {payload or 'no reason given'}. Nothing was changed. Look: "
                         f"`python -m controller why --workflow {wid} --at \"{fmt_minute(now)}\"`; the planner re-plans on its own."))
    return out


def _starved_now(store: Store, now: int) -> list[tuple[str, str, str]]:
    out = []
    for key, value in _rows(store, "SELECT key, value FROM detector_state WHERE detector='experiments'"):
        st = json.loads(value)
        start = st.get("start_minute")
        if st.get("phase") != "running" or start is None or now - start < STARVE_AFTER_DAYS * DAY:
            continue
        wid, dim = key.split(":", 1)
        col = {"welcome_message": "welcome", "voice": "voice"}[dim]
        # the cube is calls aggregated by day/hour/variant: day granularity (the start day counts whole) is plenty here
        have = {r.key[0]: r.connects for r in cube.rollup(store, wid, by=(col,), day_min=start // DAY)}
        thin = sorted(a for a, v in (st.get("arms") or {}).items()
                      if v.get("state") == "active" and have.get(a, 0) < STARVE_MIN_CONNECTS)
        if thin:
            out.append((key, wid, f"{dim} experiment has run {(now - start) // DAY} days but arm(s) {', '.join(thin)} have under "
                                  f"{STARVE_MIN_CONNECTS} connects: not enough traffic to ever conclude. Look: "
                                  f"`python -m controller view --workflow {wid}` (WHAT'S BEING TESTED)."))
    return out


def starved_experiments(store: Store, now: int) -> list[Alert]:
    """Once per sim day (the answer is cached in ``detector_state`` so the condition stays active between checks)."""
    day = now // DAY
    marker = _rows(store, "SELECT value FROM detector_state WHERE detector=? AND key=?", (DETECTOR, STARVE_MARKER))
    cached = json.loads(marker[0][0]) if marker else None
    if cached is None or cached["day"] != day:
        cached = {"day": day, "found": _starved_now(store, now)}
        store.conn.execute("INSERT OR REPLACE INTO detector_state(detector, key, value) VALUES (?,?,?)",
                           (DETECTOR, STARVE_MARKER, json.dumps(cached)))
    return [Alert(f"cond:starved:{key}", DIGEST, "starved_experiment", wid, msg) for key, wid, msg in cached["found"]]


def weak_base(store: Store, now: int) -> list[Alert]:
    return [Alert(f"once:weakbase:{i}", DIGEST, "weak_base_did", wid,
                  f"{summary}. A number with a weak base is not a probe casualty; it will stay at low share. Look: "
                  f"`python -m controller view --workflow {wid}` (DID probes).")
            for i, wid, summary in _rows(store, "SELECT id, workflow_id, summary FROM decisions WHERE kind='did_weak_base'")]


def _hour_rates(store: Store, wid: str, lo: int, hi: int) -> dict[int, tuple[int, int, int, int]]:
    return {r.key[0]: (r.dials, r.connects, r.engaged, 0) for r in cube.rollup(store, wid, by=("hour",), day_min=lo, day_max=hi)}


def drift_z(store: Store, wid: str, day: int, kpi: str) -> tuple[float, float, float] | None:
    """(z, observed, expected) of ``day`` against the trailing three days at the same hours (hour-adjusted)."""
    base, cur = _hour_rates(store, wid, day - 3, day - 1), _hour_rates(store, wid, day, day)
    obs = exp = var = 0.0
    for hour, (dials, connects, engaged, _) in cur.items():
        b = base.get(hour)
        if not b:
            continue
        n_b, x_b = (b[0], b[1]) if kpi == "pickup" else (b[1], b[2])
        n, x = (dials, connects) if kpi == "pickup" else (connects, engaged)
        if n_b < DRIFT_MIN_BASE_N or n <= 0:
            continue
        p = x_b / n_b
        obs, exp = obs + x, exp + n * p
        var += n * p * (1 - p) * (1 + n / n_b)  # outcome noise plus the baseline's own estimation noise
    if var <= 0:
        return None
    return (obs - exp) / math.sqrt(var), obs, exp


def kpi_drift(store: Store, now: int) -> list[Alert]:
    """Once per sim day, for the day just finished: daily z-test per KPI against the 3 days before it."""
    day = now // DAY - 1
    out = []
    for (wid,) in _rows(store, "SELECT DISTINCT workflow_id FROM config_versions"):
        marker = _rows(store, "SELECT value FROM detector_state WHERE detector=? AND key=?", (DETECTOR, f"eval:drift:{wid}"))
        if day < 3 or (marker and int(marker[0][0]) >= day):
            continue
        store.conn.execute("INSERT OR REPLACE INTO detector_state(detector, key, value) VALUES (?,?,?)",
                           (DETECTOR, f"eval:drift:{wid}", str(day)))
        changed = _rows(store, "SELECT 1 FROM config_versions WHERE workflow_id=? AND ts_minute>=? AND ts_minute<? LIMIT 1",
                        (wid, day * DAY, (day + 1) * DAY))
        incident = _rows(store, "SELECT 1 FROM incidents WHERE workflow_id=? AND opened_minute<? AND (closed_minute IS NULL OR"
                                " closed_minute>=?) LIMIT 1", (wid, (day + 1) * DAY, day * DAY))
        if changed or incident:
            continue  # attributable to a known change or incident
        for kpi in ("pickup", "engagement"):
            res = drift_z(store, wid, day, kpi)
            if res and abs(res[0]) >= DRIFT_Z:
                z, obs, exp = res
                out.append(Alert(f"once:drift:{wid}:{day}:{kpi}", DIGEST, "kpi_drift", wid,
                                 f"{kpi} on day {day} was {'above' if z > 0 else 'below'} the hour-adjusted trailing-3-day "
                                 f"expectation (z {z:+.1f}; {obs:.0f} vs {exp:.0f}) with no config change or incident to explain it. "
                                 f"Look: `python -m controller view --workflow {wid} --at \"Day {day} 23:59\"`."))
    return out


# ------------------------------------------------------------------ engine
def evaluate(store: Store, now: int, credits: float | None = None, wall_lag_s: float = 0.0) -> list[Alert]:
    """Run every rule on its own: one raising rule is logged and digested (once a day), the others still run."""
    rules = (("ingest_stalled", lambda: ingest_stalled(store, now, wall_lag_s)), ("apply_by_near", lambda: apply_by_near(store, now)),
             ("credits_low", lambda: credits_low(store, credits)), ("repeated_5xx", lambda: repeated_5xx(store, now)),
             ("accounting_bugs", lambda: accounting_bugs(store, now)), ("cohort_mismatch", lambda: cohort_mismatch(store, now)),
             ("judgement_rejections", lambda: judgement_rejections(store, now)),
             ("starved_experiments", lambda: starved_experiments(store, now)),
             ("weak_base", lambda: weak_base(store, now)), ("kpi_drift", lambda: kpi_drift(store, now)))
    found: list[Alert] = []
    for name, rule in rules:
        try:
            found += rule()
        except Exception as exc:  # noqa: BLE001 - one broken rule must not silence the others
            log.exception("alert rule %s failed at sim minute %d", name, now)
            found.append(Alert(f"once:rule_failed:{name}:{now // DAY}", DIGEST, RULE_FAILED_CODE, None,
                               f"Alert rule `{name}` raised {type(exc).__name__}: {exc}. Its alerts are missing until fixed; the "
                               "other rules and metrics are unaffected. Look: the controller log (stack trace) and "
                               "`python -m controller status`."))
    return found


def apply(store: Store, now: int, found: list[Alert]) -> list[Alert]:
    """Write alerts for keys not already active; forget ``cond:`` keys whose condition has cleared. Returns new ones."""
    known = {k: v for k, v in _rows(store, "SELECT key, value FROM detector_state WHERE detector=? AND key NOT LIKE 'eval:%'", (DETECTOR,))}
    now_keys = {a.key for a in found}
    fresh = [a for a in found if a.key not in known]
    for a in fresh:
        store.add_alert(now, a.severity, a.code, a.message, a.workflow_id)
        store.conn.execute("INSERT OR REPLACE INTO detector_state(detector, key, value) VALUES (?,?,?)", (DETECTOR, a.key, str(now)))
        log.warning("%s %s: %s", a.severity.upper(), a.code, a.message)
    for key in known:
        if key.startswith("cond:") and key not in now_keys:
            store.conn.execute("DELETE FROM detector_state WHERE detector=? AND key=?", (DETECTOR, key))
    return fresh


class TickObserver:
    """Per-tick hook for loop.py: write ``metrics_ext`` and run the alert rules. Never lets observability break the loop."""

    def __init__(self, client, store: Store) -> None:
        self.client, self.store = client, store
        self._seen: dict[str, int] = {}

    def after_tick(self, state: Mapping, now: int) -> list[Alert]:
        try:
            counters = dict(getattr(self.client, "counters", {}))
            delta = {k: v - self._seen.get(k, 0) for k, v in counters.items()}
            self._seen = counters
            credits = state.get("credits_remaining")
            last = self.store.last_metric() or {}
            with self.store.transaction():
                totals = metrics.add_counters(self.store, delta)
                metrics.record(self.store, now, credits, totals)
                return apply(self.store, now, evaluate(self.store, now, credits, float(last.get("lag_s", 0.0))))
        except Exception:  # noqa: BLE001
            log.exception("observability hook failed (loop continues)")
            return []
