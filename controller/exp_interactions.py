"""Report-only welcome x voice interaction table (never read by any decision).

Computed from the ``cube`` aggregate (no calls scan) over the experiment window. For every (welcome, voice) cell: connects,
engaged, rate, the rate expected if the two dims were purely additive (row rate + column rate - overall rate) and
z = (observed - expected) / sqrt(expected (1 - expected) / connects). ``chi2`` is the sum of z^2 over cells with
(r-1)(c-1) degrees of freedom. It is stored in the daily-look decision row only.
"""
from __future__ import annotations

import logging
import math
from typing import Mapping

from . import cube

log = logging.getLogger(__name__)
MIN_CELL_CONNECTS = 1  # cells with fewer connects are listed without a z


def summarise(cells: Mapping[tuple[str, str], tuple[float, float]]) -> dict:
    """``cells``: {(welcome, voice): (connects, engaged)} -> table + chi-square against additive main effects."""
    rows = sorted({w for w, _ in cells})
    cols = sorted({v for _, v in cells})
    n_tot = sum(n for n, _ in cells.values())
    x_tot = sum(x for _, x in cells.values())
    if n_tot <= 0:
        return {"table": [], "chi2": 0.0, "df": 0}
    p_all = x_tot / n_tot

    def rate(sel) -> float:
        n = sum(c[0] for k, c in cells.items() if sel(k))
        return sum(c[1] for k, c in cells.items() if sel(k)) / n if n else p_all

    row_rate = {w: rate(lambda k, w=w: k[0] == w) for w in rows}
    col_rate = {v: rate(lambda k, v=v: k[1] == v) for v in cols}
    table, chi2 = [], 0.0
    for (w, v), (n, x) in sorted(cells.items()):
        expected = min(1.0, max(0.0, row_rate[w] + col_rate[v] - p_all))
        z = None
        if n >= MIN_CELL_CONNECTS and 0.0 < expected < 1.0:
            z = (x / n - expected) / math.sqrt(expected * (1.0 - expected) / n)
            chi2 += z * z
        table.append({"welcome": w, "voice": v, "connects": int(n), "engaged": int(x), "rate": round(x / n, 4) if n else None,
                      "expected": round(expected, 4), "z": None if z is None else round(z, 3)})
    return {"table": table, "chi2": round(chi2, 3), "df": max(0, (len(rows) - 1) * (len(cols) - 1))}


def table(store, wid: str, start_minute: int, now: int) -> dict:
    """The 2-way table over the experiment window (whole days from the start day; a cheap cube aggregate)."""
    rows = cube.rollup(store, wid, by=("welcome", "voice"), day_min=start_minute // 1440, day_max=now // 1440)
    return summarise({(r.key[0], r.key[1]): (r.connects, r.engaged) for r in rows})


def safe_table(store, wid: str, start_minute: int, now: int) -> dict:
    """Never raises: the report must not be able to break (or steer) a look."""
    try:
        return table(store, wid, start_minute, now)
    except Exception as exc:  # report-only
        log.warning("interaction table failed for %s: %s", wid, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
