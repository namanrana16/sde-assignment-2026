"""Workflow-config arithmetic: full-config builder and replicas of the simulator's rules.

The controller must not import ``sim``. The functions here re-implement, from the public
contract (docs/SIMULATOR.md section 3-4), what the approver and auto-apply policy compute,
so a proposal can be validated locally before it is sent:

* ``sim_normalise``: hard validity + the exact normalised form the sim will store.
* ``changed_dims`` / ``tv_by_cohort`` / ``max_shift``: same logic as the sim.
* ``apply_patch``: build a FULL proposed config from the current one plus a patch.

Patch keys (all optional):
  ``cohorts``            full replacement list (otherwise copied verbatim, order included)
  ``allocations``        ``{cohort|"default": {dim: {option: weight} | None}}`` (None drops the override)
  ``prompt_version``     option id
  ``hour_weights``       ``{cohort|"default": {hour: weight} | None}`` (None drops the table)
  ``lead_preferred_hour`` replacement dict or None
  ``allow_zero_hours``   True if zero hour weights are intended
"""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping

from .cohorts import FEATURES, FeatureVocab

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = ("cohorts", *AGENT_DIMS, "prompt_version", "schedule")
MAX_COHORTS = 20
_EPS = 1e-9
DIST_TOLERANCE = 1e-9  # allocations are compared on normalised values with this tolerance


