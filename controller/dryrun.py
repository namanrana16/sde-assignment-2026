"""Minimal C10: dry run from the cube, impact score, affected leads (CONTEXT section 8, C10).

Honest and simple: observed per-arm rates (marginal, last few days) re-weighted by the new
allocation, per cohort, with a normal-approximation CI. Arms never observed are labelled
``no_evidence`` and contribute no lift. Schedule changes are replayed by hour re-weighting.
Prompt changes are labelled ``validation-only`` (one prompt per workflow: no historical replay).
"""
from __future__ import annotations

import math
from typing import Mapping

from . import cube, settings
from .cohorts import FeatureVocab, assign_cohort
from .configlib import AGENT_DIMS, dist
from .store import Store

Z95 = 1.96
IMPACT_FULL_LIFT = 0.10  # a relative lift of 10% across the whole workflow scores 1.0
_DIM_COLUMN = {"welcome_message": "welcome", "voice": "voice", "caller_id": "caller_id"}


# ------------------------------------------------------------------ cell bookkeeping
def cells_by_cohort(store: Store, vocab: FeatureVocab, wid: str, cohorts: list) -> dict[str, list[int]]:
    """Cell codes seen for the workflow, grouped by first-match cohort (rest -> ``default``)."""
    groups: dict[str, list[int]] = {}
    rows = store.conn.execute("SELECT DISTINCT cell FROM leads WHERE workflow_id=?", (wid,))
    for (cell,) in rows.fetchall():
        groups.setdefault(assign_cohort(cohorts, vocab.decode(cell)), []).append(cell)
    return groups


