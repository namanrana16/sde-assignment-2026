"""Unit tests: stats helpers, fatigue-curve fit, step test, probe cap, evacuation planning."""
import math
import random

import pytest

from controller import settings, stats
from controller.detectors import did_plan as dp
from controller.detectors import fatigue_curve as fc


# ------------------------------------------------------------------ stats
def test_two_proportion_one_sided():
    z, p = stats.two_prop_z(50, 1000, 100, 1000, alternative="less")
    assert z < -4 and p < 1e-4
    z, p = stats.two_prop_z(100, 1000, 100, 1000, alternative="less")
    assert p == pytest.approx(0.5)
    z, p = stats.two_prop_z(0, 0, 5, 10)
    assert p == 1.0  # no data: no evidence


def test_isotonic_decreasing_pools_violators():
    blocks = stats.isotonic_decreasing([1, 2, 3, 4], [0.9, 0.5, 0.7, 0.2], [1, 1, 1, 1])
    levels = [b[1] for b in blocks]
    assert levels == sorted(levels, reverse=True)
    assert blocks[1][1] == pytest.approx(0.6)  # 0.5 and 0.7 pooled


def test_wilson_upper_monotone():
    assert stats.wilson_upper(3, 300) < stats.wilson_upper(6, 300) < stats.wilson_upper(6, 100)


# ------------------------------------------------------------------ fatigue curve on synthetic data
TRUE_TAU_H = 24.0 * 2


def synth(seed=1, days=14, rate_per_hour=100, dids=("D1", "D2"), l0=3000.0, base=0.3):
    """A load-fatigue world with a different functional form from the production one."""
    rng = random.Random(seed)
    load = {d: 0.0 for d in dids}
    decay = math.exp(-1 / TRUE_TAU_H)
    obs = []
    for h in range(days * 24):
        hod = h % 24
        for d in dids:
            n = rate_per_hour if 9 <= hod < 21 else 0
            health = 1.0 / (1.0 + (load[d] / l0) ** 1.5)
            x = sum(rng.random() < base * health for _ in range(n))
            obs.append((h, d, n, x))
            load[d] += n
        for d in dids:
            load[d] *= decay
    return obs


def test_fit_recovers_monotone_decreasing_curve_and_reasonable_window():
    obs = synth()
    curve = fc.fit_curve(obs)
    assert curve.fitted
    hs = [curve.h(x) for x in (0, 500, 1000, 2000, 3000, 6000)]
    assert hs[0] == pytest.approx(1.0) and all(a >= b - 1e-12 for a, b in zip(hs, hs[1:]))
    assert hs[-1] < 0.95  # fatigue is visible
    assert curve.tau_hours in [d * 24 for d in settings.DID_FATIGUE_TAU_GRID_DAYS]
    assert 0.25 * 24 < curve.tau_hours <= 8 * 24


def test_flat_curve_without_enough_data():
    assert not fc.fit_curve([(0, "D1", 100, 30)]).fitted
    flat = fc.FatigueCurve.flat()
    assert flat.h(10_000) == 1.0


def test_curve_roundtrips_through_json():
    c = fc.fit_curve(synth())
    again = fc.FatigueCurve.from_json(c.to_json())
    assert again.h(1234.0) == pytest.approx(c.h(1234.0))


def test_decayed_load_recurrence():
    series = {0: {"D1": 100}, 1: {"D1": 0}, 2: {"D1": 0}}
    loads = fc.decayed_loads(series, tau_hours=1.0, hours=range(0, 3))
    assert loads[0]["D1"] == 0.0
    assert loads[1]["D1"] == pytest.approx(100 * math.exp(-1))
    assert loads[2]["D1"] == pytest.approx(100 * math.exp(-2))


