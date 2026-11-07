"""Governance client: proposal state machine, submission, reconciliation, apply (CONTEXT C9).

States: DRAFT -> INTENT -> PENDING -> APPROVED -> APPLIED -> MONITORING -> CLOSED, with
REJECTED, EXPIRED (=rejection: re-plan with fresh data and a new id), STALE, LAPSED (alert),
SUPERSEDED and SUBMIT_FAILED. Auto path: INTENT -> AUTO_APPLIED | STALE.

Invariants (R2, R9):
* ``proposal_id = "{workflow}:{plan_seq}:{kind}"`` and the full request body are persisted in
  state INTENT BEFORE anything is sent, so a crash can always be reconciled by looking the id up.
* ``/apply`` is called only if ``now <= apply_by`` AND the live config version equals the
  proposal's base version. Otherwise the proposal goes LAPSED/STALE locally and the planner
  re-plans; the sim never logs a graded lapse or apply_conflict.
* Non-idempotent calls are never retried by the client; ``OutcomeUnknown`` leaves the proposal
  in its state and the next reconcile resolves it from the sim's own records.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from . import client as c
from . import settings
from .desired import Priority, VetoCrash
from .fatigue import apply_window_unreachable, filing_window_ok, slot_available, soft_cap_ignored
from .faults import crash_point
from .ingest import ts_to_minute
from .proposal_repo import (
    APPLIED, APPROVED, AUTO_APPLIED, CLOSED, EXPIRED, INTENT, LAPSED, MONITORING, PENDING,
    REJECTED, ROUTE_APPROVAL, ROUTE_AUTO, STALE, SUBMIT_FAILED, SUPERSEDED, ProposalRepo,
    ProposalRow)
from .proposals import ProposalDraft, validate_approval_fields, validate_auto_apply
from .store import Store

log = logging.getLogger(__name__)
LOOKUP_FAILED = object()
_SEND_ERRORS = (c.Forbidden, c.Unprocessable, c.BadRequest, c.Conflict, c.NotFound, c.OutOfCredits,
                c.Lapsed, c.NotApproved)


class Governance:
    def __init__(self, client: c.SimClient, store: Store, workflows: Mapping[str, dict]) -> None:
        self.client = client
        self.store = store
        self.workflows = dict(workflows)
        self.repo = ProposalRepo(store)
        self._live_cache: dict[str, tuple[int, dict]] = {}
        self._noted: dict[str, int] = {}
        self._skip_apply: set[str] = set()  # workflows whose /apply outcome is unknown this tick
        self.tick_minutes = settings.TICK_MINUTES  # the sim's tick (set by the loop): when decisions are first seen
        self.veto_check = None  # optional (row, now) -> reason: withhold /apply because the evidence went stale

    # ------------------------------------------------------------ helpers
    def live(self, wid: str) -> tuple[int, dict]:
        """Current (version, config) from the sim, mirrored into our store."""
        if wid not in self._live_cache:
            cfg = self.client.get_config(wid)
            self.store.store_config_body(wid, cfg["version"], cfg.get("actor"), cfg["config"])
            self._live_cache[wid] = (cfg["version"], cfg["config"])
        return self._live_cache[wid]

    def invalidate(self) -> None:
        self._live_cache.clear()

    def alert(self, now: int, severity: str, code: str, message: str, wid: str | None) -> None:
        self.store.add_alert(now, severity, code, message, wid)
        log.warning("%s %s: %s", code, wid, message)

    def note_once(self, key: str, now: int) -> bool:
        """True the first time per sim day: keeps repeated per-tick conditions from spamming."""
        day = now // settings.MINUTES_PER_DAY
        if self._noted.get(key) == day:
            return False
        row = self.store.conn.execute("SELECT value FROM detector_state WHERE detector='note_once'"
                                      " AND key=?", (key,)).fetchone()  # survives a restart
        self._noted[key] = day
        if row is not None and row[0] == str(day):
            return False
        self.store.conn.execute("INSERT OR REPLACE INTO detector_state(detector, key, value)"
                                " VALUES ('note_once', ?, ?)", (key, str(day)))
        return True

    def in_flight(self, wid: str) -> list[ProposalRow]:
        return self.repo.in_flight(wid)

    def any_in_flight(self) -> bool:
        return any(self.repo.in_flight(w) for w in self.workflows)

    def requests_used(self, wid: str, now: int) -> int:
        return self.repo.count_recent_requests(wid, now)

    def tenant_of(self, wid: str) -> str | None:
        return self.workflows[wid].get("tenant_id")

    def can_file(self, wid: str, now: int, priority: int, expires_in_hours: float | None = None
                 ) -> tuple[bool, str]:
        """P4 + P5 for an approval request: budget (reserved last slot) and timing."""
        gov = self.workflows[wid]["governance"]
        tenant, tick = self.tenant_of(wid), self.tick_minutes
        routine = priority == Priority.ROUTINE
        ok, why = slot_available(self.requests_used(wid, now), gov, priority, wid, tenant)
        if not ok:
            return False, why
        if apply_window_unreachable(gov, tick) and self.note_once(f"apply_window:{wid}", now):
            self.alert(now, "error", "apply_window_unreachable",
                       f"apply window {gov.get('apply_window_hours')} h is too short for a {tick}-min tick: "
                       "an approval would lapse before it can be applied; not filing", wid)
        ignored = soft_cap_ignored(gov, wid, expires_in_hours, tenant, tick)
        if ignored and routine and self.note_once(f"softcap:{wid}", now):
            self.store.add_decision(now, "soft_cap_ignored", f"soft cap not honoured: {ignored}", wid)
        return filing_window_ok(now, gov, routine, wid, expires_in_hours, tenant_id=tenant, tick=tick)

    def refile_cooldown(self, wid: str, kind: str, now: int) -> str | None:
        """Why this (workflow, kind) must not be refiled yet after repeated rejections, else None.

        The first rejection never delays the refile (the producer re-plans with fresh data); from the
        second rejection within 24 h the wait grows by REFILE_AFTER_REJECT_MIN per extra rejection
        (capped at a day), so a producer that keeps re-emitting cannot burn the daily budget."""
        times = self.repo.rejection_minutes(wid, kind, now - settings.MINUTES_PER_DAY)
        if len(times) < 2:
            return None
        wait = min(settings.REFILE_AFTER_REJECT_MIN * (len(times) - 1), settings.MINUTES_PER_DAY)
        if now < times[-1] + wait:
            return f"{kind} was rejected {len(times)}x in 24h: no refile until minute {times[-1] + wait}"
        return None

    def seed_plan_seqs(self) -> None:
        """Startup: raise each workflow's plan_seq floor above every ``{wid}:{seq}:{kind}`` id the sim
        already holds (approvals and auto-applied config history), so a lost state dir cannot make us
        collide with (and adopt) old proposals."""
        try:
            ids = [str(ap.get("proposal_id", "")) for ap in self.client.list_approvals()]
        except (c.SimUnavailable, c.OutcomeUnknown, c.NotFound) as exc:
            log.warning("could not seed plan_seq from /approvals: %s", exc)
            ids = []
        for wid in self.workflows:
            try:
                ids += [str(h.get("proposal_id") or "") for h in self.client.config_history(wid)]
            except (c.SimUnavailable, c.OutcomeUnknown, c.NotFound) as exc:
                log.warning("could not seed plan_seq from %s config history: %s", wid, exc)
        for pid in ids:
            wid, _, rest = pid.partition(":")
            seq = rest.partition(":")[0]
            if wid in self.workflows and seq.isdigit():
                self.repo.raise_plan_seq_floor(wid, int(seq))

    def _lookup_validation(self, run_id: str) -> dict | None:
        r = self.store.conn.execute("SELECT workflow_id, prompt_version, pass_rate FROM"
                                    " validation_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(zip(("workflow_id", "prompt_version", "pass_rate"), r)) if r else None

    # ------------------------------------------------------------ submit
    def validate(self, draft: ProposalDraft, route: str) -> list[str]:
        wf = self.workflows[draft.workflow_id]
        if route == ROUTE_AUTO:
            return validate_auto_apply(draft, wf)
        return validate_approval_fields(draft, wf, self._lookup_validation)

    def submit(self, draft: ProposalDraft, now: int, *, route: str, priority: int,
               supersedes: str | None = None, wanted: list[str] | None = None) -> ProposalRow | None:
        """Validate locally, persist INTENT, then send. Returns None if nothing was sent."""
        wid = draft.workflow_id
        problems = self.validate(draft, route)
        if problems:
            if self.note_once(f"invalid:{wid}:{route}:{problems}", now):
                self.store.add_decision(now, "proposal_invalid_locally", "; ".join(problems), wid,
                                        {"kind": draft.kind, "route": route, "dims": list(draft.dims)})
                self.alert(now, "warn", "proposal_invalid_locally",
                           f"{draft.kind} not sent: {'; '.join(problems)}", wid)
            return None
        if self.submit_backed_off(wid, draft.kind, route, now, priority):
            if self.note_once(f"backoff:{wid}:{draft.kind}:{route}", now):
                self.alert(now, "warn", "submit_backoff",
                           f"{draft.kind} via {route}: a recent submit failed; not resending for "
                           f"{self._backoff_minutes(route, priority)} sim minutes", wid)
            return None
        if route == ROUTE_APPROVAL:
            ok, why = self.can_file(wid, now, priority, draft.expires_in_hours)
            if ok and (cooldown := self.refile_cooldown(wid, draft.kind, now)):
                ok, why = False, cooldown
            if not ok:
                self.store.add_decision(now, "proposal_deferred", why, wid, {"kind": draft.kind})
                return None
        seq = self.repo.next_plan_seq(wid)
        pid = f"{wid}:{seq}:{draft.kind}"
        request = draft.approval_body(pid) if route == ROUTE_APPROVAL else draft.auto_body(pid)
        body = {"request": request, "dims": list(draft.dims), "tv": draft.tv,
                "max_shift": draft.max_shift, "summary": draft.change_summary,
                "wanted": wanted or [], "impact_score": draft.impact_score,
                "affected_leads": draft.affected_leads}
        row = ProposalRow(pid, wid, draft.kind, INTENT, None, draft.base_version, now, route,
                          int(priority), seq, None, None, None, supersedes, None, None, body)
        self.repo.create_intent(row, now)
        return self._send(row, now)

    @staticmethod
    def _urgent_approval(route: str, priority: int) -> bool:
        return route == ROUTE_APPROVAL and priority <= Priority.INCIDENT

    def _backoff_minutes(self, route: str, priority: int) -> int:
        if priority <= Priority.INCIDENT:
            # approval: short fixed retry; auto: never retry the failing route within a day
            return (settings.SUBMIT_FAILED_BACKOFF_URGENT_MIN if route == ROUTE_APPROVAL
                    else settings.MINUTES_PER_DAY)
        return settings.SUBMIT_FAILED_BACKOFF_MIN

    def submit_backed_off(self, wid: str, kind: str, route: str, now: int,
                          priority: int = Priority.ROUTINE) -> bool:
        """A persistent 403/422 must not turn into a fresh id (and a graded rejection) every tick.
        Incidents on the approval route retry on a short fixed delay (a failed send used no fatigue
        slot); a failing auto route is not retried for an incident (a graded auto_apply_rejected)."""
        return self.repo.recent_submit_failure(wid, kind, route, now, self._backoff_minutes(route, priority),
                                               fixed=self._urgent_approval(route, priority))

    def _send(self, row: ProposalRow, now: int) -> ProposalRow:
        """Send the stored request verbatim (first send and resend after a crash are identical)."""
        request, wid = row.body["request"], row.workflow_id
        try:
            if row.route == ROUTE_APPROVAL:
                resp = self.client.request_approval(request)
            else:
                resp = self.client.put_config(wid, request)
        except c.StaleBase as exc:
            return self.repo.transition(row, STALE, now, "base changed before send", {"body": exc.body})
        except c.DuplicateProposal:
            return self._adopt_remote(row, now)
        except (c.OutcomeUnknown, c.SimUnavailable) as exc:
            self.store.add_decision(now, "submit_outcome_unknown", str(exc), wid,
                                    {"proposal_id": row.proposal_id})
            return row  # stays INTENT; reconcile looks it up by proposal_id
        except _SEND_ERRORS as exc:
            self.alert(now, "error", "submit_failed", f"{row.proposal_id}: {exc}", wid)
            return self.repo.transition(row, SUBMIT_FAILED, now, str(exc), {"status": exc.status})
        crash_point("after-submit-before-record")
        self.invalidate()
        if row.route == ROUTE_AUTO:
            return self.repo.transition(row, AUTO_APPLIED, now, "auto-applied",
                                        {"changed_dims": resp.get("changed_dims")},
                                        applied_version=resp["version"], applied_minute=now)
        return self._record_approval(row, resp, now)

    def _record_approval(self, row: ProposalRow, ap: Mapping, now: int) -> ProposalRow:
        row = self.repo.transition(row, PENDING, now, "approval created",
                                   approval_id=ap["approval_id"],
                                   filed_minute=ts_to_minute(ap["created_at"]),
                                   expires_minute=ts_to_minute(ap["expires_at"]))
        return self._decide(row, ap, now) if ap["status"] != "pending" else row

    # ------------------------------------------------------------ decisions
    def _decide(self, row: ProposalRow, ap: Mapping, now: int) -> ProposalRow:
        status = ap["status"]
        if status == "rejected":
            self.alert_if_graded_reason(row, ap, now)
            return self.repo.transition(row, REJECTED, now, "rejected", {"reasons": ap.get("reasons")})
        if status == "expired":
            return self.repo.transition(row, EXPIRED, now, "expired: treated as a rejection, re-plan")
        if status in ("approved", "applied", "lapsed"):
            by = ts_to_minute(ap["apply_by"]) if ap.get("apply_by") else row.apply_by_minute
            row = self.repo.transition(row, APPROVED, now, "approved", {"decided_at": ap.get("decided_at")},
                                       apply_by_minute=by)
            if status == "applied":
                return self._mark_applied(row, now)
            if status == "lapsed":
                self.alert(now, "error", "approval_lapsed", row.proposal_id, row.workflow_id)
                return self.repo.transition(row, LAPSED, now, "sim reports lapsed")
        return row

    def alert_if_graded_reason(self, row: ProposalRow, ap: Mapping, now: int) -> None:
        """A rejection for fatigue or a field problem means OUR accounting/validation is wrong."""
        for reason in ap.get("reasons") or []:
            if "fatigue" in reason or "must" in reason or "evidence" in reason or "pass rate" in reason:
                self.alert(now, "error", "approver_rejected_for_hygiene",
                           f"{row.proposal_id}: {reason}", row.workflow_id)

    def _find_approval(self, row: ProposalRow) -> dict | None | object:
        """The sim's approval for this proposal id, None if there is none, or ``LOOKUP_FAILED``
        when the sim could not answer (the caller must not conclude anything from that)."""
        try:
            for ap in self.client.list_approvals(workflow_id=row.workflow_id):
                if ap["proposal_id"] == row.proposal_id:
                    return ap
        except (c.SimUnavailable, c.OutcomeUnknown, c.NotFound) as exc:
            log.warning("approval lookup for %s failed: %s", row.proposal_id, exc)
            return LOOKUP_FAILED
        return None

    def _adopt_remote(self, row: ProposalRow, now: int) -> ProposalRow:
        if row.route == ROUTE_AUTO:
            return self._adopt_auto(row, now)
        ap = self._find_approval(row)
        if ap is LOOKUP_FAILED:
            return row  # try again on the next reconcile
        if ap is None:
            self.alert(now, "error", "duplicate_without_approval",
                       f"{row.proposal_id}: sim says duplicate but no approval found", row.workflow_id)
            return self.repo.transition(row, SUBMIT_FAILED, now, "duplicate id, no approval")
        if ap["base_version"] != row.base_version or sorted(ap.get("changed_dims") or []) != sorted(
                row.body.get("dims") or []):
            self.alert(now, "error", "proposal_id_collision",
                       f"{row.proposal_id}: sim holds a different approval under this id; re-planning", row.workflow_id)
            self.repo.raise_plan_seq_floor(row.workflow_id, row.plan_seq)
            return self.repo.transition(row, STALE, now, "id collision with a foreign approval")
        return self._record_approval(row, ap, now)

    def _find_history(self, wid: str, proposal_id: str) -> dict | None:
        try:
            for h in self.client.config_history(wid):
                if h.get("proposal_id") == proposal_id:
                    return h
        except (c.SimUnavailable, c.OutcomeUnknown, c.NotFound) as exc:
            log.warning("config history for %s failed: %s", wid, exc)
        return None

    def _find_version(self, wid: str, proposal_id: str) -> int | None:
        h = self._find_history(wid, proposal_id)
        return h["version"] if h else None

    def _adopt_auto(self, row: ProposalRow, now: int) -> ProposalRow:
        h = self._find_history(row.workflow_id, row.proposal_id)
        if h is None:
            return row
        if h["version"] != row.base_version + 1 or sorted(h.get("changed_dims") or []) != sorted(
                row.body.get("dims") or []):
            self.alert(now, "error", "proposal_id_collision",
                       f"{row.proposal_id}: config history holds this id for a different change "
                       f"(v{h['version']}, {h.get('changed_dims')}); re-planning", row.workflow_id)
            self.repo.raise_plan_seq_floor(row.workflow_id, row.plan_seq)
            return self.repo.transition(row, STALE, now, "id collision with a foreign auto-applied version")
        self.invalidate()
        return self.repo.transition(row, AUTO_APPLIED, now, "adopted from config history",
                                    applied_version=h["version"], applied_minute=now)

    def _mark_applied(self, row: ProposalRow, now: int) -> ProposalRow:
        version = self._find_version(row.workflow_id, row.proposal_id)
        self.invalidate()
        return self.repo.transition(row, APPLIED, now, "applied", applied_version=version,
                                    applied_minute=now)

    # ------------------------------------------------------------ reconcile
    def reconcile(self, now: int) -> None:
        """Startup and per-tick: resolve every proposal whose outcome we may not have recorded."""
        self.invalidate()
        for row in self.repo.by_state(INTENT):
            self._reconcile_intent(row, now)
        for row in self.repo.by_state(PENDING):
            self._reconcile_pending(row, now)
        for row in self.repo.by_state(APPROVED):
            if any(e[1] == "apply_intent" for e in self.repo.events(row.proposal_id)):
                self._reconcile_apply_outcome(row, now)
        self._advance_monitoring(now)

    def _reconcile_intent(self, row: ProposalRow, now: int) -> None:
        wid = row.workflow_id
        if row.route == ROUTE_AUTO:
            if self._find_version(wid, row.proposal_id) is not None:
                self._adopt_auto(row, now)
                return
        else:
            ap = self._find_approval(row)
            if ap is LOOKUP_FAILED:
                return
            if ap is not None:
                self._record_approval(row, ap, now)
                return
        if self.live(wid)[0] != row.base_version:
            self.repo.transition(row, STALE, now, "base changed before the request was sent")
            return
        if row.route == ROUTE_APPROVAL:
            ok, why = filing_window_ok(now, self.workflows[wid]["governance"],
                                       row.priority == Priority.ROUTINE, wid,
                                       row.body["request"].get("expires_in_hours"),
                                       tenant_id=self.tenant_of(wid), tick=self.tick_minutes)
            if not ok:
                self.repo.transition(row, STALE, now, f"not resent: {why}")
                return
        self._send(row, now)

    def _reconcile_pending(self, row: ProposalRow, now: int) -> None:
        try:
            ap = self.client.get_approval(row.approval_id)
        except c.NotFound:
            self._approval_vanished(row, now)
            return
        if ap["status"] != "pending":
            self._decide(row, ap, now)
        elif self.live(row.workflow_id)[0] != row.base_version:
            self.repo.transition(row, STALE, now, "base changed while pending; never applied")

    def _approval_vanished(self, row: ProposalRow, now: int) -> None:
        """The sim has no such approval: alert once a day; once it is past its expiry (plus a tick of
        grace) it can never be decided, so release the in-flight slot as EXPIRED (a re-plan)."""
        if self.note_once(f"approval_missing:{row.proposal_id}", now):
            self.alert(now, "error", "approval_missing", f"{row.proposal_id}: {row.approval_id}",
                       row.workflow_id)
        if row.expires_minute is not None and now > row.expires_minute + self.tick_minutes:
            self.repo.transition(row, EXPIRED, now, "approval not found at the sim and past expiry: re-plan")

    def _reconcile_apply_outcome(self, row: ProposalRow, now: int) -> None:
        try:
            ap = self.client.get_approval(row.approval_id)
        except (c.SimUnavailable, c.OutcomeUnknown, c.NotFound) as exc:
            log.warning("apply outcome of %s unresolved: %s", row.proposal_id, exc)
            return  # stays APPROVED with its apply_intent; the next reconcile asks again
        if ap["status"] == "applied":
            self._mark_applied(row, now)
        elif ap["status"] == "lapsed":
            self.alert(now, "error", "approval_lapsed", row.proposal_id, row.workflow_id)
            self.repo.transition(row, LAPSED, now, "sim reports lapsed")
        elif ap["status"] == "approved":
            # The POST never reached the sim (crash after apply_intent, or OutcomeUnknown on a lost
            # request). Leave the row APPROVED: apply_approved re-runs its pre-checks (apply_by,
            # live version) and retries, or marks it LAPSED/STALE locally WITHOUT calling /apply.
            return
        else:  # neither applied, lapsed nor approved: nothing left to retry
            self.alert(now, "error", "apply_unexpected_status",
                       f"{row.proposal_id}: sim status {ap['status']!r} after not_approved", row.workflow_id)
            self.repo.transition(row, STALE, now, f"not applicable: sim status {ap['status']}")

    def _advance_monitoring(self, now: int) -> None:
        for row in self.repo.by_state(APPLIED, AUTO_APPLIED):
            self.repo.transition(row, MONITORING, now, "monitoring after apply")
        for row in self.repo.by_state(MONITORING):
            if row.applied_minute is not None and now >= row.applied_minute + settings.MONITOR_MINUTES:
                self.repo.transition(row, CLOSED, now, "monitoring window over")

    # ------------------------------------------------------------ apply
    def consume_decision_events(self, now: int) -> None:
        """Act on approval_decided events ingest has stored, without waiting for the next poll."""
        for row in self.repo.by_state(PENDING):
            if row.approval_id and self.repo.decided_event(row.approval_id):
                self._reconcile_pending(row, now)

    def apply_approved(self, now: int) -> list[ProposalRow]:
        """Apply approved changes if (and only if) still valid; otherwise re-plan locally."""
        self.invalidate()
        self.consume_decision_events(now)
        done: list[ProposalRow] = []
        self._skip_apply.clear()
        rows = sorted(self.repo.by_state(APPROVED),
                      key=lambda r: (r.priority, r.apply_by_minute or 0, r.plan_seq))
        for stale_view in rows:
            row = self.repo.get(stale_view.proposal_id)
            if row is None or row.state != APPROVED:
                continue  # superseded by an earlier apply in this loop
            if row.workflow_id in self._skip_apply:
                continue  # an earlier /apply here has an unknown outcome: do not risk a conflict
            if row.apply_by_minute is not None and now > row.apply_by_minute:
                self.alert(now, "error", "approval_lapsed_locally",
                           f"{row.proposal_id}: apply_by passed; /apply NOT called", row.workflow_id)
                self.repo.transition(row, LAPSED, now, "apply_by passed; not applied")
            elif self.live(row.workflow_id)[0] != row.base_version:
                self.repo.transition(row, STALE, now, "base version changed; /apply NOT called")
            elif (reason := self.veto_check(row, now) if self.veto_check else None):
                if isinstance(reason, VetoCrash) and self._veto_crash_retry(row, now, reason):
                    continue  # the hook crashed, not the evidence: keep the approval, retry next tick
                self.store.add_decision(now, "apply_withheld", f"{row.proposal_id}: /apply NOT called: {reason}",
                                        row.workflow_id, {"proposal_id": row.proposal_id, "reason": reason})
                self.repo.transition(row, SUPERSEDED, now, f"withheld at apply time: {reason}")
            else:
                applied = self._apply(row, now)
                if applied is not None:
                    done.append(applied)
        return done

    def _veto_crash_retry(self, row: ProposalRow, now: int, reason: str) -> bool:
        """Count a veto-hook crash on this approval; True while retries remain (never past apply_by, which
        the caller checks first on every tick)."""
        crashes = sum(1 for e in self.repo.events(row.proposal_id) if e[1] == "veto_crashed") + 1
        self.repo.log_only(row, "veto_crashed", now, {"n": crashes, "reason": reason})
        return crashes <= settings.VETO_CRASH_MAX_RETRIES

    def _apply(self, row: ProposalRow, now: int) -> ProposalRow | None:
        self.repo.log_only(row, "apply_intent", now, {"approval_id": row.approval_id})
        crash_point("before-apply-request")
        try:
            resp = self.client.apply_approval(row.workflow_id, row.approval_id)
        except (c.OutcomeUnknown, c.SimUnavailable) as exc:
            self.store.add_decision(now, "apply_outcome_unknown", str(exc), row.workflow_id,
                                    {"proposal_id": row.proposal_id})
            self.invalidate()  # the live config may or may not have moved
            self._skip_apply.add(row.workflow_id)
            return None
        except c.StaleBase:
            self.alert(now, "error", "apply_conflict", f"{row.proposal_id}: pre-check missed a stale base",
                       row.workflow_id)
            self.repo.transition(row, STALE, now, "stale at apply")
            return None
        except c.Lapsed:
            self.alert(now, "error", "approval_lapsed", f"{row.proposal_id}: lapsed at apply",
                       row.workflow_id)
            self.repo.transition(row, LAPSED, now, "lapsed at apply")
            return None
        except c.NotApproved:
            self._reconcile_apply_outcome(row, now)
            self._skip_apply.add(row.workflow_id)
            return None
        crash_point("after-apply-before-record")
        self.invalidate()
        applied = self.repo.transition(row, APPLIED, now, "applied", {"changed_dims": resp.get("changed_dims")},
                                       applied_version=resp["version"], applied_minute=now)
        self._supersede_partners(applied, now)
        return applied

    def _supersede_partners(self, applied: ProposalRow, now: int) -> None:
        """P2: the loser of an approved-first race is SUPERSEDED, never applied."""
        for p in self.repo.in_flight(applied.workflow_id):
            if p.supersedes == applied.proposal_id or p.proposal_id == applied.supersedes:
                self.repo.transition(p, SUPERSEDED, now, f"superseded by {applied.proposal_id}")
