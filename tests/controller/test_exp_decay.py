"""C7: CUSUM on the winner's composition-adjusted engagement + holdout confirmation; analysis groups."""
import math

import random

import pytest

from controller import exp_decay as xd, exp_state as xs, exp_stats as es, settings


def blocks(ratios, n=150, p=0.5):
    out = []
    for r in ratios:
        e = n * p
        out.append((float(n), r * e, e, n * p * (1 - p)))
    return out


def test_stable_level_never_alarms():
    c = xd.cusum(blocks([1.0, 1.02, 0.98, 1.01, 0.99, 1.0, 1.03, 0.97, 1.0, 1.01] * 3))
    assert not c["alarm"] and c["blocks"] == 30


def test_decay_after_the_reference_blocks_alarms():
    stable = [1.0] * settings.EXP_CUSUM_REF_BLOCKS
    c = xd.cusum(blocks(stable + [0.97, 0.92, 0.88, 0.85, 0.85, 0.85]))
    assert c["alarm"] and c["first_alarm_block"] is not None and c["last_ratio"] < c["ref_ratio"]


def test_too_few_blocks_cannot_alarm():
    assert not xd.cusum(blocks([1.0, 0.5, 0.5]))["alarm"]


ENOUGH = {"recent_holdout_connects": 100, "recent_winner_connects": 900, "holdout_connects": 100}


def test_cusum_rollback_holds_without_minimum_holdout_sample():  # B2
    alarm = {"alarm": True, "S": 5.0, "ref_ratio": 1.0, "last_ratio": 0.9}
    for held in ({"recent_lift": -0.05, "recent_holdout_connects": settings.EXP_ROLLBACK_HOLDOUT_MIN - 1,
                  "recent_winner_connects": 900},
                 {"recent_lift": -0.05, "recent_holdout_connects": 100,
                  "recent_winner_connects": settings.EXP_ROLLBACK_HOLDOUT_MIN - 1}):
        roll, why = xd.decide(alarm, held)
        assert not roll and "hold" in why
    held = {"recent_lift": -0.05, "recent_holdout_connects": settings.EXP_ROLLBACK_HOLDOUT_MIN,
            "recent_winner_connects": settings.EXP_ROLLBACK_HOLDOUT_MIN}
    assert xd.decide(alarm, held)[0]


def test_rollback_needs_cusum_plus_holdout_direction_or_significance():
    alarm = {"alarm": True, "S": 5.0, "ref_ratio": 1.0, "last_ratio": 0.9}
    quiet = {"alarm": False, "S": 0.0, "ref_ratio": 1.0, "last_ratio": 1.0}
    assert xd.decide(alarm, {"recent_lift": -0.05, **ENOUGH})[0]
    assert not xd.decide(alarm, {"recent_lift": 0.04, "holdout_connects": 100})[0]  # holdout disagrees: no rollback
    assert not xd.decide(quiet, {"recent_lift": -0.05, "holdout_connects": 100})[0]
    assert not xd.decide(quiet, {"since_z": -2.4, "since_lift": -0.1, "holdout_connects": 100})[0]  # CUSUM is only secondary
    assert xd.decide(quiet, {}, {"alarm": True, "z": -4.0, "boundary": 3.1, "t": 0.5})[0]


def test_desired_matching_and_patch_shape():
    cfg = {"allocations": {"default": {"voice": {"V1": 0.9, "V2": 0.1}}, "c1": {"voice": {"V2": 1.0}}}}
    assert xs.desired_matches(cfg, "voice", {"weights": {"V1": 0.9, "V2": 0.1}, "cohort_weights": {"c1": {"V2": 1.0}}})
    assert not xs.desired_matches(cfg, "voice", {"weights": {"V1": 0.8, "V2": 0.2}})
    p = xs.desired_patch("voice", {"weights": {"V1": 1.0}, "cohort_weights": {"c1": {"V2": 0.9, "V1": 0.1}}})
    assert p["allocations"]["c1"]["voice"] == {"V2": 0.9, "V1": 0.1} and p["allocations"]["default"]["voice"] == {"V1": 1.0}
    assert xs.top_option({"V1": 0.5, "V2": 0.5}) == "V1" and xs.top_option({"V1": 0.2, "V3": 0.8}) == "V3"


