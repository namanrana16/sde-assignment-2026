"""Audit queries (CONTEXT C12), answered from OUR store only (R7).

Three modes, all read-only over the controller's SQLite file:

* :func:`why_at`     - what config was active for a workflow at time T, who/why/approval, after-effects.
* :func:`why_range`  - a timeline of detections, proposals, decisions, applies and incidents.
* :func:`why_lead`   - one customer's calls and the decisions behind their allocation and prompt
  versions (split join for ``next_campaign_only``: allocations come from the call's
  ``config_version``, the prompt from the version active at the call's timestamp).

Rendering to text lives in :mod:`controller.audit_render`.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import settings
from .store import Store

_AT = re.compile(r"^\s*(?:Day|D)\s*(-?\d+)\s+(\d{1,2}):(\d{2})\s*$", re.IGNORECASE)
_DAY = re.compile(r"^\s*(?:Day|D)\s*(-?\d+)\s*$", re.IGNORECASE)
_VER_COLS = "version, actor, ts_minute, changed_dims, proposal_id, approval_id"
# Decision kinds that are per-tick noise; they appear only as "latest evidence", never in timelines.
_NOISE = ("advance_intent", "did_eval", "halluc_eval", "did_curve_fit", "baselines", "startup", "finished",
          "stopped", "plan_wait", "advance_recovered")
_EVIDENCE = ("did_eval", "halluc_eval", "did_flagged", "incident_opened", "validation_run", "did_tested",
             "did_weak_base", "did_curve_fit")
_LIFECYCLE_SKIP = ("proposal_draft", "proposal_intent", "proposal_monitoring", "proposal_closed",
                   "proposal_apply_intent")


def parse_at(text: str) -> int:
    """``"Day 3 14:00"`` (or ``"D3 14:00"``) -> sim minute (IST, Day 0 = minute 0)."""
    m = _AT.match(text)
    if not m:
        raise ValueError(f"cannot parse {text!r}; expected like 'Day 3 14:00'")
    day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"bad time in {text!r}")
    return day * settings.MINUTES_PER_DAY + hour * 60 + minute


def parse_bound(text: str, end: bool = False) -> int:
    """``"Day 2"`` -> start of Day 2 (or, with ``end``, the start of Day 3: the range is inclusive of the day)."""
    m = _DAY.match(text)
    if m:
        return (int(m.group(1)) + (1 if end else 0)) * settings.MINUTES_PER_DAY
    return parse_at(text)


def fmt_minute(minute: int | None) -> str:
    if minute is None:
        return "-"
    day, rem = divmod(minute, settings.MINUTES_PER_DAY)
    return f"Day {day} {rem // 60:02d}:{rem % 60:02d}"


def _rows(store: Store, sql: str, args: tuple = ()) -> list[dict]:
    cur = store.conn.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _decisions(store: Store, sql_where: str, args: tuple) -> list[dict]:
    rows = _rows(store, "SELECT id, sim_minute, workflow_id, kind, summary, inputs FROM decisions WHERE "
                 + sql_where, args)
    for r in rows:
        r["inputs"] = _json(r["inputs"])
    return rows


def _config_view(store: Store, wid: str, version: int) -> dict | None:
    body = store.config_body(wid, version)
    if body is None:
        return None
    return {"prompt_version": body.get("prompt_version"), "allocations": body.get("allocations"),
            "cohorts": len(body.get("cohorts") or []),
            "schedule_hours": sorted((body.get("schedule") or {}).get("hour_weights", {}) or {})}


def _version_at(store: Store, wid: str, minute: int) -> dict | None:
    rows = _rows(store, f"SELECT {_VER_COLS} FROM config_versions WHERE workflow_id=? AND ts_minute IS NOT NULL"
                 " AND ts_minute<=? ORDER BY version DESC LIMIT 1", (wid, minute))
    if not rows:  # no change observed yet: the initial version
        rows = _rows(store, f"SELECT {_VER_COLS} FROM config_versions WHERE workflow_id=? ORDER BY version LIMIT 1",
                     (wid,))
    return rows[0] if rows else None


def _next_version(store: Store, wid: str, version: int) -> dict | None:
    rows = _rows(store, f"SELECT {_VER_COLS} FROM config_versions WHERE workflow_id=? AND version>?"
                 " AND ts_minute IS NOT NULL ORDER BY version LIMIT 1", (wid, version))
    return rows[0] if rows else None


def _proposal(store: Store, proposal_id: str | None) -> dict | None:
    if not proposal_id:
        return None
    rows = _rows(store, "SELECT proposal_id, kind, state, route, approval_id, base_version, created_minute,"
                 " filed_minute, apply_by_minute, expires_minute, applied_version, applied_minute, supersedes,"
                 " body FROM proposals WHERE proposal_id=?", (proposal_id,))
    if not rows:
        return None
    p = rows[0]
    body = json.loads(p.pop("body") or "{}")
    req = body.get("request", {})
    p.update(summary=body.get("summary"), dims=body.get("dims"), tv=body.get("tv"),
             impact_score=body.get("impact_score"), affected_leads=body.get("affected_leads"),
             rationale=req.get("rationale"), dry_run=req.get("dry_run"),
             superseded_by=[r["proposal_id"] for r in _rows(
                 store, "SELECT proposal_id FROM proposals WHERE supersedes=?", (proposal_id,))])
    p["events"] = _rows(store, "SELECT ts_minute, event, approval_id, payload, event_id FROM proposal_events"
                        " WHERE proposal_id=? ORDER BY id", (proposal_id,))
    for e in p["events"]:
        e["payload"] = _json(e["payload"])
    p["events"] = merge_decision_events(p["events"])
    p["approval"] = _approval(p, store)
    return p


_DECISIONS = ("APPROVED", "REJECTED", "EXPIRED")


def merge_decision_events(events: list[dict]) -> list[dict]:
    """The sim's decision event (sim time) and our own state change (the tick that observed it) describe ONE
    approval decision. Show it once: ``decided_at`` = sim decision time, ``observed_at`` = our observation."""
    sim = {e["event"].upper(): e for e in events if e.get("event_id") is not None and e["event"].upper() in _DECISIONS}
    out, used = [], set()
    for e in events:
        if e.get("event_id") is not None and e["event"].upper() in _DECISIONS and e["event"].upper() in sim:
            twin = any(o["event"] == e["event"].upper() and o.get("event_id") is None for o in events)
            if twin:
                continue  # folded into our own row below
        if e["event"] in _DECISIONS and e.get("event_id") is None and e["event"] in sim and e["event"] not in used:
            used.add(e["event"])
            decided = sim[e["event"]]
            out.append({**e, "ts_minute": decided["ts_minute"], "decided_at": decided["ts_minute"],
                        "observed_at": e["ts_minute"]})
            continue
        out.append(e)
    return out


def workflow_policy(store: Store, wid: str) -> dict | None:
    """Governance policy and objective stored at startup (``meta``), so audit answers need no HTTP."""
    raw = store.get_meta(f"workflow_policy:{wid}")
    if not raw:
        return None
    d = json.loads(raw)
    gov = d.get("governance") or {}
    return {"approver_hours": gov.get("approver_hours"), "approver_latency_minutes": gov.get("approver_latency_minutes"),
            "auto_apply": gov.get("auto_apply"), "proposal_ttl_hours": gov.get("proposal_ttl_hours"), "max_requests_per_24h": gov.get("max_requests_per_24h"),
            "objective": d.get("objective")}


def _approval(p: dict, store: Store | None = None) -> dict | None:
    """Approval facts. The sim exposes no approver names (A4): the approver is the workflow's policy approver."""
    if not p.get("approval_id"):
        return None
    decided = next((e for e in p["events"] if e["event"] in _DECISIONS), None)
    decided_min = (decided.get("decided_at", decided["ts_minute"]) if decided else None)
    observed = decided.get("observed_at", decided["ts_minute"]) if decided else None
    return {"approval_id": p["approval_id"], "decided_at": fmt_minute(decided_min) if decided else None,
            "observed_at": fmt_minute(observed) if decided else None,
            "decision": decided["event"] if decided else None,
            "decided_minute": decided_min, "observed_minute": observed,
            "approver": f"policy approver of {p['proposal_id'].split(':')[0]}",
            "policy": workflow_policy(store, p["proposal_id"].split(":")[0]) if store is not None else None}


