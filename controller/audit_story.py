"""Plain-English facts for an audit answer: what changed, why, what happened next (CONTEXT C12).

Everything is read from OUR store. :func:`attach` adds the keys ``what``, ``why``, ``later`` and ``rollback`` to
a :func:`controller.audit.why_at` result; :mod:`controller.audit_render` turns them into the header.
"""
from __future__ import annotations

from typing import Any

from . import audit, settings
from .configlib import config_diff
from .stats import p_text

DAY = settings.MINUTES_PER_DAY
_EXP = ("experiment_revert", "experiment_promote", "experiment_conclude", "experiment_entry",
        "experiment_exhausted", "experiment_interrupted")
# Proposal kind -> the decision kinds (newest first match) that produced it.
_TRIGGERS: dict[str, tuple[str, ...]] = {
    "did_evacuation": ("did_flagged",), "halluc_fix": ("incident_opened",), "schedule_rollback": ("schedule_rollback",),
    "schedule_refresh": ("schedule_plan",), "schedule_plan": ("schedule_plan",),
    "did_probe": ("did_tested", "did_weak_base", "did_flag_expired"), "did_spread": ("did_tested", "did_weak_base"),
    "did_rebalance": ("did_weak_base",), "prompt_safety": ("validation_run",), "prompt_rollback": ("incident_opened",),
}
_REASON_CHARS = 300


def _kinds(kind: str) -> tuple[str, ...]:
    return _EXP if kind.startswith("experiment_") else _TRIGGERS.get(kind, ())


def trigger_row(store, wid: str, prop: dict | None) -> dict | None:
    """The decision row that produced a proposal (not merely the latest detector rows)."""
    if not prop:
        return None
    kinds = _kinds(prop["kind"])
    if not kinds:
        return None
    exp = prop["kind"].startswith("experiment_")
    since = (prop["created_minute"] or 0) - (2 * DAY if exp else DAY)
    ph = ",".join("?" * len(kinds))
    rows = audit._decisions(store, f"workflow_id=? AND kind IN ({ph}) AND sim_minute<=? AND sim_minute>=?"
                            " ORDER BY id DESC", (wid, *kinds, prop["created_minute"] or 0, since))
    dims = set(prop.get("dims") or [])
    for r in rows:  # experiments carry their dim: prefer the row about the dims this proposal changes
        dim = r["inputs"].get("dim") if isinstance(r["inputs"], dict) else None
        if not exp or dim is None or dim in dims:
            return r
    return None


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%" if abs(x * 100 - round(x * 100)) < 0.05 else f"{x * 100:.1f}%"


def _arms(old: dict, new: dict) -> str:
    arms = sorted(set(old) | set(new))
    moved = [a for a in arms if abs(old.get(a, 0.0) - new.get(a, 0.0)) >= 0.005]
    txt = ", ".join(f"{a} {_pct(old.get(a, 0.0))}->{_pct(new.get(a, 0.0))}" for a in moved[:4])
    return txt + (f" (+{len(moved) - 4} more)" if len(moved) > 4 else "")


def short_change(store, wid: str, version: int) -> str:
    old, new = store.config_body(wid, version - 1), store.config_body(wid, version)
    if old is None or new is None:
        return "(config body not stored)"
    try:
        diff = config_diff(old, new)
    except (KeyError, TypeError):
        return "(diff unavailable)"
    parts: list[str] = []
    if "prompt_version" in diff:
        parts.append(f"prompt {diff['prompt_version']['old']}->{diff['prompt_version']['new']}")
    seen: dict[str, tuple[str, int]] = {}
    for _key, dims in sorted(diff.get("allocations", {}).items()):
        for dim, ch in dims.items():
            text = _arms(ch["old"], ch["new"])
            first, n = seen.get(dim, (text, 0))
            seen[dim] = (first, n + 1)
    for dim, (text, n) in seen.items():
        parts.append(f"{dim}: {text}" + (f" (same in {n} cohort keys)" if n > 1 else ""))
    if "cohorts" in diff:
        c = diff["cohorts"]
        parts.append(f"cohorts +{len(c['added'])}/-{len(c['removed'])}")
    if "schedule" in diff:
        parts.append(f"schedule: {len(diff['schedule']['hour_weights_changed'])} hour tables changed")
    return "; ".join(parts) or "no visible change"


