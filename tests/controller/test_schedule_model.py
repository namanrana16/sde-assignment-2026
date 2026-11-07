"""B2: per-cohort hour multipliers, weights tables, outcome estimates."""
import random

from controller import schedule_model as sm

HOURS = list(range(10, 20))


def rows_for(profiles, n=1600, seed=5, survivorship=False):
    """strata rows; profiles: cohort -> hour -> rate. Cell 1 = cohort A, cell 2 = cohort B."""
    rnd = random.Random(seed)
    rows = []
    for cohort, cell in (("A", 1), ("B", 2)):
        for h in HOURS:
            for att in (1, 2):
                rate = profiles[cohort][h] * (0.7 if (survivorship and att == 2) else 1.0)
                nn = n if att == 1 else n // 2
                rows.append((cell, h, att, 0, nn, sum(rnd.random() < rate for _ in range(nn)), 0))
    return rows


MORNING = {h: 0.3 * (1.5 - 0.1 * (h - 10)) for h in HOURS}
EVENING = {h: 0.3 * (0.6 + 0.1 * (h - 10)) for h in HOURS}
assign = lambda cell: "A" if cell == 1 else "B"  # noqa: E731


def test_weights_follow_cohort_profile_mean_one_clipped_and_complete():
    m = sm.fit(rows_for({"A": MORNING, "B": EVENING}), assign, HOURS)
    t = sm.tables(m, ["A", "B"])
    assert set(t) == {"A", "B", "default"}
    for tab in t.values():
        assert set(tab) == {str(h) for h in HOURS} and all(0.3 <= w <= 3.0 for w in tab.values())
        assert abs(sum(tab.values()) / len(tab) - 1) < 0.02
    assert t["A"]["10"] > t["A"]["19"] and t["B"]["19"] > t["B"]["10"]


def test_shrinkage_pulls_noisy_cohort_toward_pooled():
    flat = {h: 0.3 for h in HOURS}
    m_flat = sm.fit(rows_for({"A": flat, "B": flat}, n=400), assign, HOURS)
    assert m_flat.tau2 < 0.01
    spread = max(m_flat.mhat["A"].values()) - min(m_flat.mhat["A"].values())
    raw = sm.fit(rows_for({"A": MORNING, "B": EVENING}, n=400), assign, HOURS)
    assert spread < max(raw.mhat["A"].values()) - min(raw.mhat["A"].values())


def test_survivorship_strata_remove_attempt_mix_confound():
    # attempt 2 (lower pickup) is dialled only in late hours -> raw late-hour pickup looks worse
    rnd, rows = random.Random(1), []
    for h in HOURS:
        for att, nn in ((1, 1600 if h < 15 else 400), (2, 400 if h < 15 else 1600)):
            rate = 0.3 * (0.6 if att == 2 else 1.0)
            rows.append((1, h, att, 0, nn, sum(rnd.random() < rate for _ in range(nn)), 0))
    m = sm.fit(rows, lambda cell: "A", HOURS)
    late = sum(m.mhat["A"][h] for h in range(15, 20)) / 5
    early = sum(m.mhat["A"][h] for h in range(10, 15)) / 5
    assert abs(late / early - 1) < 0.08  # hour effect is flat by construction


def test_outcome_lift_positive_and_flat_tables_are_neutral():
    m = sm.fit(rows_for({"A": MORNING, "B": EVENING}), assign, HOURS)
    flat = {}
    new = sm.tables(m, ["A", "B"])
    assert abs(sm.outcome(m, flat, flat)["rel_lift"]) < 1e-9
    assert sm.outcome(m, new, flat)["rel_lift"] > 0


def test_engagement_drop_is_reported_absolute():
    rows = [(c, h, 1, 0, 400, 120, 60 if (c == 1) else 60 + (h - 10) * 5) for c in (1, 2) for h in HOURS]
    m = sm.fit(rows, assign, HOURS)
    out = sm.outcome(m, sm.tables(m, ["A", "B"]), {})
    assert "engagement_drop" in out


def test_verify_change_flags_a_pickup_drop_standardised_to_the_before_mix():
    before = {(h, c): (500, 150, 75) for h in HOURS for c in "AB"}
    after_ok = {k: (500, 150, 75) for k in before}
    after_bad = {k: (500, 100, 50) for k in before}
    ok, bad = sm.verify_change(before, after_ok), sm.verify_change(before, after_bad)
    assert abs(ok["pickup"]["drop"]) < 1e-9 and ok["pickup"]["p_drop"] > 0.4
    assert bad["pickup"]["drop"] > 0.09 and bad["pickup"]["p_drop"] < 1e-6


def test_verify_change_ignores_a_pure_retiming():
    before = {(h, "A"): (500 if h < 15 else 100, 150 if h < 15 else 30, 0) for h in HOURS}
    after = {(h, "A"): (100 if h < 15 else 500, 30 if h < 15 else 150, 0) for h in HOURS}
    res = sm.verify_change(before, after)
    assert abs(res["pickup"]["drop"]) < 1e-9  # same hour-cell rates: the mix shift is not a drop
