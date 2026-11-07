"""B1: data-driven cohort partition."""
import random

from controller import cohorts as co
from controller.cohorts import FEATURES, FeatureVocab

HOURS = list(range(10, 20))
LANGS, TIERS = ("hi", "en", "ta"), ("1", "2")


def vocab_and_rows(profile, seed=3, per_cell_hour=3000):
    """rows (hour, cell, dials, connects); ``profile(lang, tier, hour)`` gives the true pickup rate."""
    rnd = random.Random(seed)
    vocab, rows = FeatureVocab(), []
    for lang in LANGS:
        for tier in TIERS:
            cell = vocab.encode({"lang": lang, "city_tier": tier, "age_band": "31-45", "value_band": "mid"})
            for h in HOURS:
                n = per_cell_hour
                rows.append((h, cell, n, sum(rnd.random() < profile(lang, tier, h) for _ in range(n))))
    return vocab, rows


def lang_profile(lang, tier, h):
    shape = {"hi": 1.0 + 0.08 * (19 - h), "en": 1.0 + 0.08 * (h - 10), "ta": 1.0}[lang]
    return 0.15 * shape


def test_picks_the_feature_that_drives_hour_profiles():
    vocab, rows = vocab_and_rows(lang_profile)
    part = co.select_partition(rows, vocab, HOURS)
    assert part is not None and part.features == ("lang",)
    assert {c["id"] for c in part.cohorts} == {"lang=hi", "lang=en", "lang=ta"}


def test_partition_is_non_overlapping():
    vocab, rows = vocab_and_rows(lang_profile)
    part = co.select_partition(rows, vocab, HOURS)
    assert co.is_partition(list(part.cohorts), vocab)
    assert not co.is_partition([{"id": "a", "where": {"lang": ["hi"]}}, {"id": "b", "where": {"city_tier": ["1"]}}], vocab)


def test_no_partition_when_hour_profile_is_shared():
    vocab, rows = vocab_and_rows(lambda lang, tier, h: (0.1 + 0.02 * (h - 10)) * (1.5 if lang == "hi" else 1.0))
    assert co.select_partition(rows, vocab, HOURS) is None  # level differences alone are not hour variation


def test_small_groups_fall_into_default_and_cap_is_respected():
    vocab, rows = vocab_and_rows(lang_profile, per_cell_hour=3000)
    # 'ta' cells shrunk below n_min (100 dials x 10 hours = 1000): they are not listed as a cohort
    thin = [(h, c, n // 100 if vocab.decode(c)["lang"] == "ta" else n, x // 100 if vocab.decode(c)["lang"] == "ta" else x)
            for h, c, n, x in rows]
    part = co.select_partition(thin, vocab, HOURS)
    ids = {c["id"] for c in part.cohorts}
    assert "lang=ta" not in ids and len(part.cohorts) <= 20 and part.n_min == 1000


def test_n_min_scales_with_window_hours():
    assert co.n_min(10) == 1000 and co.n_min(12) == 1200
