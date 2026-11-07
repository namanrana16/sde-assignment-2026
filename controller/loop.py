"""The controller loop: fixed per-tick order (CONTEXT section 8, C9).

reconcile -> ingest -> apply approved -> detectors -> planner/submit -> metrics -> advance.
Detectors and producers come from a registry (empty by default: the controller then changes
nothing); governance reconciliation runs at startup and on every tick.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

from . import settings
from .client import AdvanceNotApplied, Finished, SimClient
from . import levers  # noqa: F401  (registers the Phase 3 detectors on the default registry)
from .credits import reconcile_ledger
from .desired import DEFAULT_REGISTRY, Registry
from .faults import crash_point
from .fatigue import in_hours
from .alerts import TickObserver
from .governance import Governance
from .planner import Planner
from .ingest import IngestStats, Ingestor, mirror_configs
from . import metrics, person_analysis
from .store import Store
from .world_id import note_progress

log = logging.getLogger(__name__)
_MAX_ADVANCE_FAILURES = 5
# Test knob (crash_test.py): slow the loop so a kill lands at many different sim moments. 0 in production.
TICK_SLEEP_S = float(os.environ.get("CONTROLLER_TICK_SLEEP_S", "0"))


class WorldRegressed(RuntimeError):
    """The sim went backwards (it restarted as a new world): exit non-zero so run.sh restarts us and
    ``open_world_store`` archives this DB and opens a fresh one."""


@dataclass(frozen=True)
class Windows:
    """Hour-granular activity windows read from ``GET /workflows``."""

    spans: tuple[tuple[int, int], ...]  # [start_hour, end_hour) in IST
    unknown: bool = False  # a workflow without readable windows: always treat as active

    @classmethod
    def from_workflows(cls, workflows: list[dict]) -> "Windows":
        spans: list[tuple[int, int]] = []
        unknown = False
        for wf in workflows:
            dialling = wf.get("dialling_window")
            approver = (wf.get("governance") or {}).get("approver_hours")
            if dialling and len(dialling) == 2:
                spans.append((int(dialling[0]), int(dialling[1])))
            else:
                unknown = True  # cannot tell when this workflow dials: always active
            if approver and len(approver) == 2:  # a missing approver window only drops its own span
                spans.append((int(approver[0]), int(approver[1])))
        return cls(tuple(spans), unknown)

    def active_at(self, minute: int) -> bool:
        tod = minute % settings.MINUTES_PER_DAY
        return self.unknown or any(in_hours(tod, (s, e)) for s, e in self.spans)

    def minutes_to_next_boundary(self, minute: int, tick: int = settings.TICK_MINUTES) -> int:
        """Minutes until the next window start (searching up to one day ahead)."""
        for ahead in range(tick, settings.MINUTES_PER_DAY + 1, tick):
            if self.active_at(minute + ahead):
                return ahead
        return settings.MINUTES_PER_DAY


def choose_step(minute: int, end_minute: int, windows: Windows, in_flight: bool,
                tick: int = settings.TICK_MINUTES) -> int:
    """One tick, or up to 60 min when idle outside every window with nothing in flight."""
    step = tick
    if not in_flight and not windows.active_at(minute):
        step = min(max(settings.LONG_STEP_MINUTES // tick, 1) * tick,
                   windows.minutes_to_next_boundary(minute, tick))
    step = min(step, end_minute - minute)
    return max(step, tick)


@dataclass
class RunResult:
    finished: bool
    ticks: int
    sim_minute: int
    stats: IngestStats = field(default_factory=IngestStats)


class Controller:
    def __init__(self, client: SimClient, store: Store, ingestor: Ingestor | None = None,
                 stop_at_minute: int | None = None, registry: Registry | None = None) -> None:
        self.client = client
        self.registry = registry if registry is not None else DEFAULT_REGISTRY
        self.gov: Governance | None = None
        self.planner: Planner | None = None
        self.store = store
        self.ingestor = ingestor or Ingestor(client, store)
        self.stop_at_minute = stop_at_minute
        self.windows = Windows(())
        self._workflow_ids: list[str] = []
        self._advance_returned = time.monotonic()
        self._last_state: dict = {}
        self._saw_finished = False
        self._phases: dict[str, float] = {}
        self.observer = TickObserver(client, store)

    # ------------------------------------------------------------ startup
    def startup(self) -> IngestStats:
        state = self.client.state()
        workflows = self.client.workflows()
        self._workflow_ids = [w["workflow_id"] for w in workflows]
        self.windows = Windows.from_workflows(workflows)
        for w in workflows:  # the audit prints approver hours/latency from OUR store (R7)
            self.store.set_meta(f"workflow_policy:{w['workflow_id']}",
                                json.dumps({"governance": w.get("governance"), "objective": w.get("objective")},
                                           sort_keys=True))
        mirror_configs(self.client, self.store, self._workflow_ids)
        self.gov = Governance(self.client, self.store, {w["workflow_id"]: w for w in workflows})
        self.gov.tick_minutes = int(state.get("tick_minutes") or settings.TICK_MINUTES)
        self.gov.seed_plan_seqs()
        self.planner = Planner(self.client, self.store, self.gov, self.registry)
        self.gov.veto_check = lambda row, now: self.registry.veto(self.planner.context(now), row.workflow_id, row.kind)
        self.gov.reconcile(state["sim_minute"])  # resolve anything a previous run left in flight
        reconcile_ledger(self.client, self.store, state["sim_minute"])
        stats = self.ingestor.sync()
        self.store.add_decision(state["sim_minute"], "startup",
                                f"startup: ingested {stats.events} events in {stats.chunks} chunks",
                                inputs={"workflows": self._workflow_ids,
                                        "cursor": self.store.get_cursor().last_event_id})
        self._advance_returned = time.monotonic()  # lag is measured from here, not from import
        return stats

    # ------------------------------------------------------------ per tick
    def in_flight(self) -> bool:
        return self.gov is not None and self.gov.any_in_flight()

    def plan_and_submit(self, now: int, phases: dict[str, float] | None = None) -> None:
        """Detectors, planner and submission (steps 4-6 of the tick)."""
        assert self.planner is not None
        t = time.perf_counter()
        self.planner.run_detectors(now)
        t2 = time.perf_counter()
        self.planner.plan(now)
        if phases is not None:
            phases["detect_s"], phases["plan_s"] = t2 - t, time.perf_counter() - t2

    def run_person_analysis(self, now: int, phases: dict[str, float]) -> None:
        """Daily per-person call-time analysis (flag-gated, ANALYSIS ONLY): never proposes, never raises."""
        t = time.perf_counter()
        try:
            person_analysis.maybe_run_daily(self.store, now)
        except Exception as exc:  # noqa: BLE001  (an analysis bug must not stop the loop)
            log.exception("person analysis failed")
            self.store.add_alert(now, "warn", "person_analysis_failed", f"{type(exc).__name__}: {exc}")
        phases["person_s"] = time.perf_counter() - t

    def _guarded(self, step: str, now: int, fn, *args) -> None:
        """Run one tick step; a deterministic bug in it must not stop the sim advancing (the run would
        score nothing). Roll back any open transaction, log, alert once per sim day, carry on. Sim-progress
        errors (``AdvanceNotApplied``/``WorldRegressed``) are never swallowed. Skipping a step is safe: the
        proposal state machine is persisted per transition and every step re-reads it next tick."""
        try:
            fn(*args)
        except (AdvanceNotApplied, WorldRegressed, Finished):
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("tick step %s failed", step)
            try:
                if self.store.conn.in_transaction:
                    self.store.conn.execute("ROLLBACK")
                key = f"tick_step_failed:{step}"
                if self.gov is None or self.gov.note_once(key, now):
                    self.store.add_alert(now, "error", "tick_step_failed",
                                         f"{step}: {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001  (reporting must never take the tick down)
                log.exception("could not record tick_step_failed")

    def _check_world(self, state: dict) -> None:
        """Mid-run regression check (startup does it in ``open_world_store``)."""
        last_minute = self.store.get_meta("sim_minute")
        cursor = self.store.get_cursor().last_event_id
        if (last_minute is not None and state["sim_minute"] < int(last_minute))                 or cursor > state["last_event_id"]:
            msg = (f"sim regressed mid-run: sim_minute {state['sim_minute']} < recorded {last_minute} or "
                   f"last_event_id {state['last_event_id']} < cursor {cursor}; exiting for a fresh world")
            self.store.add_alert(state["sim_minute"], "error", "world_regressed", msg)
            raise WorldRegressed(msg)

    def tick(self) -> tuple[bool, IngestStats]:
        """One loop iteration. Returns ``(finished_and_drained, stats)``."""
        t0 = time.perf_counter()
        state = self._last_state = self.client.state()
        self._check_world(state)
        note_progress(self.store, state)
        assert self.gov is not None
        now = state["sim_minute"]
        finished = state["finished"]
        ph: dict[str, float] = {}
        self.gov.tick_minutes = int(state.get("tick_minutes") or settings.TICK_MINUTES)
        t = time.perf_counter()
        self._guarded("reconcile", now, self.gov.reconcile, now)
        ph["reconcile_s"] = time.perf_counter() - t
        t = time.perf_counter()
        stats = self.ingestor.sync()
        ph["ingest_s"] = time.perf_counter() - t
        if not finished:
            t = time.perf_counter()
            self._guarded("apply_approved", now, self.gov.apply_approved, now)
            ph["apply_s"] = time.perf_counter() - t
            self._guarded("plan_and_submit", now, self.plan_and_submit, now, ph)
            self.run_person_analysis(now, ph)
        self._phases = ph
        hit_stop = self.stop_at_minute is not None and state["sim_minute"] >= self.stop_at_minute
        step = 0
        if not (finished or hit_stop):
            step = choose_step(state["sim_minute"], state["end_minute"], self.windows,
                               self.in_flight(), state.get("tick_minutes", settings.TICK_MINUTES))
        self._record_metrics(state["sim_minute"], stats, t0, step)
        metrics.record_phases(self.store, now, self._phases)
        t = time.perf_counter()
        self.observer.after_tick(state, now)  # metrics_ext + alert rules (never raises)
        self._record_observer_phase(time.perf_counter() - t)
        if finished:
            self.store.add_decision(state["sim_minute"], "finished", "scenario finished; exiting 0")
            return True, stats
        if hit_stop:
            self.store.add_decision(state["sim_minute"], "stopped",
                                    f"stop_at_minute {self.stop_at_minute} reached")
            return True, stats
        if self._advance(state["sim_minute"], step):
            self.store.add_decision(state["sim_minute"], "finished",
                                    "advance reported finished; exiting 0")
            self._last_state = {**state, "finished": True}
            self._saw_finished = True
            return True, stats + self.ingestor.sync()  # drain what the final advance produced
        return False, stats

    def _record_observer_phase(self, seconds: float) -> None:
        """Add the observer's cost to this tick's tick_phases row (written just before it ran)."""
        try:
            self.store.conn.execute(
                "UPDATE tick_phases SET observer_s=? WHERE id=(SELECT MAX(id) FROM tick_phases)", (seconds,))
        except Exception:  # noqa: BLE001  (load analysis only, never breaks the loop)
            log.debug("observer phase not recorded", exc_info=True)

    def _record_metrics(self, minute: int, stats: IngestStats, t0: float, step: int) -> None:
        now = time.perf_counter()
        lag = max(0.0, time.monotonic() - self._advance_returned)
        self.store.add_metric(minute, stats.events, stats.wall_s, lag, now - t0, step)

    def _advance(self, minute: int, step: int) -> bool:
        """Advance; returns True if the sim reported ``finished``."""
        self.store.add_decision(minute, "advance_intent", f"advance {step} min from {minute}",
                                inputs={"target": minute + step})
        try:
            result = self.client.advance(step)
        except Finished:
            log.info("advance reported finished")
            return True
        if result.get("recovered"):
            self.store.add_decision(minute, "advance_recovered",
                                    "advance transport error; sim_minute had moved")
        self._advance_returned = time.monotonic()
        crash_point("after-advance-before-ingest")
        return False

    # ------------------------------------------------------------ run
    def run(self) -> RunResult:
        self.startup()
        ticks, failures = 0, 0
        total = IngestStats()
        while True:
            try:
                done, stats = self.tick()
            except AdvanceNotApplied as exc:
                failures += 1
                self.store.add_alert(None, "error", "advance_failed", str(exc))
                if failures >= _MAX_ADVANCE_FAILURES:
                    raise
                time.sleep(settings.RETRY_BASE_DELAY_S)
                continue
            failures = 0
            ticks += 1
            if TICK_SLEEP_S:
                time.sleep(TICK_SLEEP_S)
            total += stats
            if done:
                final = self.client.state()  # one read for both fields
                return RunResult(final["finished"] or self._saw_finished, ticks, final["sim_minute"], total)