def _evidence(store: Store, wid: str, upto: int, since: int) -> list[dict]:
    """The inputs behind a proposal: the newest row of each evidence kind in (since, upto]."""
    out: list[dict] = []
    for kind in _EVIDENCE:
        rows = _decisions(store, "workflow_id=? AND kind=? AND sim_minute<=? AND sim_minute>? ORDER BY id DESC"
                          " LIMIT 1", (wid, kind, upto, since))
        out += rows
    return sorted(out, key=lambda r: r["id"])


def _after(store: Store, wid: str, applied: int | None, until: int | None) -> list[dict]:
    if applied is None:
        return []
    end = until if until is not None else applied + settings.MONITOR_MINUTES
    ph = ",".join("?" * len(_NOISE))
    rows = _decisions(store, f"workflow_id=? AND sim_minute>=? AND sim_minute<=? AND kind NOT IN ({ph})"
                      " AND kind NOT LIKE 'proposal_%' ORDER BY id", (wid, applied, end, *_NOISE))
    for kind in ("did_eval", "halluc_eval"):
        rows += _decisions(store, "workflow_id=? AND kind=? AND sim_minute>? AND sim_minute<=? ORDER BY id DESC"
                           " LIMIT 1", (wid, kind, applied, end))
    return sorted(rows, key=lambda r: r["id"])


