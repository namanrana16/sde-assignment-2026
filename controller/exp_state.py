"""Experiment state, live-config reading and pre-registered analysis groups (C6)."""
from __future__ import annotations

import json
import re
from typing import Mapping

from . import cube, settings
from .cohorts import FeatureVocab, assign_cohort
from .detectors.common import load_state, save_state
from .dryrun import cells_by_cohort

DETECTOR = "experiments"
DIMS = ("welcome_message", "voice")
DAY = settings.MINUTES_PER_DAY
TOL = 1e-4  # weights are rounded to 6 digits before the sim normalises them
PENDING, RUNNING, PROMOTED, CLOSED = "pending", "running", "promoted", "closed"


def key(wid: str, dim: str) -> str:
    return f"{wid}:{dim}"


def load(store, wid: str, dim: str) -> dict:
    return load_state(store, DETECTOR, key(wid, dim))


def save(store, wid: str, dim: str, st: Mapping) -> None:
    save_state(store, DETECTOR, key(wid, dim), st)


def clock(minute: int) -> str:
    return f"Day {minute // DAY} {minute % DAY // 60:02d}:{minute % 60:02d}"


def eligible(wf: Mapping, dim: str) -> bool:
    return (settings.ENABLE_EXPERIMENTS and wf["objective"]["primary"] == "engagement_rate"
            and len(wf["options"].get(dim, [])) > 1)


def live_dist(cfg: Mapping, dim: str, cohort: str = "default") -> dict[str, float]:
    allocs = cfg.get("allocations", {})
    return dict((allocs.get(cohort) or {}).get(dim) or allocs["default"][dim])


def cohort_overrides(cfg: Mapping, dim: str) -> dict[str, dict[str, float]]:
    """Cohorts that carry their own allocation for ``dim`` (their calls are not served by the default split)."""
    return {cid: dict(a[dim]) for cid, a in (cfg.get("allocations") or {}).items() if cid != "default" and (a or {}).get(dim)}


def same_overrides(a: Mapping[str, Mapping[str, float]], b: Mapping[str, Mapping[str, float]]) -> bool:
    return set(a) == set(b) and all(same_dist(a[c], b[c]) for c in a)


def top_option(dist: Mapping[str, float]) -> str:
    """The production option: max weight, ties broken by option id (deterministic)."""
    return max(sorted(dist), key=lambda o: dist[o])


def same_dist(a: Mapping[str, float], b: Mapping[str, float], tol: float = TOL) -> bool:
    return all(abs(a.get(o, 0.0) - b.get(o, 0.0)) <= tol for o in set(a) | set(b))


def desired_matches(cfg: Mapping, dim: str, desired: Mapping) -> bool:
    if not same_dist(live_dist(cfg, dim), desired["weights"]):
        return False
    return all(same_dist(live_dist(cfg, dim, cid), w) for cid, w in (desired.get("cohort_weights") or {}).items())


def desired_patch(dim: str, desired: Mapping) -> dict:
    allocs = {"default": {dim: desired["weights"]}}
    for cid, w in (desired.get("cohort_weights") or {}).items():
        allocs[cid] = {dim: w}
    return {"allocations": allocs}


def _natural(value: str) -> tuple:
    m = re.match(r"\D*(\d+)", value)
    return (int(m.group(1)) if m else 10 ** 9, value)


def analysis_groups(store, vocab: FeatureVocab, wid: str, cohorts: list, now: int) -> list[dict]:
    """<= 2 pre-registered unions of the cohort partition, each >= EXP_GROUP_MIN_SHARE of traffic.

    Defined from cohort definitions and traffic only (never outcomes): cohorts ordered by the natural order of their
    first feature value, cut where the cumulative traffic share is nearest one half.
    """
    named = [c for c in cohorts if c.get("where")]
    if len(named) < 2:
        return []
    by_cohort = cells_by_cohort(store, vocab, wid, named)
    day = now // DAY
    dials = {r.key[0]: r.dials for r in cube.rollup(store, wid, by=("cell",), day_min=day - 3, day_max=day)}
    total = sum(dials.values())
    if total <= 0:
        return []
    order = sorted(named, key=lambda c: _natural(str(next(iter(next(iter(c["where"].values())))))))
    share = {c["id"]: sum(dials.get(cell, 0) for cell in by_cohort.get(c["id"], [])) / total for c in order}
    best, run = None, 0.0
    for i, c in enumerate(order[:-1]):
        run += share[c["id"]]
        rest = sum(share[o["id"]] for o in order[i + 1:])
        if run >= settings.EXP_GROUP_MIN_SHARE and rest >= settings.EXP_GROUP_MIN_SHARE:
            if best is None or abs(run - 0.5) < abs(best[1] - 0.5) - 1e-12:
                best = (i + 1, run)
    if best is None:
        return []
    out = []
    for name, part in (("g0", order[:best[0]]), ("g1", order[best[0]:])):
        ids = [c["id"] for c in part]
        out.append({"name": name, "cohorts": ids, "cells": sorted(c for i in ids for c in by_cohort.get(i, [])),
                    "share": round(sum(share[i] for i in ids), 4)})
    return out


def override_cells(store, vocab: FeatureVocab, wid: str, cfg: Mapping, dim: str) -> list[int]:
    """Cells served by a cohort that has its own allocation for ``dim`` (first-match cohort assignment)."""
    over = cohort_overrides(cfg, dim)
    if not over:
        return []
    by_cohort = cells_by_cohort(store, vocab, wid, [c for c in cfg.get("cohorts") or [] if c.get("where")])
    return sorted(c for cid in over for c in by_cohort.get(cid, []))


def cohort_signature(cohorts: list) -> str:
    return json.dumps([(c["id"], c["where"]) for c in cohorts], sort_keys=True)


def assign(cohorts: list, vocab: FeatureVocab, cell: int) -> str:
    return assign_cohort(cohorts, vocab.decode(cell))
