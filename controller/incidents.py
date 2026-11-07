"""Incident records (``incidents`` table), the credit policy for incident work, and validation runs.

Incident mode (CONTEXT glossary) is simply "a workflow has an open incident"; the planner reads
priorities from the desired changes, this module keeps the durable record.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import settings
from .client import OutcomeUnknown, SimClient
from .credits import call_with_credit_ledger
from .store import Store

log = logging.getLogger(__name__)
OPEN, CLOSED = "open", "closed"


# ------------------------------------------------------------------ incident rows
def open_incident(store: Store, wid: str, kind: str, now: int, body: dict) -> int:
    cur = store.conn.execute("INSERT INTO incidents(workflow_id, kind, state, opened_minute, body)"
                             " VALUES (?,?,?,?,?)", (wid, kind, OPEN, now, json.dumps(body, sort_keys=True)))
    store.add_decision(now, "incident_opened", f"{kind} incident opened", wid, {"incident_id": cur.lastrowid, **body})
    return int(cur.lastrowid)


def _row(r: tuple) -> dict:
    return {"id": r[0], "workflow_id": r[1], "kind": r[2], "state": r[3], "opened_minute": r[4],
            "closed_minute": r[5], "body": json.loads(r[6]) if r[6] else {}}


def open_incidents(store: Store, wid: str, kind: str | None = None) -> list[dict]:
    sql = ("SELECT id, workflow_id, kind, state, opened_minute, closed_minute, body FROM incidents"
           " WHERE workflow_id=? AND state=?" + (" AND kind=?" if kind else "") + " ORDER BY id")
    args: tuple = (wid, OPEN) + ((kind,) if kind else ())
    return [_row(r) for r in store.conn.execute(sql, args).fetchall()]


def all_incidents(store: Store, wid: str, kind: str) -> list[dict]:
    return [_row(r) for r in store.conn.execute(
        "SELECT id, workflow_id, kind, state, opened_minute, closed_minute, body FROM incidents"
        " WHERE workflow_id=? AND kind=? ORDER BY id", (wid, kind)).fetchall()]


def update_incident(store: Store, incident_id: int, body: dict) -> None:
    store.conn.execute("UPDATE incidents SET body=? WHERE id=?", (json.dumps(body, sort_keys=True), incident_id))


def close_incident(store: Store, incident_id: int, now: int, note: str) -> None:
    row = store.conn.execute("SELECT workflow_id, kind, state FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None or row[2] == CLOSED:
        return
    store.conn.execute("UPDATE incidents SET state=?, closed_minute=? WHERE id=?", (CLOSED, now, incident_id))
    store.add_decision(now, "incident_closed", f"{row[1]} incident closed: {note}", row[0],
                       {"incident_id": incident_id})


# ------------------------------------------------------------------ credit policy (R8)
def reserve_per_workflow(credits_initial: float | None, n_workflows: int) -> float:
    """Incident reserve kept per workflow: the configured 25, but at most half the starting balance split over
    the workflows, so a many-workflow fleet on a small budget can still afford routine checks (docs/THRESHOLDS.md)."""
    base = settings.CREDIT_RESERVE_PER_WORKFLOW
    if not credits_initial or n_workflows <= 0:
        return base
    return min(base, credits_initial / (2 * n_workflows))


def initial_credits(store: Store) -> float | None:
    raw = store.get_meta("credits_initial")
    return float(raw) if raw is not None else None


def can_spend(credits: float, cost: float, n_workflows: int, *, incident_spent: float | None = None,
              credits_initial: float | None = None) -> bool:
    """Routine spend (``incident_spent is None``) must leave the full incident reserve of every
    workflow untouched. Incident spend may use the reserve but one incident spends at most
    ``INCIDENT_CREDIT_CAP`` and never more than we have (so the sim can never answer 402)."""
    if cost > credits + 1e-9:
        return False
    if incident_spent is None:
        return credits - cost >= reserve_per_workflow(credits_initial, n_workflows) * n_workflows - 1e-9
    return incident_spent + cost <= settings.INCIDENT_CREDIT_CAP + 1e-9


def spent_since(store: Store, wid: str, since_minute: int) -> float:
    row = store.conn.execute("SELECT COALESCE(SUM(credits),0) FROM llm_calls WHERE workflow_id=? AND sim_minute>=?",
                             (wid, since_minute)).fetchone()
    return float(row[0])


def daily_cap_ok(store: Store, wid: str, now: int, cost: float) -> bool:
    """Per-workflow, per-sim-day ceiling on credit spend so repeated false incidents cannot drain credits."""
    day_start = now // settings.MINUTES_PER_DAY * settings.MINUTES_PER_DAY
    return spent_since(store, wid, day_start) + cost <= settings.INCIDENT_CREDIT_DAILY_CAP + 1e-9


def pick_validation_n(credits: float, floor_credits: float = 0.0) -> int | None:
    """Largest standard validation size the credits can cover (leaving ``floor_credits``), else None."""
    for n in settings.HALLUC_VALIDATION_N_STEPS:
        if validation_cost(n) <= credits - floor_credits + 1e-9:
            return n
    return None


def validation_cost(n: int) -> float:
    return float(-(-n // 100))  # ceil(n / 100)


# ------------------------------------------------------------------ validation runs
_RUN_ID = re.compile(r'"validation_run_id"\s*:\s*"([^"]+)"')


def validation_for(store: Store, wid: str, prompt: str, min_pass: float | None = None) -> dict | None:
    """Best (highest pass rate, newest) stored validation of this workflow's prompt."""
    rows = store.conn.execute("SELECT run_id, n, pass_rate, sim_minute FROM validation_runs WHERE workflow_id=?"
                              " AND prompt_version=? ORDER BY pass_rate DESC, sim_minute DESC", (wid, prompt)).fetchall()
    for run_id, n, rate, minute in rows:
        if min_pass is None or rate >= min_pass:
            return {"run_id": run_id, "n": n, "pass_rate": rate, "sim_minute": minute}
    return None


