"""Planner: rules P1-P5 of CONTEXT section 8 C9 over a registry of desired changes.

Producers are pure functions of the data (see ``desired``); the planner decides *whether and
how* each tick's wanted changes are sent, one submission per workflow per tick at most:

* P1  at most one in-flight approval per workflow, except a superseding higher-priority one.
* P2  priority incident > severe (harm-stop/rollback) > daily bundle. A more urgent need while
      something is in flight files a superseding proposal carrying ALL still-wanted changes, in
      the reserved slot; whichever is approved first and still valid is applied (governance).
* P3  operational auto-applies only when nothing is in flight (exception: flagged-DID
      evacuation, ``evidence["flag_evacuation"]``), at most one step per hour (per tick in
      incident mode); anything the policy does not allow falls back to the approval path.
* P4  fatigue ledger and soft caps (``fatigue``); P5 timing windows (``fatigue``); the daily
      bundle is a scheduled job keyed by sim day with ``last_run`` persisted in ``jobs``.
* P7  local validation happens in ``Governance.submit`` (a failing proposal is never sent).
"""
from __future__ import annotations

import logging
from typing import Mapping

from . import settings
from .cohorts import VOCAB_META_KEY, FeatureVocab
from .configlib import ProposalError, apply_patch, changed_dims
from .desired import DEFAULT_REGISTRY, ROUTE_AUTO, DesiredChange, PlanContext, Priority, Registry
from .governance import Governance
from .proposal_repo import ROUTE_APPROVAL as ROW_APPROVAL
from .proposal_repo import ROUTE_AUTO as ROW_AUTO
from .proposal_repo import SUBMIT_FAILED, ProposalRepo, ProposalRow
from .proposals import ProposalDraft, build_draft
from .store import Store

log = logging.getLogger(__name__)
DAY = settings.MINUTES_PER_DAY


