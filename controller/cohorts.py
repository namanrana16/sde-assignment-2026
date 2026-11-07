"""Cell encoding and cohort predicate matching (first match wins)."""
from __future__ import annotations

import json
from typing import Mapping, Sequence

FEATURES = ("lang", "city_tier", "age_band", "value_band")
DEFAULT_COHORT = "default"
VOCAB_META_KEY = "feature_vocab"


class FeatureVocab:
    """Interns feature values per feature; a cell is four 8-bit codes packed in an int."""

    def __init__(self, tables: Mapping[str, Sequence[str]] | None = None) -> None:
        self._values: dict[str, list[str]] = {f: [] for f in FEATURES}
        self._index: dict[str, dict[str, int]] = {f: {} for f in FEATURES}
        self.dirty = False
        for feature, values in (tables or {}).items():
            for value in values:
                self._code(feature, value)
        self.dirty = False

    def _code(self, feature: str, value: str) -> int:
        idx = self._index[feature]
        code = idx.get(value)
        if code is None:
            code = len(self._values[feature])
            if code > 255:
                raise ValueError(f"too many distinct values for feature {feature}")
            self._values[feature].append(value)
            idx[value] = code
            self.dirty = True
        return code

    def encode(self, features: Mapping[str, str]) -> int:
        cell = 0
        for feature in FEATURES:
            cell = (cell << 8) | self._code(feature, str(features[feature]))
        return cell

    def decode(self, cell: int) -> dict[str, str]:
        out: dict[str, str] = {}
        for feature in reversed(FEATURES):
            out[feature] = self._values[feature][cell & 0xFF]
            cell >>= 8
        return {f: out[f] for f in FEATURES}

    def values(self, feature: str) -> list[str]:
        return list(self._values.get(feature, []))

    def dump(self) -> str:
        return json.dumps(self._values)

    @classmethod
    def load(cls, text: str | None) -> "FeatureVocab":
        return cls(json.loads(text)) if text else cls()


def matches(where: Mapping[str, Sequence[str]], features: Mapping[str, str]) -> bool:
    """AND across features, OR within a feature's list. Empty ``where`` matches all."""
    return all(features.get(feature) in allowed for feature, allowed in where.items())


def assign_cohort(cohorts: Sequence[Mapping], features: Mapping[str, str]) -> str:
    """First matching cohort in order, otherwise ``default``."""
    for cohort in cohorts:
        if matches(cohort.get("where", {}), features):
            return cohort["id"]
    return DEFAULT_COHORT


class CohortChecker:
    """Compares our cohort assignment with the event's ``cohort_id`` (CONTEXT C2 self-check)."""

    def __init__(self, vocab: FeatureVocab) -> None:
        self._vocab = vocab
        self._memo: dict[tuple[str, int, int], str] = {}

    def expected(self, workflow_id: str, version: int, cohorts: Sequence[Mapping],
                 cell: int) -> str:
        key = (workflow_id, version, cell)
        got = self._memo.get(key)
        if got is None:
            got = assign_cohort(cohorts, self._vocab.decode(cell))
            self._memo[key] = got
        return got


# --------------------------------------------------------------------------- C2: data-driven partition
import itertools
import math
from dataclasses import dataclass, field

from . import settings


@dataclass(frozen=True)
class Partition:
    features: tuple[str, ...]
    cohorts: tuple[dict, ...]  # [{"id", "where"}], pairwise disjoint (a partition: first-match order cannot matter)
    score: float  # Wilson-Hilferty z of the group x hour interaction likelihood-ratio statistic
    gain: float  # raw likelihood-ratio statistic
    df: int
    n_min: int
    candidates: dict = field(default_factory=dict)  # features -> score, for the audit trail

    def as_dict(self) -> dict:
        return {"features": list(self.features), "cohorts": list(self.cohorts), "score": round(self.score, 2),
                "gain": round(self.gain, 2), "df": self.df, "n_min": self.n_min, "candidates": self.candidates}


def interaction_z(stat: float, df: int) -> float:
    """Normal score of a chi-square statistic (Wilson-Hilferty): comparable across candidates with different df."""
    if df <= 0:
        return 0.0
    c = 2.0 / (9.0 * df)
    return ((stat / df) ** (1.0 / 3.0) - (1.0 - c)) / math.sqrt(c)


def cohort_id(features: tuple[str, ...], values: tuple[str, ...]) -> str:
    return "&".join(f"{f}={v}" for f, v in zip(features, values))


def n_min(n_hours: int) -> int:
    """Smallest cohort volume (dials in the learning window): ``COHORT_MIN_DIALS_PER_HOUR`` per in-window hour."""
    return settings.COHORT_MIN_DIALS_PER_HOUR * max(n_hours, 1)


def _deviance(x: float, n: float, e: float) -> float:
    d = 0.0
    if x > 0 and e > 0:
        d += x * math.log(x / e)
    if n - x > 0 and n - e > 0:
        d += (n - x) * math.log((n - x) / (n - e))
    return 2.0 * d


