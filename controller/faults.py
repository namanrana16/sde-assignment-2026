"""Fault injection for crash-recovery tests (CONTEXT C13).

``CRASH_AT=<point>:<n>`` makes the n-th hit (default 1) of the named point call
``os._exit`` so no ``finally``/``atexit`` handler runs, exactly like ``kill -9``.
Points: mid-ingest-txn, after-submit-before-record, after-apply-before-record,
after-advance-before-ingest, after-credit-call-before-record, before-apply-request
(between the apply_intent log and the /apply POST).
"""
from __future__ import annotations

import os
from typing import Callable

POINTS = ("mid-ingest-txn", "after-submit-before-record", "after-apply-before-record",
          "after-advance-before-ingest",
          "after-credit-call-before-record", "before-apply-request")
CRASH_EXIT_CODE = 137

_hits: dict[str, int] = {}
_exit: Callable[[int], None] = os._exit


def parse_spec(spec: str | None) -> tuple[str, int] | None:
    if not spec:
        return None
    point, _, count = spec.partition(":")
    return point.strip(), int(count) if count.strip() else 1


def crash_point(name: str, spec: str | None = None) -> None:
    """Exit hard if the configured ``CRASH_AT`` names this point and its count is reached."""
    parsed = parse_spec(spec if spec is not None else os.environ.get("CRASH_AT"))
    if parsed is None or parsed[0] != name:
        return
    _hits[name] = _hits.get(name, 0) + 1
    if _hits[name] == parsed[1]:
        _exit(CRASH_EXIT_CODE)


def reset(exit_fn: Callable[[int], None] | None = None) -> None:
    """Test helper: clear hit counters and optionally replace ``os._exit``."""
    global _exit
    _hits.clear()
    _exit = exit_fn or os._exit
