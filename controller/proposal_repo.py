"""Persistence for the proposal state machine: ``proposals``, ``proposal_events``, ``decisions``.

Every transition writes the proposals row, one proposal_events row and one decisions row in a
single transaction, so the audit trail can never disagree with the current state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import settings
from .store import Store

DRAFT, INTENT, PENDING, APPROVED, APPLIED = "DRAFT", "INTENT", "PENDING", "APPROVED", "APPLIED"
MONITORING, CLOSED, REJECTED, EXPIRED = "MONITORING", "CLOSED", "REJECTED", "EXPIRED"
STALE, LAPSED, SUPERSEDED, SUBMIT_FAILED, AUTO_APPLIED = (
    "STALE", "LAPSED", "SUPERSEDED", "SUBMIT_FAILED", "AUTO_APPLIED")

TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({INTENT}),
    INTENT: frozenset({PENDING, AUTO_APPLIED, STALE, SUBMIT_FAILED, SUPERSEDED}),
    PENDING: frozenset({APPROVED, REJECTED, EXPIRED, STALE, SUPERSEDED}),
    APPROVED: frozenset({APPLIED, STALE, LAPSED, SUPERSEDED}),
    APPLIED: frozenset({MONITORING, CLOSED}),
    AUTO_APPLIED: frozenset({MONITORING, CLOSED}),
    MONITORING: frozenset({CLOSED}),
}
IN_FLIGHT = (INTENT, PENDING, APPROVED)  # approval-route proposals that block P1
OPEN = (INTENT, PENDING, APPROVED, APPLIED, AUTO_APPLIED, MONITORING)
ROUTE_APPROVAL, ROUTE_AUTO = "approval", "auto"


class IllegalTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class ProposalRow:
    proposal_id: str
    workflow_id: str
    kind: str
    state: str
    approval_id: str | None
    base_version: int
    created_minute: int
    route: str
    priority: int
    plan_seq: int
    filed_minute: int | None
    apply_by_minute: int | None
    expires_minute: int | None
    supersedes: str | None
    applied_version: int | None
    applied_minute: int | None
    body: dict


_COLS = ("proposal_id, workflow_id, kind, state, approval_id, base_version, created_minute, route,"
         " priority, plan_seq, filed_minute, apply_by_minute, expires_minute, supersedes,"
         " applied_version, applied_minute, body")
_UPDATABLE = {"approval_id", "filed_minute", "apply_by_minute", "expires_minute", "applied_version",
              "applied_minute"}


def _row(r: tuple) -> ProposalRow:
    return ProposalRow(*r[:-1], json.loads(r[-1]) if r[-1] else {})


class ProposalRepo:
    def __init__(self, store: Store) -> None:
        self.store = store

    @property
    def conn(self):
        return self.store.conn

    # ---------------------------------------------------------------- reads
    def get(self, proposal_id: str) -> ProposalRow | None:
        r = self.conn.execute(f"SELECT {_COLS} FROM proposals WHERE proposal_id=?",
                              (proposal_id,)).fetchone()
        return _row(r) if r else None

    def by_state(self, *states: str, workflow_id: str | None = None) -> list[ProposalRow]:
        sql = f"SELECT {_COLS} FROM proposals WHERE state IN ({','.join('?' * len(states))})"
        args: list[Any] = list(states)
        if workflow_id:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        return [_row(r) for r in self.conn.execute(sql + " ORDER BY plan_seq, proposal_id", args)]

    def in_flight(self, workflow_id: str) -> list[ProposalRow]:
        return [p for p in self.by_state(*IN_FLIGHT, workflow_id=workflow_id)
                if p.route == ROUTE_APPROVAL]

    def next_plan_seq(self, workflow_id: str) -> int:
        mine = self.conn.execute("SELECT COALESCE(MAX(plan_seq), 0) FROM proposals"
                                 " WHERE workflow_id=?", (workflow_id,)).fetchone()[0]
        floor = int(self.store.get_meta(f"plan_seq_floor:{workflow_id}") or 0)
        return max(mine, floor) + 1

    def raise_plan_seq_floor(self, workflow_id: str, seq: int) -> None:
        """Ids up to ``seq`` already exist in the sim (e.g. our state dir was lost): never reuse them."""
        key = f"plan_seq_floor:{workflow_id}"
        if seq > int(self.store.get_meta(key) or 0):
            self.store.set_meta(key, str(seq))

    def count_recent_requests(self, workflow_id: str, now: int) -> int:
        """Fatigue ledger keyed on created_at (the sim's rule: created_minute > now - 24h).

        An INTENT row we are not sure was sent counts too (conservative).
        """
        since = now - settings.MINUTES_PER_DAY
        return self.conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE workflow_id=? AND route='approval' AND"
            " ((filed_minute IS NOT NULL AND filed_minute > ?) OR"
            "  (filed_minute IS NULL AND state='INTENT' AND created_minute > ?))",
            (workflow_id, since, since)).fetchone()[0]

    def routine_bundle_on_day(self, workflow_id: str, day: int, routine_priority: int) -> bool:
        """Has a routine approval-route proposal (any state but SUBMIT_FAILED, INTENT included) been
        created on this sim day? That row, not the separate job mark, is the proof the bundle is spent."""
        r = self.conn.execute(
            "SELECT 1 FROM proposals WHERE workflow_id=? AND route='approval' AND priority>=? AND"
            " state!='SUBMIT_FAILED' AND created_minute>=? AND created_minute<? LIMIT 1",
            (workflow_id, routine_priority, day * settings.MINUTES_PER_DAY,
             (day + 1) * settings.MINUTES_PER_DAY)).fetchone()
        return r is not None

    def recent_submit_failure(self, workflow_id: str, kind: str, route: str, now: int,
                              backoff_min: int, *, fixed: bool = False) -> bool:
        """Is (workflow, kind, route) backed off after SUBMIT_FAILED rows? The window starts at
        ``backoff_min`` and doubles per failure in the last 24 h (capped at a day); ``fixed`` skips the doubling."""
        rows = [r[0] for r in self.conn.execute(
            "SELECT created_minute FROM proposals WHERE workflow_id=? AND kind=? AND route=? AND"
            " state='SUBMIT_FAILED' AND created_minute>? ORDER BY created_minute",
            (workflow_id, kind, route, now - settings.MINUTES_PER_DAY))]
        if not rows:
            return False
        window = backoff_min if fixed else min(backoff_min * 2 ** (len(rows) - 1), settings.MINUTES_PER_DAY)
        return now - rows[-1] < window

    def rejection_minutes(self, workflow_id: str, kind: str, since: int) -> list[int]:
        """Sim minutes (ascending) at which proposals of (workflow, kind) were recorded REJECTED after ``since``."""
        return [r[0] for r in self.conn.execute(
            "SELECT e.ts_minute FROM proposal_events e JOIN proposals p ON p.proposal_id=e.proposal_id"
            " WHERE p.workflow_id=? AND p.kind=? AND p.route='approval' AND e.event=? AND e.ts_minute>?"
            " ORDER BY e.ts_minute, e.id", (workflow_id, kind, REJECTED, since))]

    def last_auto_minute(self, workflow_id: str) -> int | None:
        r = self.conn.execute("SELECT MAX(applied_minute) FROM proposals WHERE workflow_id=? AND"
                              " route='auto' AND applied_minute IS NOT NULL", (workflow_id,)).fetchone()
        return r[0]

    def events(self, proposal_id: str) -> list[tuple]:
        return self.conn.execute("SELECT ts_minute, event, payload FROM proposal_events"
                                 " WHERE proposal_id=? ORDER BY id", (proposal_id,)).fetchall()

    def decided_event(self, approval_id: str) -> str | None:
        """Raw sim status from a consumed ``approval_decided`` event, if ingest saw one."""
        r = self.conn.execute("SELECT event FROM proposal_events WHERE approval_id=? AND"
                              " event_id IS NOT NULL ORDER BY id DESC LIMIT 1", (approval_id,)).fetchone()
        return r[0] if r else None

    # ---------------------------------------------------------------- writes
    def create_intent(self, row: ProposalRow, now: int) -> None:
        """DRAFT then INTENT with the full body persisted BEFORE anything is sent."""
        with self.store.transaction():
            self.conn.execute(
                f"INSERT INTO proposals({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.proposal_id, row.workflow_id, row.kind, INTENT, None, row.base_version,
                 row.created_minute, row.route, row.priority, row.plan_seq, None, None, None,
                 row.supersedes, None, None, json.dumps(row.body, sort_keys=True)))
            self._log(row.proposal_id, now, DRAFT, row.workflow_id, None, {"kind": row.kind})
            self._log(row.proposal_id, now, INTENT, row.workflow_id, row.approval_id,
                      {"route": row.route, "base_version": row.base_version,
                       "dims": row.body.get("dims"), "supersedes": row.supersedes})

    def transition(self, row: ProposalRow, new_state: str, now: int, note: str = "",
                   payload: dict | None = None, **fields: Any) -> ProposalRow:
        """Atomic state change + proposal_events row + decisions row."""
        cur = self.get(row.proposal_id)
        if cur is None or new_state not in TRANSITIONS.get(cur.state, frozenset()):
            raise IllegalTransition(f"{row.proposal_id}: {cur.state if cur else None} -> {new_state}")
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        sets = ", ".join(["state=?"] + [f"{k}=?" for k in fields])
        with self.store.transaction():
            self.conn.execute(f"UPDATE proposals SET {sets} WHERE proposal_id=?",
                              (new_state, *fields.values(), row.proposal_id))
            approval_id = fields.get("approval_id") or cur.approval_id
            self._log(row.proposal_id, now, new_state, cur.workflow_id, approval_id,
                      {"from": cur.state, "note": note, **(payload or {}), **fields})
            if "approval_id" in fields:  # link events ingest saw before we knew the approval id
                self.conn.execute("UPDATE proposal_events SET proposal_id=? WHERE approval_id=?"
                                  " AND proposal_id IS NULL", (row.proposal_id, fields["approval_id"]))
        return self.get(row.proposal_id)  # type: ignore[return-value]

    def log_only(self, row: ProposalRow, event: str, now: int, payload: dict | None = None) -> None:
        """A write-ahead marker (e.g. apply_intent) that is not a state change."""
        with self.store.transaction():
            self._log(row.proposal_id, now, event, row.workflow_id, row.approval_id, payload or {})

    def _log(self, proposal_id: str, now: int, event: str, workflow_id: str,
             approval_id: str | None, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO proposal_events(proposal_id, approval_id, workflow_id, ts_minute, event,"
            " payload) VALUES (?,?,?,?,?,?)",
            (proposal_id, approval_id, workflow_id, now, event, json.dumps(payload, sort_keys=True)))
        self.store.add_decision(now, f"proposal_{event.lower()}", f"{proposal_id}: {event}",
                                workflow_id, {"proposal_id": proposal_id, "approval_id": approval_id,
                                              **payload})
