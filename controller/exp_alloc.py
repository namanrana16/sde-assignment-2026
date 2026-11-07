"""Allocation arithmetic of the C6 experiment lifecycle (pure; shared by the controller and the Monte Carlo)."""
from __future__ import annotations

from typing import Mapping, Sequence

from . import settings

_EPS = 1e-9


def entry_slots() -> int:
    return int((settings.EXP_MAX_CHALLENGER_TOTAL + _EPS) // settings.EXP_ENTRY_SHARE)


def entry_shares(challengers: Sequence[str]) -> dict[str, float]:
    """First challengers in catalogue order, each at the entry share, total <= the entry cap."""
    return {a: settings.EXP_ENTRY_SHARE for a in list(challengers)[:entry_slots()]}


def entry_plan(live: Mapping[str, float], control: str, options: Sequence[str],
               excluded: Sequence[str]) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Entry arms for a live default that may already split the dim: (shares, incumbents, untested).

    Options holding live share are INCUMBENTS: they keep their live share (entry never lowers one). New challengers
    (options with no live share) are carved from the control's share only, at the entry share each, at most the entry
    cap in total and never taking the control below EXP_MIN_CONTROL_SHARE (fewer, or none, when it cannot be met).
    A 100%-one-option default has no incumbents and gets exactly ``entry_shares``.
    """
    incumbents = {o: round(float(w), 6) for o, w in live.items() if o != control and w > _EPS and o in options}
    fresh = [o for o in options if o != control and o not in excluded and o not in incumbents]
    room = (float(live.get(control, 0.0)) - settings.EXP_MIN_CONTROL_SHARE + _EPS) // settings.EXP_ENTRY_SHARE
    slots = max(0, min(entry_slots(), int(room)))
    new = fresh[:slots]
    return ({**incumbents, **{a: settings.EXP_ENTRY_SHARE for a in new}}, incumbents, fresh[slots:])


def next_shares(shares: Mapping[str, float], ramp: Sequence[str], drop: Sequence[str],
                untested: Sequence[str] = (), floors: Mapping[str, float] | None = None) -> dict[str, float]:
    """Drop, ramp (to the ramp share, keeping control >= the floor) and rotate untested options in.

    ``floors``: shares an arm is never squeezed below by the proportional shrink (incumbents' live entry shares).
    """
    out = {a: s for a, s in shares.items() if a not in set(drop)}
    for arm in ramp:
        if arm in out:
            out[arm] = max(out[arm], settings.EXP_RAMP_SHARE)
    room = 1.0 - settings.EXP_MIN_CONTROL_SHARE
    total = sum(out.values())
    if total > room + _EPS:  # shrink the ramped arms proportionally (never below the entry share / an incumbent's floor)
        fixed = sum(s for a, s in out.items() if a not in ramp)
        scale = max(0.0, room - fixed) / (total - fixed) if floors and total > fixed else room / total
        out = {a: max((floors or {}).get(a, settings.EXP_ENTRY_SHARE), s * scale) if a in ramp else s for a, s in out.items()}
    for arm in untested:
        if sum(out.values()) + settings.EXP_ENTRY_SHARE <= settings.EXP_MAX_CHALLENGER_TOTAL + _EPS:
            out[arm] = settings.EXP_ENTRY_SHARE
    return out


def weights(control: str, shares: Mapping[str, float]) -> dict[str, float]:
    live = {a: float(s) for a, s in shares.items() if s > _EPS}
    return {control: round(1.0 - sum(live.values()), 6), **{a: round(s, 6) for a, s in live.items()}}


def promotion_weights(winner: str, old_control: str, stage: int = 2) -> dict[str, float]:
    """Stage 1: winner at EXP_STAGE1_SHARE against the old control; stage 2: winner 90% / holdout 10%."""
    share = settings.EXP_STAGE1_SHARE if stage == 1 else 1.0 - settings.EXP_HOLDOUT
    return {winner: round(share, 6), old_control: round(1.0 - share, 6)}