def _numbers(inputs: Any) -> str:
    """Key test numbers of a decision's inputs (schedule rollbacks carry their before/after test)."""
    if not isinstance(inputs, dict):
        return ""
    tests = [v for v in inputs.values() if isinstance(v, dict) and "p_drop" in v]
    if not tests:
        return ""
    t = min(tests, key=lambda v: v["p_drop"])
    return f" [drop {t.get('drop')}, z {t.get('z')}, {p_text(t['p_drop'])}]"


def _trigger_text(trig: dict | None, prop: dict | None) -> str:
    """Trigger summary with its numbers; an incident row is only a headline, so the proposal's own summary
    (which carries the flag rates, n and p) is used for it."""
    if trig is None:
        return (prop or {}).get("summary") or ""
    if trig["kind"] == "incident_opened" and prop and prop.get("summary"):
        return f"{trig['summary']}: {prop['summary']}"
    return trig["summary"] + _numbers(trig["inputs"])


def is_rollback(kind: str, summary: str | None, trigger: dict | None) -> bool:
    text = (summary or "").lower()
    return ("rollback" in kind or "revert" in kind
            or (trigger is not None and trigger["kind"] in ("experiment_revert", "schedule_rollback"))
            or text.startswith("revert") or "harm-stop" in text or "harm stop" in text)


def later_changes(store, wid: str, ver: dict, limit: int = 4) -> list[dict]:
    """Later versions that touched any of the same dims (the initial version: any later version)."""
    mine = set(audit._json(ver["changed_dims"]) or [])
    rows = audit._rows(store, f"SELECT {audit._VER_COLS} FROM config_versions WHERE workflow_id=? AND version>?"
                       " AND ts_minute IS NOT NULL ORDER BY version", (wid, ver["version"]))
    out = []
    for r in rows:
        theirs = set(audit._json(r["changed_dims"]) or [])
        if mine and not (mine & theirs):
            continue
        prop = audit._proposal(store, r["proposal_id"])
        trig = trigger_row(store, wid, prop)
        summary = _trigger_text(trig, prop)
        kind = prop["kind"] if prop else (r["actor"] or "unknown")
        out.append({"version": r["version"], "at": audit.fmt_minute(r["ts_minute"]), "at_minute": r["ts_minute"],
                    "actor": r["actor"], "kind": kind, "dims": sorted(theirs), "reason": summary[:_REASON_CHARS],
                    "rollback": is_rollback(kind, (prop or {}).get("summary"), trig)})
        if len(out) >= limit:
            break
    return out


def attach(store, rep: dict[str, Any]) -> None:
    """Add what/why/later/rollback to a ``why_at`` result (mutates ``rep``, which is ours)."""
    ver, prop = rep["version"], rep["proposal"]
    if ver is None:
        return
    wid = rep["workflow"]
    rep["what"] = short_change(store, wid, ver["version"]) if ver["version"] > 1 else "initial configuration"
    trig = trigger_row(store, wid, prop)
    rep["why"] = None
    if trig:
        same = audit._decisions(store, "workflow_id=? AND kind=? AND sim_minute=? ORDER BY id LIMIT 3",
                                (wid, trig["kind"], trig["sim_minute"]))
        rep["why"] = {"kind": trig["kind"], "at": audit.fmt_minute(trig["sim_minute"]),
                      "summary": (_trigger_text(trig, prop) if trig["kind"] == "incident_opened"
                                  else " | ".join(dict.fromkeys(r["summary"] for r in same)))}
    rep["later"] = later_changes(store, wid, ver)
    rep["policy"] = audit.workflow_policy(store, wid)
    # Only the very next change to the same dims can undo this one, and a rollback is not itself rolled back.
    nxt = rep["later"][0] if rep["later"] else None
    mine_is_rollback = is_rollback(prop["kind"], prop["summary"], trig) if prop else False
    rep["rollback"] = nxt if nxt and nxt["rollback"] and not mine_is_rollback else None