class Planner:
    def __init__(self, client, store: Store, gov: Governance, registry: Registry | None = None) -> None:
        self.client = client
        self.store = store
        self.gov = gov
        self.registry = registry or DEFAULT_REGISTRY

    # ------------------------------------------------------------ scheduled jobs
    def job_due(self, name: str, wid: str, now: int) -> bool:
        last = self.store.get_job(name, wid)
        if not (last is None or last // DAY < now // DAY):
            return False
        # a kill between submit and job_mark must not allow a second bundle: the row is the proof
        return not (name == settings.JOB_DAILY_BUNDLE and ProposalRepo(self.store).routine_bundle_on_day(
            wid, now // DAY, int(Priority.ROUTINE)))

    def job_mark(self, name: str, wid: str, now: int) -> None:
        self.store.set_job(name, wid, now)

    # ------------------------------------------------------------ entry points
    def context(self, now: int) -> PlanContext:
        return PlanContext(self.store, self.client, self.gov.workflows, now)

    def run_detectors(self, now: int) -> None:
        self.registry.run_detectors(self.context(now))

    def plan(self, now: int) -> list[ProposalRow]:
        """Plan every workflow. Free when no producer is registered (no HTTP, no queries)."""
        if not self.registry.producers:
            return []
        ctx = self.context(now)
        out = []
        for wid in sorted(self.gov.workflows):
            row = self._plan_workflow(ctx, wid, now)
            if row is not None:
                out.append(row)
        return out

    # ------------------------------------------------------------ per workflow
    def _wait(self, wid: str, now: int, reason: str) -> None:
        if self.gov.note_once(f"wait:{wid}:{reason}", now):
            self.store.add_decision(now, "plan_wait", reason, wid)

    def _effective(self, wf: Mapping, current: Mapping, changes: list[DesiredChange], now: int
                   ) -> list[DesiredChange]:
        """Drop changes already in effect (producers re-emit until applied) or unbuildable."""
        keep = []
        for ch in changes:
            try:
                if changed_dims(current, apply_patch(wf, current, ch.patch, self._vocab())):
                    keep.append(ch)
            except ProposalError as exc:
                if self.gov.note_once(f"bad:{ch.workflow}:{ch.kind}:{exc}", now):
                    self.gov.alert(now, "warn", "desired_change_invalid", f"{ch.kind}: {exc}", ch.workflow)
        return keep

    def _plan_workflow(self, ctx: PlanContext, wid: str, now: int) -> ProposalRow | None:
        wanted = self.registry.collect(ctx, wid)
        if not wanted:
            return None
        wf = self.gov.workflows[wid]
        version, current = self.gov.live(wid)
        wanted = self._effective(wf, current, wanted, now)
        if not wanted:
            return None
        auto = [c for c in wanted if c.route_preference == ROUTE_AUTO]
        approval = [c for c in wanted if c.route_preference != ROUTE_AUTO]
        if auto:
            row, fallback = self._try_auto(wf, version, current, auto, now)
            if row is not None:
                return row  # steps are never chained within a tick (A5)
            if fallback:
                approval += auto
        return self._plan_approval(wf, version, current, approval, now) if approval else None

    # ------------------------------------------------------------ P3: auto path
    def _try_auto(self, wf: Mapping, version: int, current: Mapping, changes: list[DesiredChange],
                  now: int) -> tuple[ProposalRow | None, bool]:
        """Returns (row, fall_back_to_approval)."""
        wid = wf["workflow_id"]
        draft = self._draft(wf, version, current, changes, now)
        if draft is None:
            return None, False
        if self.gov.validate(draft, ROW_AUTO):
            return None, True  # the policy does not allow it as auto: use the approval path
        if self.gov.submit_backed_off(wid, draft.kind, ROW_AUTO, now, min(c.priority for c in changes)):
            return None, True  # an auto 403/422 happened: use the approval route instead (its own backoff applies)
        in_flight = self.gov.in_flight(wid)
        evacuation = any(c.evidence.get("flag_evacuation") for c in changes)
        if in_flight and not evacuation:
            self._wait(wid, now, "auto-apply waits: an approval is in flight (P3)")
            return None, False
        incident = any(c.priority == Priority.INCIDENT for c in changes)
        gap = (settings.AUTO_APPLY_INCIDENT_GAP_MINUTES if incident
               else settings.AUTO_APPLY_MIN_GAP_MINUTES)
        last = self.gov.repo.last_auto_minute(wid)
        if last is not None and now - last < gap:
            self._wait(wid, now, f"auto-apply: at most one step per {gap} min (A5)")
            return None, False
        row = self.gov.submit(draft, now, route=ROW_AUTO, priority=min(c.priority for c in changes),
                              wanted=[c.summary for c in changes])
        return row, False

    # ------------------------------------------------------------ P1/P2/P4/P5: approval path
    def _plan_approval(self, wf: Mapping, version: int, current: Mapping,
                       changes: list[DesiredChange], now: int) -> ProposalRow | None:
        wid = wf["workflow_id"]
        top = min(c.priority for c in changes)
        routine = top == Priority.ROUTINE
        in_flight = self.gov.in_flight(wid)
        supersedes = None
        if in_flight:
            if top >= min(p.priority for p in in_flight):
                self._wait(wid, now, "an approval is already in flight (P1)")
                return None
            supersedes = max(in_flight, key=lambda p: p.plan_seq).proposal_id
        elif routine and not self.job_due(settings.JOB_DAILY_BUNDLE, wid, now):
            return None
        ok, why = self.gov.can_file(wid, now, top)
        if not ok:
            self._wait(wid, now, why)
            return None
        draft = self._draft(wf, version, current, changes, now)
        if draft is None:
            return None
        row = self.gov.submit(draft, now, route=ROW_APPROVAL, priority=top, supersedes=supersedes,
                              wanted=[c.summary for c in changes])
        if routine and not in_flight and row is not None and row.state != SUBMIT_FAILED:
            self.job_mark(settings.JOB_DAILY_BUNDLE, wid, now)  # one routine bundle per day
        return row

    def _draft(self, wf, version, current, changes, now) -> ProposalDraft | None:
        try:
            return build_draft(self.store, self._vocab(), wf, version, current, changes, now)
        except ProposalError as exc:
            if self.gov.note_once(f"merge:{wf['workflow_id']}:{exc}", now):
                self.gov.alert(now, "warn", "desired_change_invalid", f"cannot merge changes: {exc}",
                               wf["workflow_id"])
            return None

    def _vocab(self) -> FeatureVocab:
        return FeatureVocab.load(self.store.get_meta(VOCAB_META_KEY))
