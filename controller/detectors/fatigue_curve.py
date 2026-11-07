"""Learned DID load-fatigue curve (CONTEXT C5): pickup vs trailing decayed dial volume.

No functional form is assumed: a non-increasing isotonic fit of pickup on each DID's trailing
decayed dial volume, with the decay window chosen from a small grid by an AIC-style criterion.
History starts at zero load, so the ramp identifies the curve; with too little data the curve is
flat (no fatigue is assumed or corrected for).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .. import settings
from ..stats import isotonic_decreasing

Obs = tuple[int, str, int, int]  # (abs_hour, did, dials, connects)
MIN_DIALS_PER_POINT = 50
TIE_TOLERANCE = 1.0  # AIC units: fits this close are indistinguishable


@dataclass(frozen=True)
class FatigueCurve:
    tau_hours: float
    xs: tuple[float, ...] = ()
    hs: tuple[float, ...] = ()
    fitted: bool = False
    n_obs: int = 0

    @classmethod
    def flat(cls) -> "FatigueCurve":
        return cls(tau_hours=24.0 * 2)

    def h(self, load: float) -> float:
        """Relative health in (0, 1]: 1 at (or below) the lightest observed load."""
        if not self.fitted or not self.xs or load <= self.xs[0]:
            return 1.0
        if load >= self.xs[-1]:
            return self.hs[-1]
        for i in range(1, len(self.xs)):
            if load <= self.xs[i]:
                f = (load - self.xs[i - 1]) / max(self.xs[i] - self.xs[i - 1], 1e-12)
                return self.hs[i - 1] + f * (self.hs[i] - self.hs[i - 1])
        return self.hs[-1]

    def to_json(self) -> str:
        return json.dumps({"tau_hours": self.tau_hours, "xs": list(self.xs), "hs": list(self.hs),
                           "fitted": self.fitted, "n_obs": self.n_obs})

    @classmethod
    def from_json(cls, text: str) -> "FatigueCurve":
        d = json.loads(text)
        return cls(d["tau_hours"], tuple(d["xs"]), tuple(d["hs"]), d["fitted"], d.get("n_obs", 0))

    def params(self) -> dict:
        return {"tau_hours": self.tau_hours, "fitted": self.fitted, "n_obs": self.n_obs,
                "xs": [round(x, 1) for x in self.xs], "hs": [round(h, 4) for h in self.hs]}


def decayed_loads(dials: Mapping[int, Mapping[str, int]], tau_hours: float, hours: Iterable[int]
                  ) -> dict[int, dict[str, float]]:
    """Trailing decayed dial volume of each DID at the START of every requested hour.

    ``L[h+1] = (L[h] + dials[h]) * exp(-1/tau)``, run over every hour (also hours without dials).
    """
    wanted = list(hours)
    if not wanted:
        return {}
    lo = min(min(dials, default=wanted[0]), min(wanted))
    hi = max(wanted)
    dids = sorted({d for row in dials.values() for d in row})
    k = math.exp(-1.0 / tau_hours)
    load = {d: 0.0 for d in dids}
    out: dict[int, dict[str, float]] = {}
    want = set(wanted)
    for h in range(lo, hi + 1):
        if h in want:
            out[h] = dict(load)
        row = dials.get(h, {})
        load = {d: (load[d] + row.get(d, 0)) * k for d in dids}
    return out


def _points(obs: Sequence[Obs], loads: Mapping[int, Mapping[str, float]]) -> list[tuple[float, float, float]]:
    """(x, y, w) per (DID, day): dial-weighted mean load and the pickup of that DID-day."""
    acc: dict[tuple[str, int], list[float]] = {}
    for hour, did, n, x in obs:
        if n <= 0:
            continue
        a = acc.setdefault((did, hour // 24), [0.0, 0.0, 0.0])
        a[0] += n * loads[hour][did]
        a[1] += x
        a[2] += n
    return [(a[0] / a[2], a[1] / a[2], a[2]) for a in acc.values() if a[2] >= MIN_DIALS_PER_POINT]


def _criterion(points: list[tuple[float, float, float]]) -> tuple[float, list]:
    xs, ys, ws = zip(*points)
    mean_w = sum(ws) / len(ws)
    wn = [w / mean_w for w in ws]
    blocks = isotonic_decreasing(xs, ys, wn)
    fit = []
    for x in xs:
        level = blocks[-1][1]
        for bx, bl, _ in blocks:
            if x <= bx + 1e-12:
                level = bl
                break
        fit.append(level)
    sse = sum(w * (y - f) ** 2 for w, y, f in zip(wn, ys, fit)) + 1e-12
    n = len(points)
    return n * math.log(sse / n) + 2 * len(blocks), blocks


def fit_curve(obs: Sequence[Obs], taus_days: Sequence[float] | None = None) -> FatigueCurve:
    """Fit the curve from hourly per-DID observations ``(abs_hour, did, dials, connects)``."""
    taus = [d * 24.0 for d in (taus_days or settings.DID_FATIGUE_TAU_GRID_DAYS)]
    if not obs:
        return FatigueCurve.flat()
    dials: dict[int, dict[str, int]] = {}
    for hour, did, n, _ in obs:
        dials.setdefault(hour, {})[did] = dials.get(hour, {}).get(did, 0) + n
    hours = sorted({o[0] for o in obs})
    fits: list[tuple[float, float, list]] = []
    n_points = 0
    for tau in taus:
        pts = _points(obs, decayed_loads(dials, tau, hours))
        n_points = len(pts)
        if len(pts) < settings.DID_CURVE_MIN_OBS or max(p[0] for p in pts) - min(p[0] for p in pts) <= 1e-9:
            continue
        crit, blocks = _criterion(pts)
        fits.append((crit, tau, blocks))
    if not fits:
        return FatigueCurve(taus[len(taus) // 2], n_obs=n_points)
    # While every DID ramps up together the fit only sees the ORDER of the loads, which is the same
    # for every decay window: the window is then unidentified, and we take the median of the tied
    # grid values (no information -> middle of the grid). Diverging loads in live data break ties.
    best_crit = min(f[0] for f in fits)
    tied = [f for f in fits if f[0] <= best_crit + TIE_TOLERANCE]
    _, tau, blocks = tied[len(tied) // 2]
    top = blocks[0][1]
    if top <= 0:
        return FatigueCurve(tau, n_obs=n_points)
    xs = tuple(b[0] for b in blocks)
    hs = tuple(max(min(b[1] / top, 1.0), 0.05) for b in blocks)
    return FatigueCurve(tau, xs, hs, True, n_points)