class ProposalError(ValueError):
    """A config or proposal that must never be sent."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def dist(cfg: Mapping, cohort: str, dim: str) -> dict:
    """Effective distribution of a cohort for a dim (falls back to ``default``)."""
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def dist_equal(a: Mapping, b: Mapping, tol: float = DIST_TOLERANCE) -> bool:
    """Same normalised distribution up to ``tol`` per option (ulp noise is not a change)."""
    return all(abs(a.get(o, 0.0) - b.get(o, 0.0)) <= tol for o in sorted(set(a) | set(b)))


def changed_dims(old: Mapping, new: Mapping) -> list[str]:
    ch = []
    if old["cohorts"] != new["cohorts"]:
        ch.append("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for dim in AGENT_DIMS:
        if any(not dist_equal(dist(old, k, dim), dist(new, k, dim)) for k in keys):
            ch.append(dim)
    if old["prompt_version"] != new["prompt_version"]:
        ch.append("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.append("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def tv_by_cohort(old: Mapping, new: Mapping) -> dict[tuple[str, str], float]:
    """Total-variation distance per (cohort, dim), over the union of both cohort lists."""
    keys = {"default"} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}
    out: dict[tuple[str, str], float] = {}
    for key in sorted(keys):
        for dim in AGENT_DIMS:
            a, b = dist(old, key, dim), dist(new, key, dim)
            out[(key, dim)] = 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in sorted(set(a) | set(b)))
    return out


def max_shift(old: Mapping, new: Mapping) -> float:
    return max(tv_by_cohort(old, new).values(), default=0.0)


def normalise_weights(weights: Mapping[str, float], where: str) -> dict[str, float]:
    """Validate, drop zeros and normalise to sum 1."""
    if not isinstance(weights, Mapping) or not weights:
        raise ProposalError([f"{where}: must be a non-empty {{option: weight}} map"])
    if any(isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0 or math.isnan(w)
           for w in weights.values()):
        raise ProposalError([f"{where}: weights must be numbers >= 0"])
    total = float(sum(weights.values()))
    if total <= 0:
        raise ProposalError([f"{where}: weights sum to 0"])
    # canonical key order: the sim samples options in body order, so order must not depend on set iteration (R5)
    return {o: float(w) / total for o, w in sorted(weights.items()) if w > 0}


def sim_normalise(wf: Mapping, cfg: Mapping) -> dict:
    """Replica of the sim's config validation; returns the clean copy the sim would store."""
    p: list[str] = []
    extra = set(cfg) - {"cohorts", "allocations", "prompt_version", "schedule"}
    if extra:
        p.append(f"unknown keys: {sorted(extra)}")
    out: dict[str, Any] = {"cohorts": [], "allocations": {}, "prompt_version": None,
                           "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    ids: list[str] = []
    for i, c in enumerate(cfg.get("cohorts") or []):
        cid = c.get("id")
        if not isinstance(cid, str) or not cid or cid == "default" or cid in ids:
            p.append(f"cohorts[{i}].id must be a unique non-empty string other than 'default'")
            continue
        where = c.get("where") or {}
        for feat, vals in where.items():
            if feat not in FEATURES:
                p.append(f"cohort {cid}: unknown feature {feat}")
            elif not isinstance(vals, list) or not vals:
                p.append(f"cohort {cid}: bad values for {feat}: {vals}")
        ids.append(cid)
        out["cohorts"].append({"id": cid, "where": {k: list(v) for k, v in where.items()}})
    if len(ids) > MAX_COHORTS:
        p.append(f"at most {MAX_COHORTS} cohorts")
    _normalise_allocations(wf, cfg, ids, out, p)
    if cfg.get("prompt_version") not in wf["options"]["prompt_version"]:
        p.append(f"prompt_version {cfg.get('prompt_version')!r} not allowed")
    out["prompt_version"] = cfg.get("prompt_version")
    _normalise_schedule(wf, cfg, ids, out, p)
    if p:
        raise ProposalError(p)
    return out


def _normalise_allocations(wf, cfg, ids, out, p) -> None:
    allocs = cfg.get("allocations") or {}
    if "default" not in allocs:
        p.append("allocations.default is required")
    for key, dims in allocs.items():
        if key != "default" and key not in ids:
            p.append(f"allocations.{key}: not a defined cohort")
            continue
        out["allocations"][key] = {}
        for dim, weights in (dims or {}).items():
            if dim not in AGENT_DIMS:
                p.append(f"allocations.{key}.{dim}: unknown dimension")
                continue
            bad = [o for o in (weights or {}) if o not in wf["options"][dim]]
            if bad:
                p.append(f"allocations.{key}.{dim}: options not allowed for this workflow: {bad}")
                continue
            try:
                out["allocations"][key][dim] = normalise_weights(weights, f"allocations.{key}.{dim}")
            except ProposalError as exc:
                p.extend(exc.problems)
        if key == "default":
            missing = [d for d in AGENT_DIMS if d not in out["allocations"]["default"]]
            if missing:
                p.append(f"allocations.default missing dims {missing}")


def _normalise_schedule(wf, cfg, ids, out, p) -> None:
    sched = cfg.get("schedule") or {}
    lo, hi = wf["dialling_window"]
    for key, table in (sched.get("hour_weights") or {}).items():
        if key != "default" and key not in ids:
            p.append(f"schedule.hour_weights.{key}: not a defined cohort")
            continue
        clean = {}
        for h, w in table.items():
            try:
                hour = int(h)
            except (TypeError, ValueError):
                p.append(f"schedule.hour_weights.{key}: bad hour {h!r}")
                continue
            if not lo <= hour < hi:
                p.append(f"schedule.hour_weights.{key}: hour {hour} outside window [{lo},{hi})")
            elif not isinstance(w, (int, float)) or w < 0:
                p.append(f"schedule.hour_weights.{key}.{h}: weight must be >= 0")
            else:
                clean[str(hour)] = float(w)
        out["schedule"]["hour_weights"][key] = clean
    lph = sched.get("lead_preferred_hour") or {}
    if len(lph) > 200_000:
        p.append("schedule.lead_preferred_hour: at most 200k entries")
    for pid, h in lph.items():
        if not isinstance(h, int) or not lo <= h < hi:
            p.append(f"schedule.lead_preferred_hour[{pid}]: hour {h!r} outside dialling window")
            break
    out["schedule"]["lead_preferred_hour"] = {str(k): int(v) for k, v in lph.items()
                                              if isinstance(v, int)}


def complete_hours(table: Mapping, wf: Mapping, where: str, allow_zero: bool) -> dict[str, float]:
    """Every in-window hour present (an omitted hour would get weight 0 and starve it).

    Missing hours take the mean of the positive weights given (neutral); explicit zeros are
    rejected unless ``allow_zero`` says they are intended.
    """
    lo, hi = wf["dialling_window"]
    clean: dict[str, float] = {}
    for h, w in table.items():
        try:
            hour = int(h)
        except (TypeError, ValueError):
            raise ProposalError([f"{where}: hour key {h!r} is not an integer"]) from None
        if not lo <= hour < hi:
            raise ProposalError([f"{where}: hour {hour} outside window [{lo},{hi})"])
        if (isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0):
            raise ProposalError([f"{where}.{h}: weight must be a number >= 0"])
        if w == 0 and not allow_zero:
            raise ProposalError([f"{where}.{h}: zero weight without allow_zero_hours"])
        clean[str(hour)] = float(w)
    positive = [w for w in clean.values() if w > 0]
    fill = sum(positive) / len(positive) if positive else 1.0
    return {str(h): clean.get(str(h), fill) for h in range(lo, hi)}


def check_cohort_vocab(cohorts: list, vocab: FeatureVocab | None) -> None:
    """Reject cohort ``where`` values never observed in lead events (the sim would accept them
    and the cohort would silently match nobody). Features with an empty vocabulary are skipped."""
    if vocab is None:
        return
    problems = []
    for c in cohorts:
        for feat, vals in (c.get("where") or {}).items():
            known = set(vocab.values(feat))
            if known and isinstance(vals, list):
                bad = [v for v in vals if str(v) not in known]
                if bad:
                    problems.append(f"cohort {c.get('id')}: unknown {feat} values {bad}")
    if problems:
        raise ProposalError(problems)


def apply_patch(wf: Mapping, current: Mapping, patch: Mapping,
                vocab: FeatureVocab | None = None) -> dict:
    """FULL proposed config = ``current`` (cohorts verbatim unless patched) + ``patch``.

    Distributions the patch does not touch are copied from ``current`` bit for bit: they are
    already the sim's normalised form and re-normalising would add ulp-sized phantom changes.
    """
    cfg = copy.deepcopy(dict(current))
    cfg.setdefault("cohorts", [])
    cfg.setdefault("allocations", {})
    cfg.setdefault("schedule", {"hour_weights": {}, "lead_preferred_hour": {}})
    cfg["schedule"].setdefault("hour_weights", {})
    cfg["schedule"].setdefault("lead_preferred_hour", {})
    if "cohorts" in patch:
        check_cohort_vocab(list(patch["cohorts"]), vocab)
        cfg["cohorts"] = copy.deepcopy(list(patch["cohorts"]))
        keep = {c["id"] for c in cfg["cohorts"]} | {"default"}
        cfg["allocations"] = {k: v for k, v in cfg["allocations"].items() if k in keep}
        cfg["schedule"]["hour_weights"] = {k: v for k, v in cfg["schedule"]["hour_weights"].items()
                                           if k in keep}
    for key, dims in (patch.get("allocations") or {}).items():
        for dim, weights in dims.items():
            if weights is None:
                if key == "default":
                    raise ProposalError(["allocations.default cannot drop a dim"])
                cfg["allocations"].get(key, {}).pop(dim, None)
            else:
                cfg["allocations"].setdefault(key, {})[dim] = normalise_weights(
                    weights, f"allocations.{key}.{dim}")
    if "prompt_version" in patch:
        cfg["prompt_version"] = patch["prompt_version"]
    allow_zero = bool(patch.get("allow_zero_hours"))
    for key, table in (patch.get("hour_weights") or {}).items():
        if table is None:
            cfg["schedule"]["hour_weights"].pop(key, None)
        else:
            cfg["schedule"]["hour_weights"][key] = complete_hours(
                table, wf, f"schedule.hour_weights.{key}", allow_zero)
    if "lead_preferred_hour" in patch:
        cfg["schedule"]["lead_preferred_hour"] = dict(patch["lead_preferred_hour"] or {})
    out = sim_normalise(wf, cfg)
    return _restore_unpatched(out, current, patch)


def _restore_unpatched(out: dict, current: Mapping, patch: Mapping) -> dict:
    patched = {(k, d) for k, dims in (patch.get("allocations") or {}).items() for d in dims}
    for key, dims in out["allocations"].items():
        for dim in list(dims):
            old = (current.get("allocations", {}).get(key) or {}).get(dim)
            if old is not None and (key, dim) not in patched:
                dims[dim] = dict(old)
    return out


def config_diff(old: Mapping, new: Mapping) -> dict:
    """Human-readable, non-empty-when-changed diff for the approver."""
    diff: dict[str, Any] = {"changed_dims": changed_dims(old, new)}
    old_ids = {c["id"]: c["where"] for c in old["cohorts"]}
    new_ids = {c["id"]: c["where"] for c in new["cohorts"]}
    if old["cohorts"] != new["cohorts"]:
        diff["cohorts"] = {"added": {k: v for k, v in new_ids.items() if k not in old_ids},
                           "removed": sorted(k for k in old_ids if k not in new_ids),
                           "redefined": sorted(k for k in new_ids
                                               if k in old_ids and old_ids[k] != new_ids[k]),
                           "order_changed": list(old_ids) != list(new_ids)}
    alloc: dict[str, Any] = {}
    for key in sorted({"default"} | set(old_ids) | set(new_ids)):
        for dim in AGENT_DIMS:
            a, b = dist(old, key, dim), dist(new, key, dim)
            if not dist_equal(a, b):
                alloc.setdefault(key, {})[dim] = {"old": a, "new": b}
    if alloc:
        diff["allocations"] = alloc
    if old["prompt_version"] != new["prompt_version"]:
        diff["prompt_version"] = {"old": old["prompt_version"], "new": new["prompt_version"]}
    if old["schedule"] != new["schedule"]:
        oh, nh = old["schedule"]["hour_weights"], new["schedule"]["hour_weights"]
        diff["schedule"] = {
            "hour_weights_changed": sorted(k for k in set(oh) | set(nh) if oh.get(k) != nh.get(k)),
            "lead_preferred_hour_changed": (old["schedule"]["lead_preferred_hour"]
                                            != new["schedule"]["lead_preferred_hour"])}
    return diff
