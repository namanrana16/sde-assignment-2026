"""exp_stats: clustered contrasts, Lan-DeMets OBF boundaries, Holm."""
import math
import random

import pytest

from controller import exp_stats as es


def test_first_look_boundary_matches_the_spending_function():
    b = es.obf_boundary([0.25], 0.05)
    assert b == pytest.approx(3.75, abs=0.02)  # 2(1-Phi(1.96/0.5)) = 8.9e-5 -> z 3.74


def test_final_boundary_is_near_nominal_and_boundaries_decrease():
    ts = [0.25, 0.5, 0.75, 1.0]
    bs = [es.obf_boundary(ts[:k], 0.05) for k in range(1, 5)]
    assert bs == sorted(bs, reverse=True)
    assert 1.6 < bs[-1] < 1.85 and bs[0] > 3.5


def test_boundaries_hold_the_overall_one_sided_alpha_under_the_null():
    ts = [0.2, 0.45, 0.7, 1.0]
    bs = [es.obf_boundary(ts[:k], 0.05) for k in range(1, 5)]
    rng = random.Random(7)
    hits, n = 0, 20000
    for _ in range(n):
        b, prev = 0.0, 0.0
        crossed = False
        for t, bound in zip(ts, bs):
            b += rng.gauss(0, math.sqrt(t - prev))
            prev = t
            if b / math.sqrt(t) >= bound:
                crossed = True
                break
        hits += crossed
    assert hits / n == pytest.approx(0.05, abs=0.007)


def test_spent_alpha_is_monotone_and_exhausted_at_one():
    assert es.obf_spend(0.5, 0.05) < es.obf_spend(0.8, 0.05) < es.obf_spend(1.0, 0.05) == 0.05


def test_contrast_matches_iid_formula_without_clustering():
    arm = {i: (1.0, 1.0 if i < 60 else 0.0) for i in range(100)}
    ctl = {1000 + i: (1.0, 1.0 if i < 50 else 0.0) for i in range(100)}
    c = es.contrast(arm, ctl)
    assert c.rate_a == 0.6 and c.rate_c == 0.5 and c.lift == pytest.approx(0.2)
    iid = math.sqrt(0.4 / (0.6 * 100) + 0.5 / (0.5 * 100))
    assert c.se == pytest.approx(iid, rel=0.06)
    assert c.z > 0 and c.lift_lower(1.645) < c.lift


def test_clustering_widens_the_standard_error():
    rng = random.Random(1)
    arm, ctl = {}, {}
    for i in range(60):  # leads with 5 connects each, all-or-nothing engagement (strong clustering)
        arm[i] = (5.0, 5.0 * (rng.random() < 0.5))
        ctl[100 + i] = (5.0, 5.0 * (rng.random() < 0.5))
    iid_like = {}
    for i in range(300):
        iid_like[i] = (1.0, float(rng.random() < 0.5))
    iid_ctl = {1000 + i: (1.0, float(rng.random() < 0.5)) for i in range(300)}
    assert es.contrast(arm, ctl).se > 1.5 * es.contrast(iid_like, iid_ctl).se


def test_shared_leads_are_handled_jointly():
    arm = {1: (2.0, 2.0), 2: (2.0, 0.0)}
    ctl = {1: (2.0, 2.0), 2: (2.0, 0.0)}
    c = es.contrast(arm, ctl)
    assert c.effect == 0 and c.se == pytest.approx(0.0, abs=1e-12)  # identical leads in both arms: no noise


def test_empty_side_gives_none():
    assert es.contrast({}, {1: (1.0, 1.0)}) is None
    assert es.contrast({1: (1.0, 0.0)}, {1: (1.0, 1.0)}) is None  # log scale needs events


def test_diff_scale_for_pickup():
    arm = {i: (3.0, 1.0) for i in range(50)}
    ctl = {100 + i: (3.0, 1.0 if i % 2 else 0.0) for i in range(50)}
    c = es.contrast(arm, ctl, scale="diff")
    assert c.effect == pytest.approx(1 / 3 - 1 / 6)


def test_holm_stops_at_first_failure_and_alpha_adjusts():
    seen = []

    def boundary(a, arm):
        seen.append(a)
        return 2.0
    out = es.holm_order({"A": 3.0, "B": 2.5, "C": 1.0}, 3, 0.06, boundary)
    assert [out[a]["rejected"] for a in "ABC"] == [True, True, False]
    assert seen == [0.02, 0.03, 0.06]
    out = es.holm_order({"A": 1.0, "B": 5.0}, 2, 0.05, lambda a, arm: 2.0)
    assert out["B"]["rejected"] and not out["A"]["rejected"]


def test_stratified_single_stratum_equals_plain_contrast():
    rng = random.Random(3)
    arm = {i: (3.0, float(sum(rng.random() < 0.55 for _ in range(3)))) for i in range(200)}
    ctl = {1000 + i: (3.0, float(sum(rng.random() < 0.45 for _ in range(3)))) for i in range(200)}
    a, b = es.contrast(arm, ctl), es.contrast_strata([(arm, ctl)])
    assert b.effect == pytest.approx(a.effect) and b.se == pytest.approx(a.se) and b.z == pytest.approx(a.z)


def test_stratification_removes_share_and_base_rate_confounding():
    # day 1: arm is 90% of traffic at a LOW base rate; day 2: arm is 10% at a HIGH base rate; true lift 0
    d1_arm = {i: (1.0, float(i % 10 < 3)) for i in range(900)}
    d1_ctl = {5000 + i: (1.0, float(i % 10 < 3)) for i in range(100)}
    d2_arm = {10000 + i: (1.0, float(i % 10 < 7)) for i in range(100)}
    d2_ctl = {20000 + i: (1.0, float(i % 10 < 7)) for i in range(900)}
    pooled_arm, pooled_ctl = {**d1_arm, **d2_arm}, {**d1_ctl, **d2_ctl}
    assert es.contrast(pooled_arm, pooled_ctl).effect < -0.3  # Simpson: the naive pooled contrast is badly biased
    c = es.contrast_strata([(d1_arm, d1_ctl), (d2_arm, d2_ctl)])
    assert abs(c.effect) < 0.02


def test_stratified_empty_strata_are_skipped_and_none_when_nothing_to_compare():
    arm = {1: (2.0, 1.0)}
    assert es.contrast_strata([]) is None and es.contrast_strata([(arm, {})]) is None
    ctl = {2: (2.0, 1.0)}
    assert es.contrast_strata([(arm, {}), (arm, ctl)]).effect == pytest.approx(0.0)


def test_a_lead_in_two_strata_is_one_cluster():
    arm1, ctl1 = {1: (2.0, 2.0), 2: (2.0, 0.0)}, {3: (2.0, 1.0), 4: (2.0, 1.0)}
    c = es.contrast_strata([(arm1, ctl1), (arm1, ctl1)])
    assert c.clusters == 4


def test_look_after_the_full_information_look_has_no_alpha_left():
    assert math.isfinite(es.obf_boundary([0.5, 1.0], 0.05))
    assert es.obf_boundary([0.5, 1.0, 1.0], 0.05) == math.inf
    assert es.obf_boundary([0.5, 1.0, 1.4], 0.05) == math.inf
    assert math.isfinite(es.obf_boundary([0.5, 0.5], 0.05))  # same information: the earlier boundary stands
