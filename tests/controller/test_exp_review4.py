"""Decay alarm past t=0.9, zero-engaged arms, Holm family guard."""
import math

import pytest

from controller import exp_decay as xd, exp_rules as er, exp_stats as es, settings


def _contrast(t: float, z: float = 0.0, n_c: float = 1e6) -> es.Contrast:
    se = 1.0 / math.sqrt(t * xd.epoch_information())
    return es.Contrast(n_c, n_c, 0.5, 0.5, z * se, se, z, 1000)


# ---------------------------------------------------------------- sequential decay test across epochs
def test_final_look_after_an_overshooting_look_is_taken_and_ends_the_epoch():
    ts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.93]
    out = xd.sequential_alarm(_contrast(1.0), ts)
    assert out["look"] and out["epoch_done"] and out["boundary"] is not None and math.isfinite(out["boundary"])


def test_final_look_boundary_uses_the_remaining_alpha():
    b = es.obf_boundary([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.93, 1.0], settings.EXP_DECAY_ALPHA)
    assert math.isfinite(b) and 1.2 < b < 2.5


def test_fresh_epoch_takes_looks_again_after_the_final_look():
    out = xd.sequential_alarm(_contrast(1.0), [0.9, 0.95])
    assert out["look"] and out["epoch_done"]
    fresh = xd.sequential_alarm(_contrast(0.12), [])
    assert fresh["look"] and len(fresh["ts"]) == 1 and abs(fresh["ts"][0] - fresh["t"]) < 1e-3 and not fresh["epoch_done"]


# ---------------------------------------------------------------- Holm family size
def test_holm_family_size_is_at_least_the_number_of_arms():
    out = es.holm_order({"a": 3.0, "b": 2.0, "c": 1.0}, 1, 0.09, lambda a, arm: 0.0)
    assert out["a"]["alpha_adj"] == 0.03 and out["c"]["alpha_adj"] == 0.09


# ---------------------------------------------------------------- zero-engaged arms
def _rows(n_leads, rate, offset=0):
    """Engaged rows: ``n_leads`` leads with one connect each, a deterministic ``rate`` share engaged."""
    k = round(n_leads * rate)
    return {offset + i: (1.0, 1.0 if i < k else 0.0) for i in range(n_leads)}


def _data(ctl_rate, arm_rate, n_arm=200, n_ctl=400, recent_arm=None):
    arm, ctl = _rows(n_arm, arm_rate), _rows(n_ctl, ctl_rate, offset=10_000)
    rec = _rows(n_arm // 2, arm_rate if recent_arm is None else recent_arm)
    return {"A": er.ArmData(arm, rec, {}), "C": er.ArmData(ctl, _rows(n_ctl // 2, ctl_rate, offset=10_000), {})}


def test_zero_engaged_arm_is_harm_stopped_dropped_and_never_a_winner():
    data = _data(0.5, 0.0)
    s = er.summarise(data, "C", ["A"])
    assert s["A"]["ok"] and s["A"]["lift"] == -1.0 and s["A"]["info"] == 0.0 and math.isfinite(s["A"]["z"])
    assert er.harm_stops(s) == {"A": "now"}
    assert er.drop_holds(s["A"], False)
    out = er.daily_look(data, "C", ["A"], 1, 0.05, arm_t={}, looks_taken=0, ramped=[], exposure_days=3.0, max_drop=0.02)
    assert "A" in out.drop and out.winner is None and not out.ramp


def test_zero_engaged_arm_with_a_low_control_rate_is_not_harm_stopped():
    s = er.summarise(_data(0.02, 0.0, n_arm=100, n_ctl=1000), "C", ["A"])
    assert s["A"]["ok"] and er.harm_stops(s) == {}


def test_zero_engaged_arm_is_excluded_from_the_winner_test():
    data = _data(0.5, 0.0, n_arm=400)
    out = er.daily_look(data, "C", ["A"], 1, 0.05, arm_t={}, looks_taken=0, ramped=[], exposure_days=3.0, max_drop=0.02)
    assert not out.took_look and out.winner is None


def test_recent_collapse_of_a_ramped_arm_is_recent_dropped():
    data = _data(0.5, 0.5, recent_arm=0.0)
    s = er.summarise(data, "C", ["A"])
    assert s["A"]["recent_z"] <= settings.EXP_RAMPED_DROP_RECENT_Z and s["A"]["recent_lift"] == -1.0
    assert er.drop_holds(s["A"], True) and er.ramp_blocked(s["A"])


def test_an_arm_with_engagement_keeps_its_record_unchanged():
    data = _data(0.5, 0.4)
    s = er.summarise(data, "C", ["A"])["A"]
    assert "zero_engaged" not in s
    assert s["rate_arm"] == pytest.approx(0.4) and s["rate_ctrl"] == pytest.approx(0.5)  # engaged / connects, by hand
    assert s["lift"] == pytest.approx(0.4 / 0.5 - 1.0) and s["z"] < 0 < s["info"]


def test_every_epoch_ends_once_information_reaches_the_design_value_whatever_the_growth_path():
    """Lifecycle property: for any increasing information path, the epoch reaches epoch_done, never stalls.

    It guards against a stall when looks overshoot the 0.1 information steps.
    """
    import random
    rng = random.Random(7)
    for _ in range(40):  # each look integrates a boundary: keep the test fast
        ts, t, done = [], 0.0, False
        for _step in range(400):
            t = min(1.2, t + rng.uniform(0.005, 0.2))  # information keeps growing, in uneven steps
            out = xd.sequential_alarm(_contrast(t), ts)
            ts = out["ts"]
            if out["epoch_done"]:
                done = True
                break
        assert done, f"epoch stalled: looks {ts}"
        assert ts[-1] >= 1.0 and all(b > a for a, b in zip(ts, ts[1:]))