# ------------------------------------------------------------------ step test
def test_step_test_flags_a_sudden_drop_and_not_noise():
    # peers pick up 30%; D2 historically 30% too, now 10% over 300 dials
    r = stats.ratio_drop_test(x_w=30, n_w=300, xp_w=270, np_w=900, x_r=900, n_r=3000, xp_r=2700, np_r=9000)
    assert r.drop > 0.6 and r.p < 1e-6
    quiet = stats.ratio_drop_test(x_w=88, n_w=300, xp_w=270, np_w=900, x_r=900, n_r=3000, xp_r=2700, np_r=9000)
    assert quiet.p > 0.05 and quiet.drop < 0.15


def test_step_test_load_adjustment_explains_a_drop():
    base = dict(x_w=80, n_w=300, xp_w=270, np_w=900, x_r=900, n_r=3000, xp_r=2700, np_r=9000)
    raw = stats.ratio_drop_test(**base)
    adj = stats.ratio_drop_test(**base, expected_ratio_change=0.85)  # curve says load costs 15%
    assert adj.drop < raw.drop and adj.p > raw.p


# ------------------------------------------------------------------ probe cap
def test_probe_cap_formula():
    h = 0.8
    assert dp.probe_cap(h) == pytest.approx(0.015 * h / (h - settings.DID_H_FLOOR))
    assert dp.probe_cap(1.0) == pytest.approx(0.015 / (1 - settings.DID_H_FLOOR))
    # worst case: a probed DID at h_floor costs at most 1.5% of expected DID health
    s = dp.probe_cap(h)
    assert s * (h - settings.DID_H_FLOOR) / h == pytest.approx(0.015)
    assert dp.probe_cap(0.3) <= settings.DID_PROBE_MAX_SHARE
    assert dp.probe_cap(settings.DID_H_FLOOR) == settings.DID_PROBE_MAX_SHARE  # clamp at h <= h_floor
    assert dp.probe_cap(0.1) == settings.DID_PROBE_MAX_SHARE
    assert dp.reprobe_cap(0.9) < dp.probe_cap(0.9)


# ------------------------------------------------------------------ allocation planning
def test_tv():
    assert dp.tv({"A": 0.5, "B": 0.5}, {"A": 1.0}) == pytest.approx(0.5)


def test_target_probes_one_untested_did_at_cap_and_splits_rest_evenly():
    st = {"D1": dp.TESTED, "D2": dp.TESTED, "D3": dp.UNTESTED, "D4": dp.UNTESTED}
    t = dp.target_allocation({"D1": 0.5, "D2": 0.5}, st, h_bar=0.85)
    cap = dp.probe_cap(0.85)
    assert t["D3"] == pytest.approx(cap) and "D4" not in t  # only one probe at a time
    assert t["D1"] == pytest.approx((1 - cap) / 2) == pytest.approx(t["D2"])
    st["D3"] = dp.TESTED
    t2 = dp.target_allocation(t, st, h_bar=0.85)
    assert t2["D4"] == pytest.approx(cap) and t2["D3"] == pytest.approx((1 - cap) / 3)


def test_target_excludes_bad_dids_and_goes_even_when_all_tested():
    st = {"D1": dp.TESTED, "D2": dp.FLAGGED, "D3": dp.WEAK}
    assert dp.target_allocation({"D1": 0.5, "D2": 0.5}, st, 0.9) == {"D1": pytest.approx(1.0)}
    st = {"D1": dp.TESTED, "D2": dp.TESTED, "D3": dp.TESTED}
    t = dp.target_allocation({"D1": 0.5, "D2": 0.5}, st, 0.9)
    assert all(v == pytest.approx(1 / 3) for v in t.values())


def test_evacuation_prefers_tested_and_keeps_existing_untested_probe():
    st = {"D1": dp.TESTED, "D2": dp.FLAGGED, "D3": dp.UNTESTED}
    t = dp.target_allocation({"D1": 0.4, "D2": 0.4, "D3": 0.2}, st, 0.9)
    assert t == {"D1": pytest.approx(0.8), "D3": pytest.approx(0.2)}  # D3 keeps its holding, D1 absorbs D2