def validations_of(store: Store, wid: str, prompt: str) -> list[dict]:
    rows = store.conn.execute("SELECT run_id, n, pass_rate, sim_minute FROM validation_runs WHERE workflow_id=?"
                              " AND prompt_version=? ORDER BY sim_minute, run_id", (wid, prompt)).fetchall()
    return [{"run_id": r, "n": n, "pass_rate": p, "sim_minute": m} for r, n, p, m in rows]


def store_validation(store: Store, run: dict, credits: float, minute: int) -> None:
    store.conn.execute("INSERT OR IGNORE INTO validation_runs(run_id, workflow_id, prompt_version, n, pass_rate,"
                       " credits, sim_minute) VALUES (?,?,?,?,?,?,?)",
                       (run["validation_run_id"], run["workflow_id"], run["prompt_version"], run["n"],
                        run["pass_rate"], credits, minute))


def recover_validations(store: Store) -> int:
    """Re-create validation_runs rows lost to a crash between the paid call and its insert."""
    known = {r[0] for r in store.conn.execute("SELECT run_id FROM validation_runs")}
    fixed = 0
    for credits, response, minute in store.conn.execute(
            "SELECT credits, response, sim_minute FROM llm_calls WHERE response LIKE '%validation_run_id%'").fetchall():
        try:
            run = json.loads(response)
        except ValueError:
            continue
        if run.get("validation_run_id") and run["validation_run_id"] not in known:
            store_validation(store, run, credits, minute)
            known.add(run["validation_run_id"])
            fixed += 1
    return fixed


def run_validation(client: SimClient, store: Store, wid: str, prompt: str, now: int,
                   n: int = settings.HALLUC_VALIDATION_N) -> dict | None:
    """One paid validation run through the ledger. None if the outcome is unknown (never retried)."""
    try:
        run = call_with_credit_ledger(client, store, wid, now, f"validation {wid} {prompt} n={n}",
                                      client.run_validation, wid, prompt, n)
    except OutcomeUnknown:
        return None
    store_validation(store, run, validation_cost(n), now)
    store.add_decision(now, "validation_run", f"{wid} {prompt}: pass_rate {run['pass_rate']} (n={n}, {run['validation_run_id']})",
                       wid, {k: run[k] for k in ("validation_run_id", "prompt_version", "n", "failures", "pass_rate")})
    return run