def active_cell_counts(store: Store, wid: str, now: int, cadence_days: int) -> dict[int, int]:
    """Active in-cadence leads per cell: campaign younger than the cadence, no engaged call yet."""
    rows = store.conn.execute(
        "SELECT l.cell, COUNT(*) FROM leads l JOIN campaigns c ON c.campaign_id=l.campaign_id"
        " WHERE l.workflow_id=? AND ? - ((c.ts_minute - (((c.ts_minute % ?) + ?) % ?)) / ?) < ?"
        " AND NOT EXISTS (SELECT 1 FROM calls k WHERE k.lid=l.lid AND k.engaged=1)"
        " GROUP BY l.cell", (wid, now // settings.MINUTES_PER_DAY, *(settings.MINUTES_PER_DAY,) * 4, cadence_days)).fetchall()
    return {cell: n for cell, n in rows}


def recent_cell_counts(store: Store, wid: str, since_minute: int) -> dict[int, int]:
    rows = store.conn.execute("SELECT cell, COUNT(*) FROM leads WHERE workflow_id=? AND ts_minute>=?"
                              " GROUP BY cell", (wid, since_minute)).fetchall()
    return {cell: n for cell, n in rows}


def _table(cfg: Mapping, cohort: str) -> dict | None:
    hw = cfg["schedule"]["hour_weights"]
    return (hw.get(cohort) if cohort in hw else hw.get("default")) or None  # {} means weight 1


def _agent_changed(old: Mapping, new: Mapping, oc: str, nc: str) -> bool:
    return any(dist(old, oc, d) != dist(new, nc, d) for d in AGENT_DIMS)


def affected_leads(store: Store, vocab: FeatureVocab, wf: Mapping, old: Mapping, new: Mapping,
                   now: int) -> int:
    """Active in-cadence leads whose effective behaviour changes.

    Under ``next_campaign_only`` the agent-facing parts only reach leads of campaigns created
    after the change, so those add the expected next-batch share instead.
    """
    wid = wf["workflow_id"]
    locked = wf.get("mid_flight") == "next_campaign_only"
    prompt = old["prompt_version"] != new["prompt_version"]
    total = 0
    for cell, n in active_cell_counts(store, wid, now, len(wf["cadence"])).items():
        feats = vocab.decode(cell)
        oc, nc = assign_cohort(old["cohorts"], feats), assign_cohort(new["cohorts"], feats)
        sched = _table(old, oc) != _table(new, nc)
        if prompt or sched or (not locked and _agent_changed(old, new, oc, nc)):
            total += n
    if locked:
        recent = recent_cell_counts(store, wid, now - settings.DRYRUN_WINDOW_DAYS * settings.MINUTES_PER_DAY)
        seen = sum(recent.values())
        moved = sum(n for cell, n in recent.items()
                    if _agent_changed(old, new, assign_cohort(old["cohorts"], vocab.decode(cell)),
                                      assign_cohort(new["cohorts"], vocab.decode(cell))))
        if seen:
            total += round(wf["daily_batch"]["size"] * moved / seen)
    return int(total)


# ------------------------------------------------------------------ rates
def _count_pair(row: cube.CubeRow, primary: str) -> tuple[int, int]:
    return (row.engaged, row.connects) if primary == "engagement_rate" else (row.connects, row.dials)


def _rate(successes: int, n: int) -> tuple[float, float]:
    """Smoothed rate and its standard error (Agresti-Coull style)."""
    p = (successes + 1) / (n + 2)
    return p, math.sqrt(p * (1 - p) / (n + 2))


def _expected(weights: Mapping[str, float], rates: Mapping[str, tuple[float, float, int]]) -> tuple:
    """(estimate, variance, weight lacking evidence) over arms with evidence."""
    seen = {a: w for a, w in weights.items() if a in rates and rates[a][2] >= settings.DRYRUN_MIN_ARM_N}
    missing = sum(w for a, w in weights.items() if a not in seen)
    total = sum(seen.values())
    if total <= 0:
        return None, None, missing
    est = sum(w * rates[a][0] for a, w in seen.items()) / total
    var = sum((w / total) ** 2 * rates[a][1] ** 2 for a, w in seen.items())
    return est, var, missing


def _mix(cfg: Mapping, shares: Mapping[int, float], vocab: FeatureVocab, dim: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for cell, share in shares.items():
        coh = assign_cohort(cfg["cohorts"], vocab.decode(cell))
        for arm, w in dist(cfg, coh, dim).items():
            out[arm] = out.get(arm, 0.0) + share * w
    return out


def _agent_entry(store, vocab, wf, old, new, group, cells, shares, window, primary) -> list[dict]:
    wid, entries = wf["workflow_id"], []
    for dim in AGENT_DIMS:
        old_w, new_w = _mix(old, shares, vocab, dim), _mix(new, shares, vocab, dim)
        if all(abs(old_w.get(a, 0) - new_w.get(a, 0)) < 1e-9 for a in set(old_w) | set(new_w)):
            continue
        rates = {}
        for r in cube.rollup(store, wid, by=(_DIM_COLUMN[dim],), cells=cells, **window):
            s, n = _count_pair(r, primary)
            rates[r.key[0]] = (*_rate(s, n), n)
        eo, vo, _ = _expected(old_w, rates)
        en, vn, miss = _expected(new_w, rates)
        arms = {a: ({"rate": round(rates[a][0], 4), "n": rates[a][2],
                     "status": "observed" if rates[a][2] >= settings.DRYRUN_MIN_ARM_N else "no_evidence"}
                    if a in rates else {"status": "no_evidence"})
                for a in sorted(set(old_w) | set(new_w))}
        entry = {"cohort": group, "dim": dim, "kpi": primary, "arms": arms,
                 "old_weights": _round(old_w), "new_weights": _round(new_w),
                 "evidence": "no_evidence", "rel_lift": None, "rel_lift_lb": 0.0}
        if en is not None and eo:
            entry.update(expected_old=round(eo, 4), expected_new=round(en, 4),
                         ci95_new=[round(en - Z95 * math.sqrt(vn), 4), round(en + Z95 * math.sqrt(vn), 4)],
                         rel_lift=round((en - eo) / eo, 4),
                         evidence="observed" if miss < 1e-9 else "partial")
            if entry["evidence"] == "observed":
                entry["rel_lift_lb"] = round((en - eo - Z95 * math.sqrt(vn + vo)) / eo, 4)
        entries.append(entry)
    return entries


def _round(weights: Mapping[str, float]) -> dict[str, float]:
    return {a: round(w, 4) for a, w in sorted(weights.items()) if w > 1e-9}


def _schedule_entry(store, vocab, wf, old, new, group, cells, shares, window) -> dict | None:
    lo, hi = wf["dialling_window"]

    def weight(cfg: Mapping, hour: int) -> float:
        total = 0.0
        for cell, share in shares.items():
            table = _table(cfg, assign_cohort(cfg["cohorts"], vocab.decode(cell)))
            total += share * (1.0 if table is None else float(table.get(str(hour), 0.0)))
        return total

    ow, nw = [weight(old, h) for h in range(lo, hi)], [weight(new, h) for h in range(lo, hi)]
    if all(abs(a - b) < 1e-9 for a, b in zip(ow, nw)):
        return None
    pickup = {r.key[0]: r for r in cube.rollup(store, wf["workflow_id"], by=("hour",), cells=cells, **window)}

    def expected(ws: list[float]) -> float | None:
        num = den = 0.0
        for h, w in zip(range(lo, hi), ws):
            row = pickup.get(h)
            if row and row.dials >= settings.DRYRUN_MIN_ARM_N:
                num += w * row.connects / row.dials
                den += w
        return num / den if den > 0 else None

    eo, en = expected(ow), expected(nw)
    return {"cohort": group, "kpi": "pickup_rate", "method": "hour re-weighting replay (approximation:"
            " capacity per hour is fixed; weights only change who is called)",
            "evidence": "observed" if eo and en is not None else "no_evidence",
            "expected_old": None if eo is None else round(eo, 4),
            "expected_new": None if en is None else round(en, 4),
            "rel_lift": None if not eo or en is None else round((en - eo) / eo, 4)}


def build_dry_run(store: Store, vocab: FeatureVocab, wf: Mapping, old: Mapping, new: Mapping,
                  now: int, dims: list[str], evidence: Mapping | None = None) -> dict:
    """Never empty: the approver rejects an empty dry_run."""
    day = now // settings.MINUTES_PER_DAY
    window = {"day_min": day - settings.DRYRUN_WINDOW_DAYS + 1, "day_max": day}
    primary = wf["objective"]["primary"]
    out: dict = {"method": "observed per-arm rates x new allocation weights, per cohort "
                           "(marginal over other dims; normal-approx 95% CI; unobserved arms flagged)",
                 "window_days": settings.DRYRUN_WINDOW_DAYS, "as_of_day": day, "primary_kpi": primary,
                 "changed_dims": dims, "allocations": [], "schedule": []}
    groups = cells_by_cohort(store, vocab, wf["workflow_id"], new["cohorts"])
    for group, cells in sorted(groups.items()):
        dials = {r.key[0]: r.dials for r in cube.rollup(store, wf["workflow_id"], by=("cell",),
                                                       cells=cells, **window)}
        total = sum(dials.values())
        if not total:
            out["allocations"].append({"cohort": group, "evidence": "no_evidence",
                                       "note": "no dials in the window"})
            continue
        shares = {c: n / total for c, n in dials.items() if n}
        out["allocations"] += _agent_entry(store, vocab, wf, old, new, group, cells, shares, window,
                                           primary)
        sched = _schedule_entry(store, vocab, wf, old, new, group, cells, shares, window)
        if sched:
            out["schedule"].append(sched)
        out.setdefault("traffic_share", {})[group] = round(total / max(1, _window_dials(store, wf, window)), 4)
    if "prompt_version" in dims:
        out["prompt_version"] = {"old": old["prompt_version"], "new": new["prompt_version"],
                                 "method": "validation-only (no historical replay: one prompt per workflow)",
                                 "validation_run_id": (evidence or {}).get("validation_run_id")}
    if "cohorts" in dims:
        out["cohorts"] = {"note": "partition change; effects are shown in the allocation/schedule entries"}
    return out


def _window_dials(store: Store, wf: Mapping, window: Mapping) -> int:
    return cube.totals(store, wf["workflow_id"], **window).dials


def impact_score(dry_run: Mapping, primary: str, override: float | None = None) -> float:
    """clip(lower-bound relative lift on the primary KPI x traffic share affected / 0.10, 0, 1)."""
    if override is not None:
        return round(min(1.0, max(0.0, override)), 4)
    shares = dry_run.get("traffic_share", {})
    lifts: dict[str, float] = {}
    for e in dry_run.get("allocations", []):
        lifts[e["cohort"]] = lifts.get(e["cohort"], 0.0) + max(0.0, e.get("rel_lift_lb") or 0.0)
    if primary == "pickup_rate":  # schedule moves pickup only
        for e in dry_run.get("schedule", []):
            if e.get("evidence") == "observed" and e.get("rel_lift") is not None:
                # no CI on a replay: haircut stands in for the lower bound
                lifts[e["cohort"]] = (lifts.get(e["cohort"], 0.0)
                                      + settings.SCHEDULE_LIFT_HAIRCUT * max(0.0, e["rel_lift"]))
    score = sum(shares.get(c, 0.0) * lift for c, lift in lifts.items()) / IMPACT_FULL_LIFT
    return round(min(1.0, max(0.0, score)), 4)