def version_basis(store: Store, wid: str, version: int | None) -> dict | None:
    """Who/why for one config version (shared by every mode)."""
    if version is None:
        return None
    rows = _rows(store, f"SELECT {_VER_COLS} FROM config_versions WHERE workflow_id=? AND version=?",
                 (wid, version))
    if not rows:
        return None
    ver = rows[0]
    prop = _proposal(store, ver["proposal_id"])
    out: dict[str, Any] = {"version": ver, "proposal": prop, "config": _config_view(store, wid, version)}
    if prop:
        out["decisions"] = _decisions(
            store, "workflow_id=? AND json_extract(inputs, '$.proposal_id')=? AND kind NOT IN"
            " ('proposal_draft','proposal_intent','proposal_monitoring','proposal_closed') ORDER BY id",
            (wid, prop["proposal_id"]))
    else:
        out["decisions"] = _decisions(store, "workflow_id=? AND kind='config_observed' AND summary LIKE ?"
                                      " ORDER BY id", (wid, f"v{version} %"))
    return out


def why_at(store: Store, workflow: str, at_minute: int) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "at", "workflow": workflow, "at": fmt_minute(at_minute), "at_minute": at_minute,
                           "version": None, "config": None, "proposal": None, "evidence": [],
                           "decisions": [], "monitoring": [], "next_change": None}
    ver = _version_at(store, workflow, at_minute)
    if ver is None:
        return out
    basis = version_basis(store, workflow, ver["version"]) or {}
    prop = basis.get("proposal")
    nxt = _next_version(store, workflow, ver["version"])
    out.update(version=ver, config=basis.get("config"), proposal=prop, decisions=basis.get("decisions", []),
               next_change=_describe_next(store, _next_same_dims(store, workflow, ver) or nxt))
    if prop:
        since = (prop["created_minute"] or 0) - settings.MINUTES_PER_DAY
        out["evidence"] = _evidence(store, workflow, prop["created_minute"] or at_minute, since)
        out["monitoring"] = _after(store, workflow, prop["applied_minute"],
                                   nxt["ts_minute"] if nxt else None)
    from . import audit_story  # late: audit_story builds on this module's helpers
    audit_story.attach(store, out)
    return out


