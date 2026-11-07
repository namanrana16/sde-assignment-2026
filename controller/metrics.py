"""Loop observability (C14): per-tick metrics beyond the ``metrics`` table, and the ``status`` report.

The ``metrics`` table (loop.py) already holds events ingested, ingest wall time, wall lag and tick time per tick.
This module adds ``metrics_ext`` (credits, in-flight approvals and their nearest deadline, the newest event minute,
cumulative local 4xx/409/403/5xx counts) written once per tick, and builds the ``status`` summary from both.
Reads work on a read-only connection (``status`` and the web UI use one).
"""
from __future__ import annotations

import json
from typing import Mapping

from . import settings
from .audit import fmt_minute
from .store import Store
from .view_data import IN_FLIGHT, newest_call_minute, rows

DAY = settings.MINUTES_PER_DAY
_DDL = """CREATE TABLE IF NOT EXISTS metrics_ext (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER NOT NULL,
    credits REAL,
    in_flight INTEGER NOT NULL,
    next_deadline INTEGER,
    newest_event_minute INTEGER,
    decisions_total INTEGER NOT NULL,
    c4xx INTEGER NOT NULL, c409 INTEGER NOT NULL, c403 INTEGER NOT NULL, c5xx INTEGER NOT NULL)"""
_PHASES_DDL = """CREATE TABLE IF NOT EXISTS tick_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER NOT NULL, reconcile_s REAL, ingest_s REAL,
    apply_s REAL, detect_s REAL, plan_s REAL)"""
COUNTER_KEYS = ("4xx", "409", "403", "5xx")


def ensure(store: Store) -> None:
    store.conn.execute(_DDL)


def record_phases(store: Store, minute: int, ph: Mapping[str, float]) -> None:
    """Per-phase wall time of a tick (load analysis); best effort, never breaks the loop."""
    try:
        store.conn.execute(_PHASES_DDL)
        store.conn.execute(
            "INSERT INTO tick_phases(sim_minute, reconcile_s, ingest_s, apply_s, detect_s, plan_s) VALUES (?,?,?,?,?,?)",
            (minute, *(ph.get(k) for k in ("reconcile_s", "ingest_s", "apply_s", "detect_s", "plan_s"))))
    except Exception:  # noqa: BLE001
        pass


def add_counters(store: Store, delta: Mapping[str, int]) -> dict[str, int]:
    """Add this process's new local error counts to the cumulative totals kept in ``meta`` (survives restarts)."""
    total = json.loads(store.get_meta("client_error_totals") or "{}")
    for k in COUNTER_KEYS:
        total[k] = int(total.get(k, 0)) + int(delta.get(k, 0))
    store.set_meta("client_error_totals", json.dumps(total, sort_keys=True))
    return total


def deadlines(store: Store) -> tuple[int, int | None]:
    """(approvals in flight, nearest apply_by of an APPROVED one or expiry of a PENDING one)."""
    marks = ",".join("?" * len(IN_FLIGHT))
    n = store.conn.execute(f"SELECT COUNT(*) FROM proposals WHERE route='approval' AND state IN ({marks})", IN_FLIGHT).fetchone()[0]
    near = store.conn.execute(
        "SELECT MIN(CASE WHEN state='APPROVED' THEN apply_by_minute ELSE expires_minute END) FROM proposals"
        " WHERE route='approval' AND state IN ('PENDING','APPROVED')").fetchone()[0]
    return n, near


def record(store: Store, minute: int, credits: float | None, totals: Mapping[str, int]) -> None:
    ensure(store)
    n, near = deadlines(store)
    newest = newest_call_minute(store)
    decisions = store.conn.execute("SELECT COUNT(*) FROM decisions WHERE kind!='advance_intent'").fetchone()[0]
    store.conn.execute(
        "INSERT INTO metrics_ext(sim_minute, credits, in_flight, next_deadline, newest_event_minute, decisions_total,"
        " c4xx, c409, c403, c5xx) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (minute, credits, n, near, newest, decisions, totals.get("4xx", 0), totals.get("409", 0),
         totals.get("403", 0), totals.get("5xx", 0)))
    store.conn.execute("DELETE FROM metrics_ext WHERE id < (SELECT MAX(id) FROM metrics_ext) - 5000")


