"""Registers the Phase 3 detectors/producers on the default registry (each behind its own flag)."""
from __future__ import annotations

from . import baselines, experiments, schedule, settings
from .desired import DEFAULT_REGISTRY, PlanContext, Registry
from .detectors import did, halluc, prompt_safety

_REGISTERED: set[int] = set()


def baselines_detector(ctx: PlanContext) -> None:
    if settings.ENABLE_DID or settings.ENABLE_HALLUC:
        baselines.ensure(ctx)


def register_defaults(registry: Registry = DEFAULT_REGISTRY) -> None:
    """Per-tick order: baselines, hallucination (Day-0 validation, incidents), caller-ID health."""
    if id(registry) in _REGISTERED:
        return
    _REGISTERED.add(id(registry))
    registry.register_detector(baselines_detector)
    halluc.register(registry)
    prompt_safety.register(registry)
    did.register(registry)
    schedule.register(registry)
    experiments.register(registry)


register_defaults()