def test_pre_promotion_reference_catches_a_decay_that_was_already_under_way():
    # level before promotion 1.10; the winner goes live already at ~1.0: relative to its first post blocks nothing changes
    flat_after = blocks([1.0] * 10)
    assert not xd.cusum(flat_after)["alarm"]
    ref = (600.0, 1.10 * 300, 300.0, 600 * 0.25)  # (n, engaged, expected, variance) over the last 48 h before promotion
    c = xd.cusum(flat_after, ref)
    assert c["alarm"] and c["reference"].startswith("pre-promotion")


def test_registry_veto_returns_the_first_reason_and_clear_removes_hooks():
    from controller.desired import Registry
    reg = Registry()
    reg.register_veto(lambda ctx, wid, kind: None)
    assert reg.veto(None, "w", "experiment_promote") is None
    reg.register_veto(lambda ctx, wid, kind: f"stale {kind}")
    assert reg.veto(None, "w", "experiment_promote") == "stale experiment_promote"
    reg.clear()
    assert reg.veto(None, "w", "experiment_promote") is None


def rows(n_leads, rate, per_lead=3, seed=0, offset=0):
    rng = random.Random(seed)
    return {offset + i: (float(per_lead), float(sum(rng.random() < rate for _ in range(per_lead)))) for i in range(n_leads)}


def test_information_epoch_is_finite_and_grows_with_a_smaller_design_lift():
    assert 100 < xd.epoch_information() < 400


def test_sequential_alarm_stays_quiet_on_a_flat_winner_and_fires_on_a_clear_decay():
    ctl = rows(900, 0.5, seed=1, offset=10_000)
    flat = es.contrast(rows(900, 0.5, seed=2), ctl)
    out = xd.sequential_alarm(flat, [])
    assert not out["alarm"]
    bad = es.contrast(rows(900, 0.38, seed=3), ctl)
    out = xd.sequential_alarm(bad, [])
    assert out["look"] and out["alarm"] and out["z"] < -out["boundary"]


def test_sequential_alarm_takes_a_look_only_when_information_has_grown_by_a_tenth():
    ctl = rows(300, 0.5, seed=1, offset=10_000)
    c = es.contrast(rows(300, 0.5, seed=2), ctl)
    first = xd.sequential_alarm(c, [])
    assert first["look"] is (first["t"] >= 0.1)
    again = xd.sequential_alarm(c, first["ts"] if first["look"] else [first["t"]])
    assert not again["look"]  # no new information


def test_sequential_alarm_needs_control_connects():
    c = es.contrast(rows(5, 0.9, seed=2), rows(5, 0.1, seed=1, offset=100))
    assert not xd.sequential_alarm(c, [])["look"]


def test_stage_two_gate_needs_time_recent_lower_bound_and_positive_concurrent_contrast():
    ctl = rows(900, 0.45, seed=1, offset=10_000)
    good = es.contrast(rows(900, 0.55, seed=2), ctl)
    flat = es.contrast(rows(900, 0.45, seed=3), ctl)
    assert xd.stage2_ready(1500, good, good)[0]
    assert not xd.stage2_ready(1300, good, good)[0]  # < 24 h
    assert not xd.stage2_ready(1500, good, flat)[0]  # recent lower bound not above 0
    assert not xd.stage2_ready(1500, flat if flat.lift <= 0 else None, good)[0]
    assert not xd.stage2_ready(1500, None, good)[0]


def test_apply_time_lower_bound_uses_the_80_percent_one_sided_bound():
    mild = es.contrast(rows(100, 0.5, seed=9), rows(100, 0.5, seed=7, offset=10_000))
    assert mild.lift > 0
    assert not xd.veto_lower_bound(mild)[0]  # a bare positive point estimate is not enough
    strong = es.contrast(rows(900, 0.6, seed=3), rows(900, 0.5, seed=4, offset=10_000))
    assert xd.veto_lower_bound(strong)[0] and not xd.veto_lower_bound(None)[0]
