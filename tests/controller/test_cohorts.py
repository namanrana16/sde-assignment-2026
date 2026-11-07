import pytest

from controller.cohorts import CohortChecker, FeatureVocab, assign_cohort, matches

F = {"lang": "ta", "city_tier": "2", "age_band": "60+", "value_band": "low"}


def test_and_across_or_within():
    assert matches({"age_band": ["60+", "46-60"], "lang": ["ta"]}, F)
    assert not matches({"age_band": ["60+"], "lang": ["hi"]}, F)
    assert matches({}, F)


def test_first_match_wins_and_default():
    cohorts = [{"id": "a", "where": {"lang": ["ta"]}}, {"id": "b", "where": {"age_band": ["60+"]}}]
    assert assign_cohort(cohorts, F) == "a"
    assert assign_cohort(list(reversed(cohorts)), F) == "b"
    assert assign_cohort(cohorts, {**F, "lang": "hi", "age_band": "18-30"}) == "default"
    assert assign_cohort([], F) == "default"


def test_vocab_roundtrip_and_persistence():
    v = FeatureVocab()
    cell = v.encode(F)
    assert v.dirty and v.decode(cell) == F
    v2 = FeatureVocab.load(v.dump())
    assert v2.decode(cell) == F and not v2.dirty
    assert v2.encode({**F, "lang": "hi"}) != cell


def test_vocab_overflow():
    v = FeatureVocab()
    with pytest.raises(ValueError):
        for i in range(300):
            v.encode({**F, "lang": str(i)})


def test_checker_memoises():
    v = FeatureVocab()
    cell = v.encode(F)
    chk = CohortChecker(v)
    cohorts = [{"id": "a", "where": {"lang": ["ta"]}}]
    assert chk.expected("w", 1, cohorts, cell) == "a"
    assert chk.expected("w", 1, [], cell) == "a"  # memo keyed by (wf, version, cell)
    assert chk.expected("w", 2, [], cell) == "default"
