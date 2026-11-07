"""Approver fatigue ledger and filing-time rules (CONTEXT P4/P5).

* The sim counts requests by ``created_at`` over a rolling 24 h, rejected and expired ones
  included. Slot tiers: the LAST slot is reserved for safety incidents (hallucination fix, flagged-DID
  evacuation); harm-stop/rollback may use the second-to-last; routine bundles get the rest
  (``max(1, total - 2)``). A rollback never blocks a safety fix. With a budget of ONE request per 24 h
  routine and severe work may use that single slot (otherwise the controller could never act except
  for incidents); the cost is that an incident right after a routine filing waits up to 24 h.
* ``settings.SOFT_CAPS`` (by workflow id) and ``SOFT_CAPS_BY_TENANT`` add soft preferences for routine requests; a cap that would
  leave no filing opportunity is ignored (``soft_cap_ignored``).
* A request is filed only inside approver hours, and only if the probability that the decision lands
  before ``expires_at`` (and is seen inside the apply window) is high enough (``decision_probability``):
  routine >= FILE_MIN_P_ROUTINE, urgent (incident/severe) >= FILE_MIN_P_URGENT, or, when no minute of
  the day reaches that, the day's best minutes if their P >= the matching FLOOR (``routine_need``,
  ``urgent_need``). Latency is uniform on [lo, hi].
"""
from __future__ import annotations

from functools import lru_cache
from typing import Mapping

from . import settings
from .desired import Priority

DAY = settings.MINUTES_PER_DAY


def in_hours(minute: int, hours: Mapping | list) -> bool:
    """Is ``minute`` inside the approver window? ``[0, 24]`` is always open; ``lo > hi`` wraps midnight."""
    lo, hi = hours
    mod = minute % DAY
    if lo <= hi:
        return lo * 60 <= mod < hi * 60
    return mod >= lo * 60 or mod < hi * 60


def next_working(t: int, hours: list) -> int:
    """First approver-working minute at or after ``t``: EXACTLY the sim's ``_next_working``
    (including its behaviour for a wrapped ``lo > hi`` window, which it does not special-case)."""
    lo, hi = hours
    day, mod = divmod(t, DAY)
    if mod < lo * 60:
        return day * DAY + lo * 60
    if mod >= hi * 60:
        return (day + 1) * DAY + lo * 60
    return t


def ttl_minutes(gov: Mapping, expires_in_hours: float | None = None) -> int:
    ttl = gov["proposal_ttl_hours"]
    return int(min(float(expires_in_hours or ttl), ttl) * 60)


def worst_case_decision(now: int, gov: Mapping) -> int:
    return next_working(now + gov["approver_latency_minutes"][1], gov["approver_hours"])


@lru_cache(maxsize=4096)
def _p_cached(tod: int, ttl_min: int, lo: int, hi: int, hours: tuple) -> float:
    ok = sum(1 for u in range(lo, hi + 1) if next_working(tod + u, list(hours)) <= tod + ttl_min)
    return ok / (hi - lo + 1)


def apply_window_factor(gov: Mapping, tick: int = settings.TICK_MINUTES) -> float:
    """P(the controller's first tick at or after the decision still falls inside the apply window).

    The sim decides at an integer minute D, we see it at the next tick boundary (a delay of 0..tick-1
    minutes, uniform) and ``apply_approved`` refuses once ``now > D + window``. A window of at least
    ``tick - 1`` minutes always suffices (factor 1: every public policy). Unknown window: 1."""
    hours = gov.get("apply_window_hours")
    if hours is None or tick <= 1:
        return 1.0
    return min(tick, int(float(hours) * 60) + 1) / tick


def apply_window_unreachable(gov: Mapping, tick: int = settings.TICK_MINUTES) -> bool:
    """Is the apply window so short against the tick that no approval could be applied in time?"""
    return apply_window_factor(gov, tick) < settings.FILE_MIN_P_URGENT_FLOOR


def decision_probability(now: int, gov: Mapping, expires_in_hours: float | None = None,
                         tick: int = settings.TICK_MINUTES) -> float:
    """P(decision <= expires_at AND still applicable) for a request filed at ``now``: the sim draws
    latency uniformly on [lo, hi] (inclusive, whole minutes), decides at next_working(now + U) and
    expires it if that is after expires_at; the decision must also be seen within the apply window
    (``apply_window_factor``). The expiry part depends only on the minute of day, so it is cached."""
    lo, hi = sorted(int(x) for x in gov["approver_latency_minutes"])
    p = _p_cached(now % DAY, ttl_minutes(gov, expires_in_hours), lo, hi, tuple(gov["approver_hours"]))
    return p * apply_window_factor(gov, tick)


def best_daily_probability(gov: Mapping, expires_in_hours: float | None = None,
                           tick: int = settings.TICK_MINUTES) -> float:
    """Highest decision_probability over the in-hours minutes of a day (15-minute grid)."""
    hours = gov["approver_hours"]
    ps = [decision_probability(m, gov, expires_in_hours, tick) for m in range(0, DAY, 15)
          if in_hours(m, hours)]
    return max(ps, default=0.0)


def _best_minutes_need(best: float, normal: float, floor: float) -> float:
    """``normal`` when some minute reaches it; else 95% of the day's best if that best clears ``floor``."""
    if best >= normal or best < floor:
        return normal  # reachable, or hopeless: every request would expire
    return max(floor, 0.95 * best)


