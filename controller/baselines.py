"""C3 history bootstrap: per-workflow baselines computed once history is ingested and persisted.

Contents (all learned from events, nothing hard-coded):
* ``did_daily``       per-DID daily dials/connects over the history ramp (identifies load fatigue)
* ``did_load_end``    trailing decayed dial volume per DID at the end of history (curve window)
* ``prompt``          per-prompt connects, flag rate and mean flow adherence
* ``engagement``      daily engagement level and variance (power calculations)
* ``lead_cells``      lead volume per feature cell
* ``pickup_hour_feature``  pickup by hour x feature value
Cold start (no history): the baseline is marked ``missing`` and recomputed daily from live data
until ``BASELINE_MIN_DAYS`` days exist.
"""
from __future__ import annotations

import math
from collections import defaultdict

from . import cube, settings
from .cohorts import FEATURES, VOCAB_META_KEY, FeatureVocab
from .desired import PlanContext
from .detectors.common import load_state, save_state
from .detectors.did_data import load_series
from .detectors.fatigue_curve import decayed_loads, fit_curve
from .store import Store

DETECTOR = "baselines"


def _variance(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def compute(store: Store, wid: str, now: int) -> dict:
    hist_days = store.conn.execute("SELECT COUNT(DISTINCT day) FROM cube WHERE workflow_id=? AND day<0",
                                   (wid,)).fetchone()[0]
    use_live = hist_days < settings.BASELINE_MIN_DAYS
    last = None if use_live else -1  # cold start: use everything seen so far
    vocab = FeatureVocab.load(store.get_meta(VOCAB_META_KEY))
    series = load_series(store, wid, now)
    daily: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for hour, did, n, x in series.obs():
        if last is not None and hour // 24 > last:
            continue
        day = daily[did].setdefault(str(hour // 24), [0, 0])
        day[0] += n
        day[1] += x
    curve = fit_curve([o for o in series.obs() if last is None or o[0] // 24 <= last])
    end_hour = ((last + 1) * 24 if last is not None else now // 60)
    loads = decayed_loads(series.dials, curve.tau_hours, [end_hour]).get(end_hour, {})
    prompts = {p: {"connects": c, "flag_rate": round(f / c, 5) if c else None,
                   "mean_adherence": round(a / c, 4) if c else None}
               for p, c, f, a in ((r.key[0], r.connects, r.flags, r.sum_adherence)
                                  for r in cube.rollup(store, wid, by=("prompt",), day_max=last))}
    eng = [r.engagement for r in cube.rollup(store, wid, by=("day",), day_max=last) if r.connects]
    cells: dict[str, int] = defaultdict(int)
    for cell, n in store.conn.execute("SELECT cell, COUNT(*) FROM leads WHERE workflow_id=? AND ts_minute<?"
                                      " GROUP BY cell", (wid, 0 if last is not None else now + 1)):
        cells[str(cell)] = n
    by_feature: dict = {f: {} for f in FEATURES}
    for row in cube.rollup(store, wid, by=("hour", "cell"), day_max=last):
        hour, cell = row.key
        feats = vocab.decode(cell)
        for f in FEATURES:
            slot = by_feature[f].setdefault(feats[f], {}).setdefault(str(hour), [0, 0])
            slot[0] += row.dials
            slot[1] += row.connects
    return {"status": "cold_start" if use_live else "history", "computed_minute": now, "history_days": hist_days,
            "did_daily": daily, "did_load_end": {d: round(v, 1) for d, v in loads.items()},
            "fatigue_tau_hours": curve.tau_hours, "prompt": prompts,
            "engagement": {"days": len(eng), "mean": round(sum(eng) / len(eng), 5) if eng else None,
                           "variance": round(_variance(eng), 8), "sd": round(math.sqrt(_variance(eng)), 5)},
            "lead_cells": dict(cells), "pickup_hour_feature": by_feature}


def get(store: Store, wid: str) -> dict:
    return load_state(store, DETECTOR, wid)


def ensure(ctx: PlanContext) -> None:
    """Detector: compute baselines once history exists; cold start refreshes daily."""
    for wid in sorted(ctx.workflows):
        cur = get(ctx.store, wid)
        if cur and (cur["status"] == "history" or (cur["status"] == "cold_start" and
                                                   ctx.now - cur["computed_minute"] < settings.BASELINE_REFRESH_MINUTES)):
            continue
        if ctx.store.conn.execute("SELECT 1 FROM cube WHERE workflow_id=? LIMIT 1", (wid,)).fetchone() is None:
            if not cur:
                save_state(ctx.store, DETECTOR, wid, {"status": "missing", "computed_minute": ctx.now})
            continue
        base = compute(ctx.store, wid, ctx.now)
        save_state(ctx.store, DETECTOR, wid, base)
        ctx.store.add_decision(ctx.now, "baselines", f"baselines ({base['status']}): {base['history_days']} history days,"
                               f" engagement {base['engagement']['mean']}, tau {base['fatigue_tau_hours']:.0f}h", wid,
                               {k: base[k] for k in ("status", "history_days", "did_load_end", "engagement",
                                                     "fatigue_tau_hours", "prompt")})
