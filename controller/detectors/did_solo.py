"""A1: own-history step test for a DID that has no usable peers (CONTEXT C5 for single-DID workflows).

With one active caller ID there is no peer to cancel common shocks, so the DID is compared with its own
past: connects in the recent window against the connects EXPECTED if it kept its reference-period pickup
at every (hour-of-day, cell), adjusted for the fatigue curve's predicted change given its load trajectory.
The hour x cell baseline removes hour-of-day and lead-mix effects, the curve removes load effects, and
what is left is a step. Pure functions here; ``did.py`` feeds them and applies persistence.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

from .. import settings
from ..stats import RatioDrop, norm_sf
from .did_eval import StepResult

Rows = Mapping[tuple[int, int], tuple[int, int]]  # (abs_hour, cell) -> (dials, connects)


def _reference_rates(rows: Rows, lo: int, hi: int) -> tuple[dict, dict, float]:
    """(hour_of_day, cell) -> [dials, connects]; hour_of_day -> [dials, connects]; overall pickup, over [lo, hi)."""
    cell_hod: dict[tuple[int, int], list[int]] = {}
    hod: dict[int, list[int]] = {}
    n_all = x_all = 0
    for (h, cell), (n, x) in rows.items():
        if not lo <= h < hi:
            continue
        a = cell_hod.setdefault((h % 24, cell), [0, 0])
        b = hod.setdefault(h % 24, [0, 0])
        a[0] += n
        a[1] += x
        b[0] += n
        b[1] += x
        n_all += n
        x_all += x
    return cell_hod, hod, (x_all / n_all if n_all else 0.0)


def expected_connects(rows: Rows, win: tuple[int, int], ref: tuple[int, int]) -> tuple[float, int, int, int, int]:
    """(expected connects, window dials, window connects, reference dials, reference connects).

    Window dials in an hour-of-day the reference never saw are left out of both sides (no baseline).
    """
    cell_hod, hod, overall = _reference_rates(rows, *ref)
    m = settings.DID_SOLO_SHRINK_N
    exp, n_w, x_w = 0.0, 0, 0
    for (h, cell), (n, x) in rows.items():
        if not win[0] <= h < win[1] or (h % 24) not in hod:
            continue
        hn, hx = hod[h % 24]
        rate_h = hx / hn if hn else overall
        cn, cx = cell_hod.get((h % 24, cell), (0, 0))
        exp += n * (cx + m * rate_h) / (cn + m)
        n_w += n
        x_w += x
    n_r = sum(v[0] for v in hod.values())
    x_r = sum(v[1] for v in hod.values())
    return exp, n_w, x_w, n_r, x_r


def solo_test(rows: Rows, did: str, cur_hour: int, k: int, fatigue_ratio: float) -> StepResult | None:
    """Step test of the last ``k`` hours against the preceding reference span, or None when too thin."""
    win = (cur_hour - k + 1, cur_hour + 1)
    ref = (win[0] - settings.DID_SOLO_REF_HOURS, win[0])
    exp, n_w, x_w, n_r, x_r = expected_connects(rows, win, ref)
    m = settings.DID_FLAG_MIN_DIALS
    exp *= fatigue_ratio
    if min(n_w, n_r) < m or exp <= 0 or x_r <= 0:
        return None
    ratio = max(x_w, 0.5) / exp
    p_w, p_r = max(x_w, 0.5) / n_w, x_r / n_r
    se = math.sqrt((1 - min(p_w, 0.99)) / max(x_w, 0.5) + (1 - p_r) / x_r)
    z = math.log(ratio) / se
    test = RatioDrop(1.0 - ratio, z, norm_sf(-z), x_w / n_w, exp / n_w)
    return StepResult(did, k, n_w, 0, n_r, test, 1.0, 1.0, solo=True)


def solo_steps(rows: Rows, did: str, cur_hour: int, fatigue_ratio_for: "callable",
               windows: Sequence[int] = settings.DID_FLAG_WINDOWS_HOURS) -> list[StepResult]:
    out = []
    for k in windows:
        res = solo_test(rows, did, cur_hour, k, fatigue_ratio_for(k))
        if res is not None:
            out.append(res)
    return out


def fatigue_ratio(series, curve, loads, did: str, win: range, ref: range) -> float:
    """h(window mean load) / h(reference mean load): the change the fatigue curve already predicts."""
    from .did_eval import _mean_h
    hw, _, nw = _mean_h(curve, loads, series, [did], win)
    hr, _, nr = _mean_h(curve, loads, series, [did], ref)
    return hw / hr if nw and nr and hr > 0 else 1.0


def load_rows(store, wid: str, did: str, cur_hour: int) -> dict:
    """(abs_hour, cell) -> (dials, connects) of one DID from the cube, for the test's days."""
    from .. import cube
    day_min = cur_hour // 24 - settings.DID_SOLO_DAYS_BACK
    return {(d * 24 + h, cell): (r.dials, r.connects)
            for r in cube.rollup(store, wid, by=("day", "hour", "cell"), day_min=day_min, variant={"caller_id": did})
            for d, h, cell in [r.key]}


def peerless_steps(store, wid: str, series, curve, cur_hour: int, dids: Sequence[str]) -> list[StepResult]:
    """Own-history steps for the DIDs that carry traffic but have no peer step results."""
    from .did_eval import _hour_loads
    if not dids:
        return []
    span = settings.DID_SOLO_REF_HOURS + max(settings.DID_FLAG_WINDOWS_HOURS) + 1
    loads = _hour_loads(series, curve, cur_hour - span, cur_hour)
    out: list[StepResult] = []
    for did in dids:
        rows = load_rows(store, wid, did, cur_hour)
        if not rows:
            continue

        def ratio(k: int, did=did) -> float:
            win = range(cur_hour - k + 1, cur_hour + 1)
            return fatigue_ratio(series, curve, loads, did, win,
                                 range(win.start - settings.DID_SOLO_REF_HOURS, win.start))
        out += solo_steps(rows, did, cur_hour, ratio)
    return out
