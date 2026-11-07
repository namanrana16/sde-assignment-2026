"""Caller-ID health evaluation: step test (flagged), peer comparison at comparable load (weak
base / passing), per CONTEXT C5. Pure functions over a ``DidSeries`` and a ``FatigueCurve``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .. import settings
from ..stats import RatioDrop, norm_sf, ratio_drop_test
from .did_data import DidSeries
from .fatigue_curve import FatigueCurve, decayed_loads


@dataclass(frozen=True)
class StepResult:
    did: str
    window_hours: int
    n_window: int
    n_peers_window: int
    n_ref: int
    test: RatioDrop
    share_window: float
    share_ref: float
    solo: bool = False  # own-history test (no peers): stricter level, see did_solo

    def flagged(self, level: float = settings.DID_FLAG_P) -> bool:
        if self.solo:
            level *= settings.DID_SOLO_P_FACTOR
        return self.test.drop >= settings.DID_FLAG_MIN_DROP and self.test.p < level

    def as_dict(self) -> dict:
        return {"did": self.did, "window_h": self.window_hours, "n": self.n_window,
                "n_peers": self.n_peers_window, "n_ref": self.n_ref, "drop": round(self.test.drop, 4),
                "z": round(self.test.z, 3), "p": self.test.p, "q_window": round(self.test.q_window, 4),
                "q_ref": round(self.test.q_ref, 4), "share_window": round(self.share_window, 4),
                "share_ref": round(self.share_ref, 4), "solo": self.solo}


@dataclass(frozen=True)
class PeerResult:
    did: str
    n: int
    connects: int
    expected: float
    q: float  # observed / expected-if-like-peers (load adjusted)
    z_weak: float  # z of H0 "at least (1 - margin) x expected"
    load: float
    peer_load: float

    def weak(self) -> bool:
        return (self.n >= settings.DID_WEAK_MIN_DIALS and self.z_weak <= -settings.Z_ONE_SIDED_P001
                and self.load <= self.peer_load * settings.DID_WEAK_LOAD_SLACK + 1e-9)

    def as_dict(self) -> dict:
        return {"did": self.did, "n": self.n, "connects": self.connects, "expected": round(self.expected, 2),
                "q": round(self.q, 4), "z_weak": round(self.z_weak, 3), "load": round(self.load, 1),
                "peer_load": round(self.peer_load, 1)}


@dataclass
class Evaluation:
    cur_hour: int
    curve: FatigueCurve
    loads_now: dict[str, float]
    steps: list[StepResult] = field(default_factory=list)
    peers: dict[str, PeerResult] = field(default_factory=dict)
    first_hour: dict[str, int | None] = field(default_factory=dict)
    total_dials: dict[str, int] = field(default_factory=dict)
    window_dials: dict[str, int] = field(default_factory=dict)
    shares: dict[str, float] = field(default_factory=dict)  # OBSERVED trailing traffic share per DID
    share_dials: int = 0  # dials behind ``shares`` (0: unknown)


def _hour_loads(series: DidSeries, curve: FatigueCurve, lo: int, hi: int):
    return decayed_loads(series.dials, curve.tau_hours, range(lo, hi + 1))


def _mean_h(curve: FatigueCurve, loads: Mapping[int, Mapping[str, float]], series: DidSeries,
            dids: Sequence[str], hours: range) -> tuple[float, float, float]:
    """Dial-weighted mean (health, load) of a DID group over hours; plus the dial total."""
    tot = hs = ls = 0.0
    for h in hours:
        for d in dids:
            n = series.dials.get(h, {}).get(d, 0)
            if n:
                tot += n
                hs += n * curve.h(loads[h][d])
                ls += n * loads[h][d]
    return (hs / tot if tot else 1.0), (ls / tot if tot else 0.0), tot


def step_tests(series: DidSeries, curve: FatigueCurve, cur_hour: int, candidates: Sequence[str],
               all_dids: Sequence[str]) -> list[StepResult]:
    """Rolling-window step-drop tests vs the prior day, for each candidate DID, per window length."""
    out: list[StepResult] = []
    lo = cur_hour - max(settings.DID_FLAG_WINDOWS_HOURS) - settings.DID_FLAG_REF_HOURS
    loads = _hour_loads(series, curve, lo, cur_hour)
    for did in candidates:
        peers = [d for d in all_dids if d != did]
        for k in settings.DID_FLAG_WINDOWS_HOURS:
            win = range(cur_hour - k + 1, cur_hour + 1)
            ref = range(cur_hour - k - settings.DID_FLAG_REF_HOURS + 1, cur_hour - k + 1)
            dw, cw = series.window(win)
            dr, cr = series.window(ref)
            n_w, n_r = dw.get(did, 0), dr.get(did, 0)
            np_w, np_r = sum(dw.get(p, 0) for p in peers), sum(dr.get(p, 0) for p in peers)
            m = settings.DID_FLAG_MIN_DIALS
            if min(n_w, n_r, np_w, np_r) < m:
                continue
            xp_w, xp_r = sum(cw.get(p, 0) for p in peers), sum(cr.get(p, 0) for p in peers)
            hd_w, _, _ = _mean_h(curve, loads, series, [did], win)
            hp_w, _, _ = _mean_h(curve, loads, series, peers, win)
            hd_r, _, _ = _mean_h(curve, loads, series, [did], ref)
            hp_r, _, _ = _mean_h(curve, loads, series, peers, ref)
            expected = (hd_w / hp_w) / (hd_r / hp_r)
            test = ratio_drop_test(cw.get(did, 0), n_w, xp_w, np_w, cr.get(did, 0), n_r, xp_r, np_r, expected)
            share_w, share_r = n_w / (n_w + np_w), n_r / (n_r + np_r)
            out.append(StepResult(did, k, n_w, np_w, n_r, test, share_w, share_r))
    return out


def flagged_step(results: Sequence[StepResult], level: float = settings.DID_FLAG_P) -> StepResult | None:
    """Most significant qualifying step with share roughly unchanged, else None."""
    lo, hi = settings.DID_FLAG_SHARE_BAND
    ok = [r for r in results if r.flagged(level) and r.share_ref > 0
          and lo <= r.share_window / r.share_ref <= hi]
    return min(ok, key=lambda r: r.test.p) if ok else None


def peer_comparison(series: DidSeries, curve: FatigueCurve, did: str, hours: range,
                    peers: Sequence[str], loads: Mapping[int, Mapping[str, float]]) -> PeerResult | None:
    """Observed vs expected connects of ``did`` if it were as good as its peers at ITS load.

    Per hour: expected pickup = pooled peer pickup x h(L_did) / mean peer h (curve-adjusted).
    """
    x = n_tot = 0
    exp = var = var_null = 0.0
    margin = 1.0 - settings.DID_WEAK_MARGIN
    for h in hours:
        n = series.dials.get(h, {}).get(did, 0)
        if not n:
            continue
        npd = sum(series.dials.get(h, {}).get(p, 0) for p in peers)
        if not npd:
            continue
        pc = sum(series.conns.get(h, {}).get(p, 0) for p in peers) / npd
        hp = sum(series.dials[h].get(p, 0) * curve.h(loads[h][p]) for p in peers) / npd
        pe = min(pc * curve.h(loads[h][did]) / max(hp, 1e-9), 0.99)
        x += series.conns.get(h, {}).get(did, 0)
        n_tot += n
        exp += n * pe
        var_null += n * (pe * margin) * (1 - pe * margin)
    if n_tot == 0 or exp <= 0:
        return None
    _, l_did, _ = _mean_h(curve, loads, series, [did], hours)
    _, l_peer, _ = _mean_h(curve, loads, series, list(peers), hours)
    z = (x - margin * exp) / math.sqrt(max(var_null, 1e-9))
    return PeerResult(did, n_tot, x, exp, x / exp, z, l_did, l_peer)


def observed_shares(series: DidSeries, cur_hour: int) -> tuple[dict[str, float], int]:
    """Trailing per-DID traffic shares from the calls actually dialled (not the config, which lags
    ``next_campaign_only``). Widens from 12 h to 24 h when the short window has too few dials."""
    for hours in (settings.DID_SHARE_WINDOW_HOURS, 24):
        dials, _ = series.window(range(cur_hour - hours + 1, cur_hour + 1))
        total = sum(dials.values())
        if total >= settings.DID_SHARE_MIN_DIALS:
            return {d: n / total for d, n in dials.items()}, total
    return {}, 0


def evaluate(series: DidSeries, curve: FatigueCurve, cur_hour: int, options: Sequence[str],
             flagged: Sequence[str] = (), bad: Sequence[str] = (), since: Mapping[str, int] | None = None
             ) -> Evaluation:
    """One full evaluation at ``cur_hour`` (absolute hour of the current sim minute).

    ``since`` maps re-probed DIDs to the absolute hour their re-probe started: older dials of such a DID
    (from before it was flagged) say nothing about its present health and are ignored for its judgement.
    """
    since = since or {}
    long_h = max(settings.DID_WEAK_WINDOW_HOURS, 72)
    span = min(max([long_h] + [cur_hour - v + 1 for v in since.values()]), 240)  # re-probe windows reach back further
    loads = _hour_loads(series, curve, cur_hour - span - 1, cur_hour + 1)
    ev = Evaluation(cur_hour, curve, dict(loads.get(cur_hour, {})))
    used = [d for d in options if series.total_dials(d) > 0]
    ev.total_dials = {d: series.total_dials(d) for d in options}
    ev.first_hour = {d: series.first_hour(d) for d in options}
    ev.shares, ev.share_dials = observed_shares(series, cur_hour)
    for d, start in since.items():  # judge a re-probed DID only on its post-expiry traffic
        hrs = [h for h in range(start, cur_hour + 1) if series.dials.get(h, {}).get(d)]
        ev.total_dials[d] = sum(series.dials[h][d] for h in hrs)
        ev.first_hour[d] = hrs[0] if hrs else None
    dw, _ = series.window(range(cur_hour - long_h + 1, cur_hour + 1))
    ev.window_dials = {d: dw.get(d, 0) for d in options}
    candidates = [d for d in used if d not in bad]
    peers_all = [d for d in used if d not in flagged]
    ev.steps = step_tests(series, curve, cur_hour, candidates, peers_all) if len(peers_all) >= 2 else []
    for d in candidates:
        refs = [p for p in peers_all if p != d and dw.get(p, 0) >= settings.DID_WEAK_MIN_DIALS and p not in bad]
        if not refs:
            continue
        windows = (settings.DID_WEAK_WINDOW_HOURS, long_h) + ((min(cur_hour - since[d] + 1, span),) if d in since else ())
        for hours in windows:
            res = peer_comparison(series, curve, d, range(max(cur_hour - hours + 1, since.get(d, -10**9)), cur_hour + 1),
                                  refs, loads)
            if res is not None and res.n >= settings.DID_PASS_MIN_DIALS:
                ev.peers[d] = res
                if hours == settings.DID_WEAK_WINDOW_HOURS and res.n >= settings.DID_WEAK_MIN_DIALS:
                    break  # prefer the short window once it has enough data
    return ev


def p_value_text(p: float) -> str:
    return f"{p:.2e}" if 1e-300 <= p < 0.001 else ("<1e-300" if p < 1e-300 else f"{p:.3f}")


def z_to_p(z: float) -> float:
    return norm_sf(-z)
