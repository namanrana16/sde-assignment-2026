"""The CUSUM reference must not carry the winner's curse (selection on a lucky pre-promotion window)."""
import pytest

from controller import exp_decay as xd, settings


def _blocks(ratios, n=150, p=0.5):
    return [(float(n), r * n * p, n * p, n * p * (1 - p)) for r in ratios]


def _reference(ratio, n=600, p=0.5):
    return (float(n), ratio * n * p, n * p, n * p * (1 - p))


def test_selected_high_reference_does_not_alarm_a_winner_that_stays_at_its_true_level():
    # promoted after a lucky 48 h at +20%; it then holds its true +5% level for 20 blocks
    out = xd.cusum(_blocks([1.05] * 20), _reference(1.20))
    assert not out["alarm"]
    assert out["ref_ratio"] <= 1.0 + settings.EXP_CUSUM_REF_CAP_LIFT + 1e-6


def test_a_real_decay_still_alarms_from_a_selected_high_reference():
    out = xd.cusum(_blocks([1.05] * 3 + [0.95, 0.90, 0.88, 0.85, 0.85, 0.85, 0.85]), _reference(1.20))
    assert out["alarm"]


def test_winner_falling_below_the_old_control_alarms():
    assert xd.cusum(_blocks([0.85] * 10), _reference(1.04))["alarm"]


def test_a_reference_below_the_cap_is_untouched():
    below_cap = 1.0 + settings.EXP_CUSUM_REF_CAP_LIFT / 3
    out = xd.cusum(_blocks([1.0] * 5), _reference(below_cap))
    assert out["ref_ratio"] == pytest.approx(below_cap, abs=1e-4)


def test_first_blocks_reference_is_not_capped():
    # no pre-promotion level: the reference is the winner's own first blocks after promotion, which carry no selection
    above_cap = 1.0 + 4 * settings.EXP_CUSUM_REF_CAP_LIFT
    out = xd.cusum(_blocks([above_cap] * 10))
    assert out["ref_ratio"] == pytest.approx(above_cap, abs=1e-4) and not out["alarm"]