def urgent_need(gov: Mapping, expires_in_hours: float | None = None,
                tick: int = settings.TICK_MINUTES) -> float:
    """P an incident/severe request needs: FILE_MIN_P_URGENT, unless no minute of the day reaches it
    (short ttl, long latency). Then file at the day's best minutes (>= 95% of the best P) if that best
    is at least FILE_MIN_P_URGENT_FLOOR: a likely-expiring request costs a slot and a hygiene count,
    but never filing guarantees the safety fix never lands."""
    return _best_minutes_need(best_daily_probability(gov, expires_in_hours, tick),
                              settings.FILE_MIN_P_URGENT, settings.FILE_MIN_P_URGENT_FLOOR)


def routine_need(gov: Mapping, expires_in_hours: float | None = None,
                 tick: int = settings.TICK_MINUTES) -> float:
    """P a routine request needs: FILE_MIN_P_ROUTINE, unless no minute reaches it. Then the day's best
    minutes if their P is at least FILE_MIN_P_ROUTINE_FLOOR (below it the expected waste, a slot and an
    'expired' hygiene count, exceeds the value of the bundle; see docs/THRESHOLDS.md)."""
    return _best_minutes_need(best_daily_probability(gov, expires_in_hours, tick),
                              settings.FILE_MIN_P_ROUTINE, settings.FILE_MIN_P_ROUTINE_FLOOR)


def soft_cap(workflow_id: str, tenant_id: str | None = None) -> dict:
    """The workflow's soft cap: a per-id override, else its tenant's, else none."""
    return settings.SOFT_CAPS.get(workflow_id) or settings.SOFT_CAPS_BY_TENANT.get(tenant_id or "") or {}


def soft_cap_ignored(gov: Mapping, workflow_id: str, expires_in_hours: float | None = None,
                     tenant_id: str | None = None, tick: int = settings.TICK_MINUTES) -> str | None:
    """Why the workflow's soft cap cannot be honoured (``None`` = it can, or there is none)."""
    soft = soft_cap(workflow_id, tenant_id)
    if not soft:
        return None
    lo, hi = gov["approver_hours"]
    nb = soft.get("not_before_hour")
    if nb is not None:
        if nb >= hi:
            return f"not_before_hour {nb} is not before approver close {hi}"
        first = max(nb * 60, lo * 60)
        need = routine_need(gov, expires_in_hours, tick)
        if not any(in_hours(m, gov["approver_hours"]) and decision_probability(m, gov, expires_in_hours, tick)
                   >= need for m in range(first, hi * 60)):
            return f"no filing minute at or after {nb}:00 passes the expiry rule"
    if soft.get("max_per_day") is not None and soft["max_per_day"] < 1:
        return f"max_per_day {soft['max_per_day']} would forbid all routine filings"
    return None


def filing_window_ok(now: int, gov: Mapping, routine: bool, workflow_id: str,
                     expires_in_hours: float | None = None, *, tenant_id: str | None = None,
                     tick: int = settings.TICK_MINUTES) -> tuple[bool, str]:
    """May a request be filed right now? Returns ``(ok, reason_if_not)``."""
    if not in_hours(now, gov["approver_hours"]):
        return False, "outside approver hours"
    soft = soft_cap(workflow_id, tenant_id)
    if (routine and soft.get("not_before_hour") is not None
            and now % DAY < soft["not_before_hour"] * 60
            and soft_cap_ignored(gov, workflow_id, expires_in_hours, tenant_id, tick) is None):
        return False, f"soft cap: not before {soft['not_before_hour']}:00"
    need = routine_need(gov, expires_in_hours, tick) if routine else urgent_need(gov, expires_in_hours, tick)
    p = decision_probability(now, gov, expires_in_hours, tick)
    if p < need:
        return False, f"P(decision before expires_at)={p:.2f} < {need}"
    return True, ""


def routine_budget(gov: Mapping, workflow_id: str, tenant_id: str | None = None) -> int:
    """Routine bundles: all but the last two slots (at least one). With a total of 1 the single slot
    is usable (see module docstring); the reserved-last-slot rule then cannot hold."""
    total = gov["max_requests_per_24h"]
    if total <= 1:
        return max(0, total)
    budget = min(max(1, total - 2), total - 1)
    cap = soft_cap(workflow_id, tenant_id).get("max_per_day")
    if cap is not None and cap >= 1:
        budget = min(budget, cap)
    return budget


def severe_budget(gov: Mapping) -> int:
    """Harm-stop / rollback may use the second-to-last slot, never the last (but a total of 1 is shared)."""
    total = gov["max_requests_per_24h"]
    return max(0, total) if total <= 1 else total - 1


def slot_limit(gov: Mapping, priority: int, workflow_id: str, tenant_id: str | None = None) -> int:
    if priority <= Priority.INCIDENT:
        return gov["max_requests_per_24h"]  # the last slot is reserved for safety incidents
    if priority <= Priority.SEVERE:
        return severe_budget(gov)
    return routine_budget(gov, workflow_id, tenant_id)


def slot_available(used: int, gov: Mapping, priority: int, workflow_id: str,
                   tenant_id: str | None = None) -> tuple[bool, str]:
    limit = slot_limit(gov, priority, workflow_id, tenant_id)
    if used >= limit:
        return False, f"fatigue budget: {used} requests in 24h, limit {limit} for priority {int(priority)}" + (
            " (last slot reserved for safety incidents)" if priority > Priority.INCIDENT else "")
    return True, ""
