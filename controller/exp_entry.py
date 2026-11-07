"""Entry-time helpers of the experiment driver: sim parameters (tick, run end) and the pre-entry power check."""
from __future__ import annotations

import json
import logging
from typing import Mapping

from . import cube, exp_state as xs, settings
from .cohorts import VOCAB_META_KEY, FeatureVocab

log = logging.getLogger(__name__)
DAY = settings.MINUTES_PER_DAY
PARAMS_META_KEY = "exp_sim_params"
POWER_LOOKBACK_DAYS = 3  # recent complete days that give the daily volume of the default-allocation traffic
# Entry needs every new challenger to be projected to reach EXP_MIN_ARM_CONNECTS_FOR_TEST connects: that is the hard
# gate of the daily look (``exp_rules.daily_look`` tests no arm below it), so an arm that cannot reach it can never
# produce a winner decision. (25% of the design information is stricter but depends on the unknown effect size.)
MIN_PROJECTED_ARM_CONNECTS = settings.EXP_MIN_ARM_CONNECTS_FOR_TEST


def sim_params(ctx) -> tuple[int, int | None]:
    """(tick minutes, run end minute or None): read once from the sim state, cached in ``meta``.

    Falls back to settings.TICK_MINUTES / unknown end when there is no client or the call fails.
    """
    raw = ctx.store.get_meta(PARAMS_META_KEY)
    if raw:
        got = json.loads(raw)
        return int(got["tick"]), got.get("end")
    if ctx.client is None:
        return settings.TICK_MINUTES, None
    try:
        state = ctx.client.state()
        got = {"tick": int(state.get("tick_minutes") or settings.TICK_MINUTES),
               "end": int(state["end_minute"]) if state.get("end_minute") is not None else None}
    except Exception as exc:  # the sim state is a convenience here, never a dependency
        log.warning("sim state unavailable for experiment parameters: %s", exc)
        return settings.TICK_MINUTES, None
    ctx.store.set_meta(PARAMS_META_KEY, json.dumps(got))
    return got["tick"], got["end"]


def daily_connects(ctx, wid: str, dim: str, cfg: Mapping) -> float | None:
    """Recent daily connects of the traffic the default allocation serves (cohorts with their own allocation excluded).

    None when the cube holds no complete or current day yet (nothing to project from).
    """
    day = ctx.now // DAY
    rows = cube.rollup(ctx.store, wid, by=("day", "cell"), day_min=day - POWER_LOOKBACK_DAYS, day_max=day - 1)
    if not rows:
        rows = cube.rollup(ctx.store, wid, by=("day", "cell"), day_min=day, day_max=day)
    if not rows:
        return None
    vocab = FeatureVocab.load(ctx.store.get_meta(VOCAB_META_KEY))
    skip = set(xs.override_cells(ctx.store, vocab, wid, cfg, dim))
    served = [r for r in rows if r.key[1] not in skip]
    days = {r.key[0] for r in rows}
    return sum(r.connects for r in served) / len(days)


def projected_arm_connects(ctx, wid: str, dim: str, cfg: Mapping, share: float) -> float | None:
    """Connects one arm at ``share`` is projected to get over the rest of the run; None when it cannot be projected."""
    _, end = sim_params(ctx)
    daily = daily_connects(ctx, wid, dim, cfg)
    if end is None or daily is None:
        return None
    return daily * share * max(0.0, (end - ctx.now) / DAY)
