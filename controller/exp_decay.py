"""C7 after a promotion.

Primary alarm: a sequential winner-vs-control CONCURRENT test (Mantel-Haenszel pooled clustered log-ratio, one-sided
Lan-DeMets alpha-spending boundary over the information gathered since the monitoring epoch began; an epoch ends
when its information reaches the design level, then a fresh one starts). Secondary: CUSUM on the winner's own
composition-adjusted engagement, which must be confirmed by the holdout direction.

The winner carries ~90% of traffic, so its own level is high-powered. Its engagement is adjusted for the hour x cell
mix of the calls it received (expected rate per (hour, cell) from the old control's pre-promotion cube history,
shrunk toward the pooled rate), then compared with its own level before promotion (or, if thin, its first blocks after). The 10%
holdout on the old control is the directional confirmation (and a second, direct significance route).
"""
from __future__ import annotations

import math
from typing import Sequence

from . import cube, exp_data, exp_rules as er, exp_stats as es, settings
from .cohorts import FeatureVocab
from .exp_state import DAY

Block = tuple[float, float, float, float]  # (connects, engaged, expected engaged, variance of engaged)


def reference_rates(store, wid: str, dim: str, control: str, day_max: int,
                    cells: Sequence[int] | None = None) -> tuple[dict, float]:
    """``{(hour, cell): shrunk engagement rate}`` of the old control over the cube days before promotion."""
    rows = cube.rollup(store, wid, by=("hour", "cell"), day_max=day_max, day_min=day_max - 14,
                       variant={exp_data.column(dim): control}, cells=cells)
    tot_n = sum(r.connects for r in rows)
    pooled = sum(r.engaged for r in rows) / tot_n if tot_n else 0.5
    m = settings.EXP_CUSUM_SHRINK_N
    return {r.key: (r.engaged + m * pooled) / (r.connects + m) for r in rows}, pooled


