"""C4a model: per-cohort hour multipliers with shrinkage, weights tables, and outcome estimates.

Pure functions over *strata* rows ``(cell, hour, attempt_no, campaign_day, dials, connects, engaged)``.

* Multiplier of cohort c in hour h = observed connects / expected connects, where the expectation applies each
  (attempt_no, campaign_day) stratum's pickup rate (pooled over hours) to the dials made in that hour. This is an
  indirect standardisation: good responders conclude early, so later attempts / campaign days pick up less and are
  dialled in different hour mixes than early ones (survivorship); the strata absorb that.
* Shrinkage is empirical Bayes toward the workflow-wide multiplier: B = tau^2 / (tau^2 + v), with v = m / E the
  sampling variance of an O/E ratio and tau^2 the between-cohort variance estimated from the data itself.
* Weights are proportional to the shrunk multiplier, normalised to mean 1 over the dialling window and clipped to
  ``SCHEDULE_WEIGHT_CLIP``; EVERY in-window hour is present (an omitted hour would get weight 0 in the sim).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from . import settings

Row = tuple  # (cell, hour, attempt_no, campaign_day, dials, connects, engaged)
POOLED = "*"


@dataclass
class Model:
    hours: list[int]
    cohorts: list[str]
    mhat: dict[str, dict[int, float]]  # shrunk multiplier per cohort and hour
    pooled: dict[int, float]
    tau2: float
    level: dict[str, float]  # cohort mean pickup
    mass: dict[str, dict[int, float]]  # observed dials per cohort and hour
    eng: dict[str, dict[int, float]]  # shrunk engagement per cohort and hour
    detail: dict = field(default_factory=dict)


def _oe(rows: Sequence[Row], hours: Sequence[int]) -> tuple[dict, dict, dict, dict, float]:
    """(multiplier m_h, expected connects E_h, dials n_h, engaged/connect e_h, pickup level) of a row set."""
    strata: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for _, _, att, cd, n, x, _e in rows:
        s = strata[(att, cd)]
        s[0] += n
        s[1] += x
    rate = {k: v[1] / v[0] for k, v in strata.items() if v[0]}
    exp: dict[int, float] = defaultdict(float)
    obs: dict[int, float] = defaultdict(float)
    dials: dict[int, float] = defaultdict(float)
    eng: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for _, h, att, cd, n, x, e in rows:
        exp[h] += n * rate.get((att, cd), 0.0)
        obs[h] += x
        dials[h] += n
        eng[h][0] += e
        eng[h][1] += x
    n_all = sum(dials.values())
    level = sum(obs.values()) / n_all if n_all else 0.0
    mult = {h: obs[h] / exp[h] for h in hours if exp.get(h, 0) > 0}
    e_h = {h: eng[h][0] / eng[h][1] for h in hours if eng[h][1] > 0}
    return mult, dict(exp), dict(dials), e_h, level


def fit(rows: Sequence[Row], assign: Callable[[int], str], hours: Sequence[int]) -> Model:
    """Estimate the shrunk per-cohort multipliers from strata rows; ``assign`` maps a cell to its cohort id."""
    by_cohort: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_cohort[assign(r[0])].append(r)
    pooled, _, _, pooled_eng, pooled_level = _oe(rows, hours)
    cohorts = sorted(by_cohort)
    raw = {c: _oe(by_cohort[c], hours) for c in cohorts}
    terms = []
    for c in cohorts:
        mult, exp, *_ = raw[c]
        for h, m in mult.items():
            v = m / exp[h]
            terms.append((m - pooled.get(h, 1.0)) ** 2 - v)
    tau2 = max(0.0, sum(terms) / len(terms)) if terms else 0.0
    mhat, level, mass, eng = {}, {}, {}, {}
    for c in cohorts:
        mult, exp, dials, e_h, lvl = raw[c]
        mhat[c] = {}
        for h in hours:
            base = pooled.get(h, 1.0)
            if h in mult:
                v = mult[h] / exp[h]
                b = tau2 / (tau2 + v) if tau2 + v > 0 else 0.0
                mhat[c][h] = base + b * (mult[h] - base)
            else:
                mhat[c][h] = base
        level[c] = lvl
        mass[c] = {h: dials.get(h, 0.0) for h in hours}
        k = settings.SCHEDULE_ENG_SHRINK_N
        cnt: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        for _, h, _a, _d, _n, x, e in by_cohort[c]:
            cnt[h][0] += e
            cnt[h][1] += x
        mean_e = sum(v[0] for v in cnt.values()) / max(sum(v[1] for v in cnt.values()), 1)
        eng[c] = {h: (cnt[h][0] + k * mean_e) / (cnt[h][1] + k) for h in hours}
    detail = {"tau2": round(tau2, 5), "pooled": {str(h): round(v, 3) for h, v in sorted(pooled.items())},
              "raw": {c: {str(h): round(v, 3) for h, v in sorted(raw[c][0].items())} for c in cohorts}}
    return Model(list(hours), cohorts, mhat, {h: pooled.get(h, 1.0) for h in hours}, tau2, level, mass, eng, detail)


def to_weights(multipliers: Mapping[int, float], hours: Sequence[int]) -> dict[str, float]:
    """Mean-1 weights proportional to the multiplier, clipped to the allowed range; every hour present."""
    lo, hi = settings.SCHEDULE_WEIGHT_CLIP
    raw = [max(multipliers.get(h, 1.0), 1e-6) for h in hours]
    mean = sum(raw) / len(raw)
    w = [min(max(v / mean, lo), hi) for v in raw]
    mean = sum(w) / len(w)
    w = [min(max(v / mean, lo), hi) for v in w]
    return {str(h): round(v, 4) for h, v in zip(hours, w)}


def tables(model: Mapping | Model, cohorts_in_use: Sequence[str]) -> dict[str, dict[str, float]]:
    """Weights table per cohort in use plus ``default`` (pooled profile when it holds no data)."""
    out: dict[str, dict[str, float]] = {}
    for c in list(cohorts_in_use) + ["default"]:
        m = model.mhat.get(c) or model.pooled
        out[c] = to_weights(m, model.hours)
    return out


def _w(table: Mapping | None, h: int) -> float:
    return 1.0 if not table else float(table.get(str(h), 0.0))


def outcome(model: Model, new: Mapping[str, Mapping], current: Mapping[str, Mapping]) -> dict:
    """Expected pickup and engagement-of-connected if dials were re-weighted from ``current`` to ``new`` tables.

    Per hour the capacity is fixed (the observed dials D_h); it is shared among cohorts in proportion to their
    volume V_c times their weight in that hour (eligible mass is assumed independent of the hour: past dials are not
    divided by past weights, so the estimate does not depend on which tables produced the history). A cohort with
    no table falls back to ``default``'s, then flat (1), like the sim.
    """
    def table(tbl: Mapping, c: str):
        return tbl[c] if c in tbl else tbl.get("default")
    res = {}
    for name, tbl in (("old", current), ("new", new)):
        p_sum = e_sum = d_sum = 0.0
        for h in model.hours:
            masses = {c: sum(model.mass[c].values()) * _w(table(tbl, c), h) for c in model.cohorts}
            tot = sum(masses.values())
            d_h = sum(model.mass[c][h] for c in model.cohorts)
            if tot <= 0 or d_h <= 0:
                continue
            for c, m in masses.items():
                dials = d_h * m / tot
                p = min(model.level[c] * model.mhat[c][h], 0.95)
                p_sum += dials * p
                e_sum += dials * p * model.eng[c][h]
                d_sum += dials
        res[name] = {"pickup": p_sum / d_sum if d_sum else 0.0, "engagement": e_sum / p_sum if p_sum else 0.0}
    old, new_ = res["old"], res["new"]
    return {"pickup_old": round(old["pickup"], 5), "pickup_new": round(new_["pickup"], 5),
            "rel_lift": round(new_["pickup"] / old["pickup"] - 1, 5) if old["pickup"] else 0.0,
            "engagement_old": round(old["engagement"], 5), "engagement_new": round(new_["engagement"], 5),
            "engagement_drop": round(old["engagement"] - new_["engagement"], 5)}


# --------------------------------------------------------------------------- post-apply verification
def _z_p(diff: float, se: float) -> tuple[float, float]:
    from .stats import norm_sf
    z = diff / se if se > 0 else 0.0
    return z, norm_sf(z)


def verify_change(before: Mapping, after: Mapping) -> dict:
    """Hour-mix-adjusted before/after comparison of pickup and engagement.

    ``before`` / ``after`` map ``(hour_of_day, cohort)`` to ``(dials, connects, engaged)``. Both periods are
    standardised to the BEFORE dial mix over the cells they share, so a pure re-timing is not a gain and a
    change in hour mix cannot masquerade as a drop; what is left is whether pickup (or engagement) within the
    same hour x cohort cells moved. There is no concurrent control: day-to-day lead mix, DID changes and fatigue
    confound it, and the caller treats it as a guardrail, not as proof of lift.
    """
    keys = [k for k in before if k in after and before[k][0] > 0 and after[k][0] > 0]
    nb_tot = sum(before[k][0] for k in keys)
    if not keys or nb_tot <= 0:
        return {"usable": False}
    out: dict = {"usable": True, "cells": len(keys), "n_after": sum(v[0] for v in after.values())}
    for name, num, den in (("pickup", 1, 0), ("engagement", 2, 1)):
        pb = pa = vb = va = 0.0
        for k in keys:
            w = (before[k][0] if den == 0 else before[k][1]) / (nb_tot if den == 0 else max(sum(before[q][1] for q in keys), 1))
            rb, ra = before[k][num] / max(before[k][den], 1), after[k][num] / max(after[k][den], 1)
            pb += w * rb
            pa += w * ra
            vb += w * w * rb * (1 - rb) / max(before[k][den], 1)
            va += w * w * ra * (1 - ra) / max(after[k][den], 1)
        se = (vb + va) ** 0.5
        z, p = _z_p(pb - pa, se)
        out[name] = {"before": round(pb, 5), "after_std": round(pa, 5), "drop": round(pb - pa, 5),
                     "se": round(se, 5), "z": round(z, 3), "p_drop": p}
    raw = lambda d, i, j: sum(v[i] for v in d.values()) / max(sum(v[j] for v in d.values()), 1)  # noqa: E731
    out["raw_pickup_before"] = round(raw(before, 1, 0), 5)
    out["raw_pickup_after"] = round(raw(after, 1, 0), 5)
    out["raw_engagement_before"] = round(raw(before, 2, 1), 5)
    out["raw_engagement_after"] = round(raw(after, 2, 1), 5)
    return out