def interaction_gain(counts: dict[str, dict[int, list[int]]], hours: list[int]) -> tuple[float, int]:
    """LR statistic of group x hour interaction over a separable (group + hour) multiplicative model, and its df.

    The separable model keeps pickup LEVEL differences between groups out of the score: only groups whose hour
    PROFILE differs (what per-cohort hour weights can exploit) score.
    """
    groups = sorted(counts)
    if len(groups) < 2 or len(hours) < 2:
        return 0.0, 0
    n = {g: [counts[g].get(h, [0, 0])[0] for h in hours] for g in groups}
    x = {g: [counts[g].get(h, [0, 0])[1] for h in hours] for g in groups}
    a = {g: sum(x[g]) / max(sum(n[g]), 1) for g in groups}
    b = [1.0] * len(hours)
    for _ in range(25):  # iterative proportional fitting of E = a_g b_h n_gh to the margins
        for j in range(len(hours)):
            den = sum(a[g] * n[g][j] for g in groups)
            b[j] = sum(x[g][j] for g in groups) / den if den > 0 else 1.0
        for g in groups:
            den = sum(b[j] * n[g][j] for j in range(len(hours)))
            a[g] = sum(x[g]) / den if den > 0 else 0.0
    stat = sum(_deviance(x[g][j], n[g][j], min(a[g] * b[j], 0.999) * n[g][j])
               for g in groups for j in range(len(hours)) if n[g][j] > 0)
    return max(stat, 0.0), (len(groups) - 1) * (len(hours) - 1)


def _group(rows, vocab: FeatureVocab, feats: tuple[str, ...], min_dials: int, cap: int):
    """Counts per kept group (value tuple) plus the pooled remainder; groups below ``min_dials`` go to the remainder."""
    totals: dict[tuple, int] = {}
    per: dict[tuple, dict[int, list[int]]] = {}
    for hour, cell, dials, conns in rows:
        values = vocab.decode(cell)
        key = tuple(values[f] for f in feats)
        totals[key] = totals.get(key, 0) + dials
        slot = per.setdefault(key, {}).setdefault(hour, [0, 0])
        slot[0] += dials
        slot[1] += conns
    kept = sorted((k for k, v in totals.items() if v >= min_dials), key=lambda k: (-totals[k], k))[:cap]
    rest: dict[int, list[int]] = {}
    for key, by_hour in per.items():
        if key in kept:
            continue
        for hour, (n, c) in by_hour.items():
            s = rest.setdefault(hour, [0, 0])
            s[0] += n
            s[1] += c
    return kept, per, rest


def select_partition(rows, vocab: FeatureVocab, hours: list[int]) -> Partition | None:
    """Pick the feature (or pair) whose groups best explain hour-of-day pickup variation (largest BIC-adjusted
    likelihood-ratio statistic of the group x hour interaction, as a df-comparable z, above the multiple-testing
    bar ``COHORT_MIN_Z``); groups with volume < n_min fall into ``default``.

    ``rows`` are ``(hour, cell, dials, connects)`` over the learning window. Data-driven only; None when no
    partition beats the pooled profile.
    """
    rows = [r for r in rows if r[0] in set(hours)]
    floor = n_min(len(hours))
    best: Partition | None = None
    scores: dict[str, float] = {}
    for k in range(1, settings.COHORT_FEATURE_SUBSETS + 1):
        for feats in itertools.combinations(FEATURES, k):
            kept, per, rest = _group(rows, vocab, feats, floor, settings.COHORT_MAX)
            if not kept:
                continue
            groups = {k_: per[k_] for k_ in kept}
            if rest:
                groups[("~rest",)] = rest
            stat, df = interaction_gain(groups, hours)
            score = interaction_z(stat, df)
            scores["+".join(feats)] = round(score, 2)
            bar = settings.COHORT_MIN_Z + (settings.COHORT_PAIR_MARGIN_Z if k == 2 and best is not None
                                           and len(best.features) == 1 else 0.0)
            if score >= settings.COHORT_MIN_Z and (best is None or score > best.score + (bar - settings.COHORT_MIN_Z)):
                cohorts = tuple({"id": cohort_id(feats, key), "where": {f: [v] for f, v in zip(feats, key)}}
                                for key in kept)
                best = Partition(feats, cohorts, score, stat, df, floor)
    if best is None:
        return None
    return Partition(best.features, best.cohorts, best.score, best.gain, best.df, best.n_min, scores)


def is_partition(cohorts: Sequence[Mapping], vocab: FeatureVocab) -> bool:
    """True if no cell matches two cohorts (so first-match order cannot matter)."""
    seen: set[int] = set()
    for c in cohorts:
        cells = {cell for cell in _all_cells(vocab) if matches(c.get("where", {}), vocab.decode(cell))}
        if cells & seen:
            return False
        seen |= cells
    return True


def _all_cells(vocab: FeatureVocab):
    pools = [range(len(vocab.values(f))) for f in FEATURES]
    for combo in itertools.product(*pools):
        cell = 0
        for code in combo:
            cell = (cell << 8) | code
        yield cell
