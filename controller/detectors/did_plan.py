"""Pure allocation planning for caller-ID health: probe cap, target split, bounded steps."""
from __future__ import annotations

from typing import Mapping

from .. import settings

TESTED, UNTESTED, SUSPECT, FLAGGED, WEAK = "tested", "untested", "suspect", "flagged", "weak"
REPROBE = "reprobe"  # an expired-flag DID being re-tested at the (smaller) re-probe cap
BAD = (FLAGGED, WEAK)
_EPS = 1e-12


def probe_cap(h_bar: float, base_step: float | None = None) -> float:
    """Largest share ``s`` for an untested DID with the worst-case DID-health step above -2%.

    ``s <= base_step * h_bar / (h_bar - h_floor)``: if the probe is as bad as ``h_floor`` the expected
    health moves by ``s * (h_bar - h_floor) / h_bar`` <= base_step. h_floor is a documented prior. When
    h_bar <= h_floor the formula diverges (the fleet is already as unhealthy as the worst case, so a probe
    cannot make it worse): clamp to ``DID_PROBE_MAX_SHARE``, which also bounds every other result.
    """
    step = settings.DID_PROBE_BASE_STEP if base_step is None else base_step
    if h_bar <= settings.DID_H_FLOOR:
        return settings.DID_PROBE_MAX_SHARE
    return min(settings.DID_PROBE_MAX_SHARE, step * h_bar / (h_bar - settings.DID_H_FLOOR))


def reprobe_cap(h_bar: float) -> float:
    return probe_cap(h_bar, settings.DID_REPROBE_HARM_BUDGET)


def tv(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in sorted(set(a) | set(b)))


def _clean(alloc: Mapping[str, float]) -> dict[str, float]:
    total = sum(v for v in alloc.values() if v > _EPS)
    return {k: v / total for k, v in alloc.items() if v > _EPS} if total > 0 else {}


def _evacuation_target(current: Mapping[str, float], status: Mapping[str, str], h_bar: float) -> dict[str, float]:
    """Move every bad DID's share onto tested-healthy DIDs; untested DIDs only when no tested one exists.

    Non-bad DIDs that already hold traffic (an untested probe, a suspect) keep what they have. The evacuated
    mass goes to the tested DIDs evenly; with no tested DID it goes to untested ones, each capped at
    max(probe cap, the evacuated share needed), and only then to suspects.
    """
    options = list(status)
    bad_mass = sum(current.get(d, 0.0) for d in options if status[d] in BAD)
    tested = [d for d in options if status[d] == TESTED]
    untested = [d for d in options if status[d] in (UNTESTED, REPROBE)]
    suspects = [d for d in options if status[d] == SUSPECT]
    keep = {d: current.get(d, 0.0) for d in untested + suspects if current.get(d, 0.0) > _EPS}
    if tested:
        rest = max(1.0 - sum(keep.values()), 0.0)
        return _clean({**keep, **{d: rest / len(tested) for d in tested}})
    if untested:
        cap = max(probe_cap(h_bar), bad_mass)
        out = dict(keep)
        for d in untested:  # evenly, each at most `cap` in total
            out[d] = min(out.get(d, 0.0) + bad_mass / len(untested), cap)
        return _clean(out)
    if suspects:
        return _clean({d: 1.0 for d in suspects})
    return _clean(current)


def target_allocation(current: Mapping[str, float], status: Mapping[str, str], h_bar: float
                      ) -> dict[str, float]:
    """Desired caller-ID split: even over non-bad DIDs, one untested DID probed at the cap.

    While a bad (flagged/weak) DID still holds traffic this is an evacuation (see ``_evacuation_target``).
    """
    options = list(status)
    if any(current.get(d, 0.0) > _EPS for d in options if status[d] in BAD):
        return _evacuation_target(current, status, h_bar)
    tested = [d for d in options if status[d] == TESTED]
    untested = [d for d in options if status[d] == UNTESTED]
    reprobe = [d for d in options if status[d] == REPROBE]
    suspects = [d for d in options if status[d] == SUSPECT]
    if not tested:
        return _clean(current)  # nothing is known to be healthy: change nothing
    cap = probe_cap(h_bar)
    out: dict[str, float] = {}
    for d in suspects:  # never grow a DID that looks worse than its peers
        out[d] = min(current.get(d, 0.0), cap)
    probing = (untested[:1] or reprobe[:1])  # ONE probing DID may gain traffic per change: harm does not stack
    for d in reprobe:  # a re-probe stays within a quarter of the harm line; a waiting one never grows
        out[d] = min(reprobe_cap(h_bar), cap) if d in probing else min(current.get(d, 0.0), reprobe_cap(h_bar), cap)
    if untested:
        out[untested[0]] = cap
    rest = max(1.0 - sum(out.values()), 0.0)
    for d in tested:
        out[d] = rest / len(tested)
    return _clean(out)


def plan_step(current: Mapping[str, float], target: Mapping[str, float], max_shift: float | None,
              status: Mapping[str, str]) -> dict[str, float]:
    """One bounded move from ``current`` towards ``target`` with TV <= max_shift * safety.

    Traffic leaves bad DIDs first; it lands on tested-healthy DIDs before untested ones.
    """
    cur, tgt = _clean(current), _clean(target)
    if max_shift is None:
        return tgt
    budget = max_shift * settings.DID_STEP_SAFETY
    if tv(cur, tgt) <= budget + _EPS:
        return tgt
    keys = sorted(set(cur) | set(tgt))
    out = {k: cur.get(k, 0.0) for k in keys}
    sources = sorted((k for k in keys if cur.get(k, 0.0) > tgt.get(k, 0.0) + _EPS),
                     key=lambda k: (status.get(k) not in BAD, k))
    sinks = sorted((k for k in keys if tgt.get(k, 0.0) > cur.get(k, 0.0) + _EPS),
                   key=lambda k: (status.get(k) != TESTED, k))
    left = budget
    for k in sources:
        take = min(cur[k] - tgt.get(k, 0.0), left)
        out[k] -= take
        left -= take
    left = budget
    for k in sinks:
        put = min(tgt[k] - cur.get(k, 0.0), left)
        out[k] += put
        left -= put
    return _clean(out)


def evacuation_impact(moves, deficits: Mapping[str, float]) -> float:
    """Impact of a safety move: traffic share moved off flagged DIDs x their relative pickup deficit, on the
    same scale as ``dryrun.impact_score`` (a 10% workflow-wide relative gain scores 1.0). ``moves`` is a list of
    ``(current, step)`` caller_id splits, one per allocation key; the largest key counts (no cohort weights here).
    """
    from ..dryrun import IMPACT_FULL_LIFT
    best = 0.0
    for cur, step in moves:
        gain = sum(max(0.0, cur.get(d, 0.0) - step.get(d, 0.0)) * deficit for d, deficit in deficits.items())
        best = max(best, gain)
    return round(min(1.0, best / IMPACT_FULL_LIFT), 4)


def expected_health(ev, alloc: Mapping[str, float], quality: Mapping[str, float]) -> float:
    """Expected traffic-weighted pickup health of ``alloc``: per-DID observed pickup ratio at comparable load
    (``quality``; an unknown DID gets the conservative prior ``DID_H_FLOOR``, as the probe cap assumes) times the fatigue-curve health at the load the DID would carry."""
    total = sum(alloc.values()) or 1.0
    load = sum(ev.loads_now.values())
    return sum((a / total) * quality.get(d, settings.DID_H_FLOOR) * ev.curve.h(load * a / total) for d, a in alloc.items())
