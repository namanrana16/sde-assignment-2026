"""Credit-costing calls (suggest-fixes, validation runs) with a recorded ledger.

Those calls are never retried (a retry would pay twice). If one fails in transit we do not
know whether it was charged, so the spend is reconciled from ``/sim/state``
``credits_remaining`` and recorded; the caller decides whether to ask again.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .client import OutcomeUnknown, SimClient
from .faults import crash_point
from .store import Store


def call_with_credit_ledger(client: SimClient, store: Store, workflow_id: str, minute: int,
                            description: str, call: Callable[..., Any], *args: Any) -> Any:
    before = client.credits_remaining()
    try:
        response = call(*args)
    except OutcomeUnknown:
        spent = round(before - client.credits_remaining(), 4)
        _record(store, workflow_id, minute, spent, description, "outcome_unknown")
        store.add_decision(minute, "credit_outcome_unknown",
                           f"{description}: transport failure, {spent} credits charged", workflow_id,
                           {"credits": spent})
        raise
    crash_point("after-credit-call-before-record")  # charged, response in hand, nothing recorded yet
    spent = round(before - float(response.get("credits_remaining", before)), 4)
    _record(store, workflow_id, minute, spent, description, json.dumps(response, sort_keys=True))
    return response


def _record(store: Store, workflow_id: str, minute: int, credits: float, request: str,
            response: str) -> None:
    store.conn.execute("INSERT INTO llm_calls(workflow_id, sim_minute, credits, request, response)"
                       " VALUES (?,?,?,?,?)", (workflow_id, minute, credits, request, response))


INITIAL_KEY = "credits_initial"
UNRECORDED_EPS = 0.001


def reconcile_ledger(client: SimClient, store: Store, minute: int) -> float:
    """Startup: make the ledger sum equal (initial balance - sim balance).

    A crash between a charge and its ledger row leaves spend we cannot attribute to a call;
    it is recorded as an ``unrecorded`` row so ``sum(llm_calls.credits)`` always reconciles.
    Returns the unrecorded amount booked now (0.0 if none).
    """
    remaining = client.credits_remaining()
    raw = store.get_meta(INITIAL_KEY)
    if raw is None:
        store.set_meta(INITIAL_KEY, repr(remaining))
        return 0.0
    booked = store.conn.execute("SELECT COALESCE(SUM(credits),0) FROM llm_calls").fetchone()[0]
    missing = round(float(raw) - remaining - booked, 4)
    if missing <= UNRECORDED_EPS:
        return 0.0
    _record(store, "", minute, missing, "unrecorded: crash between charge and ledger row", "unrecorded")
    store.add_decision(minute, "credit_unrecorded",
                       f"{missing} credits were charged but never recorded (crash); booked now", None,
                       {"credits": missing})
    return missing