def _next_same_dims(store: Store, wid: str, ver: dict) -> dict | None:
    """The next version that touched any of the same dims (None for the initial version or when none did)."""
    mine = set(_json(ver["changed_dims"]) or [])
    if not mine:
        return None
    rows = _rows(store, f"SELECT {_VER_COLS} FROM config_versions WHERE workflow_id=? AND version>?"
                 " AND ts_minute IS NOT NULL ORDER BY version", (wid, ver["version"]))
    return next((r for r in rows if mine & set(_json(r["changed_dims"]) or [])), None)


def _describe_next(store: Store, nxt: dict | None) -> dict | None:
    if nxt is None:
        return None
    prop = _proposal(store, nxt["proposal_id"])
    return {"version": nxt, "proposal_summary": prop["summary"] if prop else None,
            "proposal_state": prop["state"] if prop else None}


def _in_range_filter() -> str:
    ph = ",".join("?" * len(_NOISE + _LIFECYCLE_SKIP))
    return f"kind NOT IN ({ph})"


def why_range(store: Store, workflow: str, start: int, end: int) -> dict[str, Any]:
    skip = _NOISE + _LIFECYCLE_SKIP
    decisions = _decisions(store, f"workflow_id=? AND sim_minute>=? AND sim_minute<? AND {_in_range_filter()}"
                           " ORDER BY id", (workflow, start, end, *skip))
    alerts = _rows(store, "SELECT sim_minute, severity, code, message FROM alerts WHERE workflow_id=? AND"
                   " sim_minute>=? AND sim_minute<? ORDER BY id", (workflow, start, end))
    events = [{"minute": d["sim_minute"], "type": d["kind"], "text": d["summary"], "inputs": d["inputs"]}
              for d in decisions]
    events += [{"minute": a["sim_minute"], "type": f"alert:{a['code']}", "text": a["message"], "inputs": None}
               for a in alerts]
    events.sort(key=lambda e: (e["minute"] if e["minute"] is not None else -1))
    return {"mode": "range", "workflow": workflow, "from": fmt_minute(start), "to": fmt_minute(end),
            "events": events}


def _prompt_of(store: Store, wid: str, minute: int) -> dict | None:
    ver = _version_at(store, wid, minute)
    return version_basis(store, wid, ver["version"]) if ver else None


def why_lead(store: Store, lead_id: str, day: int | None = None) -> dict[str, Any]:
    leads = _rows(store, "SELECT lid, lead_id, person_id, workflow_id, cell, campaign_id, ts_minute FROM leads"
                  " WHERE lead_id=?", (lead_id,))
    out: dict[str, Any] = {"mode": "lead", "lead_id": lead_id, "day": day, "lead": None, "calls": [],
                           "versions": {}}
    if not leads:
        return out
    lead = leads[0]
    out["lead"] = lead
    wid = lead["workflow_id"]
    sql = ("SELECT event_id, ts_minute, day, hour, attempt_no, config_version, cohort_id, welcome, voice,"
           " caller_id, prompt, connected, engaged FROM calls WHERE lid=?")
    args: tuple = (lead["lid"],)
    if day is not None:
        sql, args = sql + " AND day=?", args + (day,)
    out["calls"] = _rows(store, sql + " ORDER BY ts_minute, event_id", args)
    for call in out["calls"]:
        active = _version_at(store, wid, call["ts_minute"])
        call["prompt_active_version"] = active["version"] if active else None
        for key in (call["config_version"], call["prompt_active_version"]):
            if key is not None and key not in out["versions"]:
                out["versions"][key] = version_basis(store, wid, key)
    return out