# ------------------------------------------------------------------ status
def _agg(store: Store, since: int) -> dict:
    r = rows(store, "SELECT COUNT(*) AS ticks, COALESCE(SUM(events_ingested),0) AS events, AVG(ingest_wall_s) AS ingest_avg,"
             " MAX(ingest_wall_s) AS ingest_max, AVG(lag_s) AS lag_avg, MAX(lag_s) AS lag_max, MAX(tick_wall_s) AS tick_max,"
             " MIN(sim_minute) AS lo, MAX(sim_minute) AS hi FROM metrics WHERE sim_minute>=?", (since,))
    return r[0] if r else {}


def collect(store: Store) -> dict:
    last = rows(store, "SELECT * FROM metrics ORDER BY id DESC LIMIT 1")
    ext = rows(store, "SELECT * FROM metrics_ext ORDER BY id DESC LIMIT 1")
    last, ext = (last[0] if last else None), (ext[0] if ext else None)
    now = last["sim_minute"] if last else 0
    since = now - DAY
    agg = _agg(store, since) if last else {}
    hours = max(((agg.get("hi") or 0) - (agg.get("lo") or 0)) / 60, 1e-9)
    decisions = rows(store, "SELECT COUNT(*) AS n FROM decisions WHERE sim_minute>=? AND kind!='advance_intent'", (since,))[0]["n"] if last else 0
    credits_24h_ago = rows(store, "SELECT credits FROM metrics_ext WHERE sim_minute<=? AND credits IS NOT NULL ORDER BY id DESC LIMIT 1", (since,))
    page = rows(store, "SELECT COUNT(*) AS n FROM alerts WHERE severity='page' AND (sim_minute IS NULL OR sim_minute>=?)", (since,))
    digest = rows(store, "SELECT COUNT(*) AS n FROM alerts WHERE severity='digest' AND (sim_minute IS NULL OR sim_minute>=?)", (since,))
    return {"last": last, "ext": ext, "now": now, "agg24": agg,
            "decisions_per_sim_hour": decisions / hours if last and agg.get("ticks") else 0.0,
            "sim_lag_minutes": (now - ext["newest_event_minute"]) if ext and ext["newest_event_minute"] is not None else None,
            "credits_burn_24h": (credits_24h_ago[0]["credits"] - ext["credits"]) if credits_24h_ago and ext and ext["credits"] is not None else None,
            "page_alerts_24h": page[0]["n"] if page else 0, "digest_alerts_24h": digest[0]["n"] if digest else 0,
            "calls": (rows(store, "SELECT SUM(dials) AS n FROM cube") or [{}])[0].get("n") or 0}  # cube = calls, aggregated


def render(m: dict) -> str:
    last, ext, a = m["last"], m["ext"], m["agg24"]
    if last is None:
        return "no ticks recorded yet"
    f = lambda v, d=2: "n/a" if v is None else f"{v:.{d}f}"  # noqa: E731
    out = [f"status at {fmt_minute(m['now'])}  (calls stored: {m['calls']})",
           "latest tick:",
           f"  events ingested {last['events_ingested']}, ingest wall {last['ingest_wall_s']:.2f}s, tick wall {last['tick_wall_s']:.2f}s,"
           f" step {last['step_minutes']} min",
           f"  lag: {last['lag_s']:.2f}s wall (advance return -> decisions done); "
           f"{m['sim_lag_minutes'] if m['sim_lag_minutes'] is not None else 'n/a'} sim min since the newest event"]
    if ext:
        near = f"{fmt_minute(ext['next_deadline'])} ({ext['next_deadline'] - m['now']} sim min from now)" if ext["next_deadline"] is not None else "none"
        out += [f"  credits remaining {f(ext['credits'])}"
                + (f", burned {f(m['credits_burn_24h'])} in the last 24 sim h" if m["credits_burn_24h"] is not None else ""),
                f"  approvals in flight {ext['in_flight']}, nearest apply_by/expiry {near}",
                f"  local error counts (cumulative): 4xx {ext['c4xx']} (409 {ext['c409']}, 403 {ext['c403']}), 5xx {ext['c5xx']}"]
    else:
        out.append("  (no extended metrics yet: written once per tick by the running controller)")
    out += ["last 24 sim hours:",
            f"  ticks {a.get('ticks', 0)}, events ingested {a.get('events', 0)}, ingest wall avg {f(a.get('ingest_avg'))}s max {f(a.get('ingest_max'))}s,"
            f" wall lag avg {f(a.get('lag_avg'))}s max {f(a.get('lag_max'))}s, slowest tick {f(a.get('tick_max'))}s",
            f"  decisions/hour {m['decisions_per_sim_hour']:.1f} (sim hours), alerts: {m['page_alerts_24h']} page, {m['digest_alerts_24h']} digest"]
    return "\n".join(out)