def _winner_rows(store, wid: str, dim: str, winner: str, since: int, until: int | None, ref: dict, pooled: float,
                 cells: Sequence[int] | None):
    """(expected p, engaged) of each winner connect in [since, until), in event order."""
    col = exp_data.column(dim)
    sql = (f"SELECT c.hour, l.cell, c.engaged FROM calls c JOIN leads l ON l.lid=c.lid WHERE c.workflow_id=?"
           f" AND c.day>=? AND c.ts_minute>=? AND c.connected=1 AND c.{col}=?")
    args: list = [wid, since // DAY, since, winner]
    if until is not None:
        sql += " AND c.ts_minute<?"
        args.append(until)
    if cells is not None:
        sql += f" AND l.cell IN ({','.join('?' * len(cells))})" if cells else " AND 0"
        args += list(cells)
    sql += " ORDER BY c.event_id"
    for hour, cell, engaged in store.conn.execute(sql, args):
        yield ref.get((hour, cell), pooled), engaged


def winner_blocks(store, wid: str, dim: str, winner: str, since: int, ref: dict, pooled: float,
                  cells: Sequence[int] | None = None) -> list[Block]:
    """Winner connects after ``since`` cut into blocks of EXP_CUSUM_BLOCK connects."""
    blocks: list[Block] = []
    n = x = e = v = 0.0
    for p, engaged in _winner_rows(store, wid, dim, winner, since, None, ref, pooled, cells):
        n, x, e, v = n + 1, x + engaged, e + p, v + p * (1 - p)
        if n >= settings.EXP_CUSUM_BLOCK:
            blocks.append((n, x, e, v))
            n = x = e = v = 0.0
    return blocks


def pre_promotion_level(store, wid: str, dim: str, winner: str, start: int, applied: int, ref: dict, pooled: float,
                        cells: Sequence[int] | None = None) -> Block | None:
    """The winner's own composition-adjusted level over its last 48 h of experiment exposure (None if too thin)."""
    lo = max(start, applied - 2 * DAY)
    n = x = e = v = 0.0
    for p, engaged in _winner_rows(store, wid, dim, winner, lo, applied, ref, pooled, cells):
        n, x, e, v = n + 1, x + engaged, e + p, v + p * (1 - p)
    return (n, x, e, v) if n >= settings.EXP_CUSUM_BLOCK else None


def cusum(blocks: Sequence[Block], reference: Block | None = None) -> dict:
    """One-sided CUSUM for a DROP of the composition-adjusted ratio.

    The reference is the winner's own level just BEFORE promotion when it has enough data (a decay that is already
    under way when the winner goes live would otherwise be baked into the reference), else its first blocks after.
    """
    k_ref = 0 if reference else settings.EXP_CUSUM_REF_BLOCKS
    out = {"blocks": len(blocks), "alarm": False, "S": 0.0, "ref_ratio": None, "last_ratio": None,
           "reference": "pre-promotion 48h" if reference else f"first {k_ref} blocks"}
    if len(blocks) <= k_ref:
        return out
    ref = reference or (sum(b[0] for b in blocks[:k_ref]), sum(b[1] for b in blocks[:k_ref]),
                        sum(b[2] for b in blocks[:k_ref]), sum(b[3] for b in blocks[:k_ref]))
    e0 = ref[2]
    if e0 <= 0:  # a zero reference expectation has no ratio to track: no alarm
        return out
    se0 = math.sqrt(ref[3]) / e0
    r0 = ref[1] / e0
    s, first_alarm = 0.0, None
    for i, (_, x, e, v) in enumerate(blocks[k_ref:], start=k_ref):
        if e <= 0:
            continue
        r, se = x / e, math.sqrt(v) / e
        z = (r0 - r) / math.sqrt(se0 ** 2 + se ** 2)
        s = max(0.0, s + z - settings.EXP_CUSUM_K)
        if s >= settings.EXP_CUSUM_H and first_alarm is None:
            first_alarm = i
    out.update(S=round(s, 3), ref_ratio=round(r0, 4), last_ratio=round(blocks[-1][1] / blocks[-1][2], 4) if blocks[-1][2] > 0 else None,
               alarm=s >= settings.EXP_CUSUM_H, first_alarm_block=first_alarm)
    return out


def holdout_view(store, wid: str, dim: str, winner: str, holdout: str, since: int, now: int,
                 cells: Sequence[int] | None = None) -> dict:
    """Winner vs the concurrent holdout: recent-window point comparison and since-promotion significance."""
    data = exp_data.load_arms(store, wid, dim, since, now, cells)
    w, h = data.get(winner, er.EMPTY), data.get(holdout, er.EMPTY)
    since_c, recent_c = er.pair_contrast(w, h), er.pair_contrast(w, h, recent=True)
    out = {"holdout_connects": es.totals(h.engaged)[0], "recent_holdout_connects": es.totals(h.recent)[0],
           "recent_winner_connects": es.totals(w.recent)[0]}
    if recent_c is not None:
        out.update(recent_winner=recent_c.rate_a, recent_holdout=recent_c.rate_c, recent_lift=recent_c.lift)
    if since_c is not None:
        out.update(since_lift=since_c.lift, since_z=since_c.z)
    return out


def epoch_information() -> float:
    """Information at which a -EXP_DECAY_DESIGN_LIFT decay has the design power at the rollback test's level."""
    n = es._N
    return (n.inv_cdf(1 - settings.EXP_DECAY_ALPHA) + n.inv_cdf(settings.EXP_DESIGN_POWER)) ** 2 / math.log(
        1 - settings.EXP_DECAY_DESIGN_LIFT) ** 2


def sequential_alarm(c: es.Contrast | None, ts: Sequence[float], n_ctrl_min: float | None = None) -> dict:
    """One check of the sequential winner-vs-control test on data since the epoch started.

    ``ts`` are the information fractions of the looks taken so far in the epoch. A look is taken when the
    information has grown by 0.1 since the previous look (at most 10 per epoch). Returns ``{"t", "z", "boundary", "alarm", "look", "epoch_done", "ts"}``.
    """
    out = {"t": 0.0, "z": None, "boundary": None, "alarm": False, "look": False, "epoch_done": False, "ts": list(ts)}
    if c is None:
        return out
    t = min(1.0, c.info / epoch_information())
    out.update(t=round(t, 4), z=round(c.z, 3))
    next_planned = (ts[-1] if ts else 0.0) + 0.1
    if t + 1e-9 < next_planned or c.n_c < (n_ctrl_min if n_ctrl_min is not None else settings.EXP_DECAY_MIN_CONNECTS):
        return out
    seq = list(ts) + [t]
    b = es.obf_boundary(seq, settings.EXP_DECAY_ALPHA)
    out.update(look=True, ts=seq, boundary=None if not math.isfinite(b) else round(b, 3),
               alarm=bool(c.z <= -b), epoch_done=t >= 1.0)
    return out


def stage2_ready(minutes_at_stage1: float, since_c: es.Contrast | None, recent_c: es.Contrast | None) -> tuple[bool, str]:
    """Stage 1 -> stage 2 gate: >= 24 h at stage 1, recent-24h one-sided lower bound > 0, 50/50 contrast still positive."""
    if minutes_at_stage1 < settings.EXP_STAGE1_MIN_MINUTES:
        return False, f"only {minutes_at_stage1 / 60:.0f} h at stage 1"
    if recent_c is None or recent_c.n_a < settings.EXP_RECENT_MIN_CONNECTS:
        return False, "recent window too thin"
    lb = recent_c.lift_lower(settings.EXP_RECENT_Z)
    if lb <= 0:
        return False, f"recent-24h lift lower bound {lb:+.1%} is not above 0"
    if since_c is None or since_c.lift <= 0:
        return False, "the concurrent 50/50 contrast since stage 1 is not positive"
    return True, (f"{minutes_at_stage1 / 60:.0f} h at stage 1, recent-24h lift {recent_c.lift:+.1%} (lower bound {lb:+.1%}), "
                  f"concurrent lift since stage 1 {since_c.lift:+.1%}")


def veto_lower_bound(recent_c: es.Contrast | None) -> tuple[bool, str]:
    """Apply-time check of a promotion stage: the recent-window one-sided 80% lower bound of the lift must be > 0."""
    if recent_c is None:
        return False, "recent window unmeasurable"
    lb = recent_c.lift_lower(settings.EXP_VETO_Z)
    return lb > 0, f"recent-window lift {recent_c.lift:+.1%}, one-sided 80% lower bound {lb:+.1%}"


def decide(cus: dict, held: dict, seq: dict | None = None) -> tuple[bool, str]:
    """Rollback when the sequential concurrent test crosses its bound, or the CUSUM alarms AND the holdout agrees."""
    if seq and seq.get("alarm"):
        return True, (f"winner is significantly worse than the concurrent control (sequential test z {seq['z']:.2f} <= "
                      f"-{seq['boundary']:.2f}, information {seq['t']:.2f} of the epoch)")
    if cus["alarm"] and held.get("recent_lift") is not None and held["recent_lift"] <= 0:
        lo = settings.EXP_ROLLBACK_HOLDOUT_MIN
        n_h, n_w = held.get("recent_holdout_connects", 0), held.get("recent_winner_connects", 0)
        if n_h < lo or n_w < lo:  # act on populations: a tiny holdout/winner window cannot confirm a rollback
            return False, (f"hold: CUSUM alarmed but the confirming window is thin (holdout {n_h}, winner {n_w} connects"
                           f" < {lo})")
        return True, (f"CUSUM on the winner's composition-adjusted engagement alarmed (S {cus['S']:.1f} >= "
                      f"{settings.EXP_CUSUM_H}; ratio {cus['ref_ratio']} -> {cus['last_ratio']}) and the holdout "
                      f"confirms (recent winner-vs-holdout lift {held['recent_lift']:+.1%})")
    return False, ""
