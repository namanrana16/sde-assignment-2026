"""Desired changes and the registry later phases plug into.

Producers (DID health, hallucination incidents, experiments, schedule refresh) are pure
functions of the data: each tick they return the changes they *currently* want, expressed as
patches against the current config (see ``configlib`` for the patch format). Once a change has
been applied, the producer stops returning it. That makes re-planning after a rejection,
expiry or stale base automatic.

Detectors run before the planner each tick and may persist incidents/state; they return None.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Iterable


class Priority(IntEnum):
    """Lower value = more urgent (CONTEXT P2: incident > severe harm-stop/rollback > daily bundle)."""

    INCIDENT = 0
    SEVERE = 1
    ROUTINE = 2


log = logging.getLogger(__name__)
ROUTE_AUTO = "auto"
ROUTE_APPROVAL = "approval"


@dataclass(frozen=True)
class DesiredChange:
    workflow: str
    kind: str  # incident | harm_stop | rollback | daily_bundle | did_evacuation ...
    priority: int
    patch: dict
    route_preference: str = ROUTE_APPROVAL  # auto | approval
    evidence: dict = field(default_factory=dict)  # validation_run_id, flag_evacuation, rationale, ...
    summary: str = ""

    def __post_init__(self) -> None:
        if self.route_preference not in (ROUTE_AUTO, ROUTE_APPROVAL):
            raise ValueError(f"bad route_preference {self.route_preference!r}")


@dataclass
class PlanContext:
    """Read access handed to detectors and producers."""

    store: Any
    client: Any
    workflows: dict[str, dict]
    now: int  # sim minute

    @property
    def day(self) -> int:
        return self.now // 1440


Producer = Callable[[PlanContext, str], Iterable[DesiredChange]]
Detector = Callable[[PlanContext], None]
Veto = Callable[[PlanContext, str, str], "str | None"]  # (ctx, workflow_id, proposal_kind) -> reason to withhold /apply


# A veto hook that crashes fails SAFE: routine changes are withheld (retried next tick), but a change whose kind
# marks a safety/harm-reducing action is never blocked by a broken hook.
FAIL_OPEN_MARKERS = ("harm_stop", "rollback", "evacuation", "halluc", "incident", "revert")


class VetoCrash(str):
    """A veto reason that means the hook itself crashed (not a real stale-evidence veto): the approval is kept."""


def _report_failure(ctx: PlanContext, code: str, name: str, wid: str | None, exc: Exception) -> None:
    """Log with traceback, write one alert + decision per sim day per (name, workflow). Never raises."""
    log.exception("%s: %s on %s", code, name, wid)
    try:
        from .detectors.common import note_once

        if note_once(ctx.store, f"{code}:{name}:{wid}", ctx.now):
            msg = f"{name} raised {type(exc).__name__}: {exc}"
            ctx.store.add_alert(ctx.now, "warn", code, msg, wid)
            ctx.store.add_decision(ctx.now, code, msg, wid, {"name": name, "error": type(exc).__name__})
    except Exception:  # noqa: BLE001 - reporting must never take the tick down
        log.exception("could not record %s", code)


def _name(fn: Callable) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)


class Registry:
    def __init__(self) -> None:
        self.producers: list[Producer] = []
        self.detectors: list[Detector] = []
        self.vetoes: list[Veto] = []

    def register_producer(self, fn: Producer) -> Producer:
        self.producers.append(fn)
        return fn

    def register_detector(self, fn: Detector) -> Detector:
        self.detectors.append(fn)
        return fn

    def register_veto(self, fn: Veto) -> Veto:
        self.vetoes.append(fn)
        return fn

    def veto(self, ctx: PlanContext, workflow_id: str, kind: str) -> str | None:
        """First reason any producer gives to withhold applying an approved change (the evidence went stale)."""
        for fn in self.vetoes:
            try:
                reason = fn(ctx, workflow_id, kind)
            except Exception as exc:  # noqa: BLE001
                _report_failure(ctx, "veto_failed", _name(fn), workflow_id, exc)
                if any(m in kind for m in FAIL_OPEN_MARKERS):
                    continue
                return VetoCrash(f"apply-time check {_name(fn)} failed ({type(exc).__name__}); "
                                 "routine change withheld until it recovers")
            if reason:
                return reason
        return None

    def clear(self) -> None:
        self.producers.clear()
        self.detectors.clear()
        self.vetoes.clear()

    def run_detectors(self, ctx: PlanContext) -> None:
        for detector in self.detectors:
            try:
                detector(ctx)
            except Exception as exc:  # noqa: BLE001
                _report_failure(ctx, "detector_failed", _name(detector), None, exc)

    def collect(self, ctx: PlanContext, workflow_id: str) -> list[DesiredChange]:
        out: list[DesiredChange] = []
        for producer in self.producers:
            try:
                out.extend(c for c in producer(ctx, workflow_id) if c.workflow == workflow_id)
            except Exception as exc:  # noqa: BLE001
                _report_failure(ctx, "producer_failed", _name(producer), workflow_id, exc)
        return sorted(out, key=lambda c: (c.priority, c.kind, c.summary))


DEFAULT_REGISTRY = Registry()  # later phases register here; empty = the controller changes nothing
