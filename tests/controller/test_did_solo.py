"""A1: own-history step test for a DID without peers."""
import random

from controller import settings
from controller.detectors import did_solo

HOURS = range(10, 20)
HOUR_RATE = {h: 0.2 + 0.03 * (h - 10) for h in HOURS}  # strong hour-of-day profile
CELLS = (1, 2)


def make_rows(days, drop_from=None, drop=0.5, seed=1, n_per_cell=100, mix_shift=None):
    rnd = random.Random(seed)
    rows = {}
    for d in range(days):
        for h in HOURS:
            ah = d * 24 + h
            for cell in CELLS:
                n = n_per_cell
                rate = HOUR_RATE[h] * (1.0 if cell == 1 else 0.6)
                if drop_from is not None and ah >= drop_from:
                    rate *= 1 - drop
                x = sum(rnd.random() < rate for _ in range(n))
                rows[(ah, cell)] = (n, x)
    return rows


def test_no_step_on_stable_series_despite_hour_profile():
    rows = make_rows(4)
    cur = 3 * 24 + 14
    res = did_solo.solo_steps(rows, "D1", cur, lambda k: 1.0)
    assert res and all(not r.flagged() for r in res)
    assert all(abs(r.test.drop) < 0.2 for r in res)


def test_detects_half_pickup_step():
    rows = make_rows(4, drop_from=3 * 24 + 11)
    cur = 3 * 24 + 14
    res = did_solo.solo_steps(rows, "D1", cur, lambda k: 1.0)
    assert any(r.flagged() for r in res)
    best = max(res, key=lambda r: r.test.drop)
    assert 0.35 < best.test.drop < 0.65 and best.solo


def test_fatigue_prediction_absorbs_expected_decline():
    rows = make_rows(4, drop_from=3 * 24 + 11, drop=0.45)
    cur = 3 * 24 + 17
    flagged = did_solo.solo_steps(rows, "D1", cur, lambda k: 1.0)
    explained = did_solo.solo_steps(rows, "D1", cur, lambda k: 0.55)
    assert any(r.flagged() for r in flagged)
    assert not any(r.flagged() for r in explained)


def test_solo_level_is_stricter_than_peer_level():
    rows = make_rows(4, drop_from=3 * 24 + 11)
    r = max(did_solo.solo_steps(rows, "D1", 3 * 24 + 14, lambda k: 1.0), key=lambda r: r.test.drop)
    assert r.flagged(settings.DID_FLAG_P) and not r.flagged(r.test.p / settings.DID_SOLO_P_FACTOR / 10)


def test_too_little_data_gives_no_result():
    assert did_solo.solo_steps(make_rows(1), "D1", 14, lambda k: 1.0) == []