def test_evacuation_uses_untested_only_when_no_tested_exists_capped_at_need():
    st = {"D1": dp.FLAGGED, "D2": dp.UNTESTED, "D3": dp.UNTESTED}
    t = dp.target_allocation({"D1": 0.6, "D2": 0.4}, st, 0.9)
    assert "D1" not in t and sum(t.values()) == pytest.approx(1.0)
    assert t["D2"] > dp.probe_cap(0.9)  # needed share exceeds the probe cap: cap = max(probe cap, need)


def test_reprobe_stays_within_a_quarter_of_the_harm_line():
    st = {"D1": dp.TESTED, "D2": dp.REPROBE}
    t = dp.target_allocation({"D1": 1.0}, st, 0.9)
    assert t["D2"] == pytest.approx(dp.reprobe_cap(0.9)) and t["D2"] * (0.9 - settings.DID_H_FLOOR) / 0.9 <= 0.005 + 1e-9


def test_evacuation_steps_respect_max_shift_and_end_with_zero():
    cur = {"D1": 0.5, "D2": 0.5}
    st = {"D1": dp.TESTED, "D2": dp.FLAGGED, "D3": dp.TESTED}
    target = dp.target_allocation(cur, st, 0.9)
    assert "D2" not in target
    steps = 0
    while dp.tv(cur, target) > 1e-9:
        nxt = dp.plan_step(cur, target, 0.2, st)
        assert dp.tv(cur, nxt) <= 0.2 * settings.DID_STEP_SAFETY + 1e-9
        assert sum(nxt.values()) == pytest.approx(1.0)
        cur = nxt
        steps += 1
        assert steps < 20
    assert cur.get("D2", 0.0) == 0.0 and steps == 3


def test_evacuation_fills_tested_receivers_before_untested():
    cur = {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3}
    st = {"D1": dp.TESTED, "D2": dp.FLAGGED, "D3": dp.UNTESTED}
    target = {"D1": 0.5, "D3": 0.5}
    first = dp.plan_step(cur, target, 0.2, st)
    assert first["D1"] == pytest.approx(0.5)  # the tested DID is filled first
    assert first["D3"] < 0.5 and first["D2"] < 1 / 3


def test_plan_step_unbounded_when_no_max_shift():
    t = {"D1": 1.0}
    assert dp.plan_step({"D1": 0.5, "D2": 0.5}, t, None, {"D1": dp.TESTED, "D2": dp.TESTED}) == {"D1": 1.0}


# ------------------------------------------------------------------ exact tests (review fix 5)
def test_fisher_greater_matches_known_value_and_small_counts():
    # 3/10 vs 0/10: p = C(10,3)/C(20,3) = 120/1140
    assert stats.fisher_greater(3, 10, 0, 10) == pytest.approx(120 / 1140, rel=1e-9)
    assert stats.fisher_greater(0, 10, 3, 10) > 0.9
    assert stats.fisher_greater(0, 0, 3, 10) == 1.0


def test_rate_greater_test_uses_exact_for_small_counts_and_z_for_large():
    small = stats.rate_greater_test(6, 250, 3, 400)
    assert small == pytest.approx(stats.fisher_greater(6, 250, 3, 400))
    big = stats.rate_greater_test(60, 2000, 30, 4000)
    assert big == pytest.approx(stats.two_prop_z(60, 2000, 30, 4000, "greater")[1])


def test_wilson_lower_and_non_inferiority():
    assert stats.wilson_lower(30, 300) < 0.1 < stats.wilson_upper(30, 300)
    assert stats.non_inferior(5, 1000, 5, 1000, 0.01)
    assert not stats.non_inferior(40, 1000, 5, 1000, 0.01)
    assert not stats.non_inferior(0, 0, 5, 1000, 0.01)
