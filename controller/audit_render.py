"""Human-readable text for the audit reports built by :mod:`controller.audit`.

Every ``at`` / range answer starts with a short plain-English header (what / why / who / when / what next).
Raw decision ``inputs`` JSON and long experiment-look lists appear only with ``verbose`` (``--verbose``);
``--json`` always carries everything.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .audit import fmt_minute

_MAX_INPUT_CHARS = 260
_MAX_ROWS = 8
_WHY_CHARS = 320
_ACTOR = re.compile(r" by (\w+) changed")


def _compact(inputs: Any) -> str:
    if inputs is None:
        return ""
    text = json.dumps(inputs, sort_keys=True, default=str)
    return text if len(text) <= _MAX_INPUT_CHARS else text[:_MAX_INPUT_CHARS] + "...(truncated; see --json)"


def _decision_lines(rows: list[dict], indent: str = "    ", verbose: bool = False) -> list[str]:
    """One line per decision. Default hides raw inputs, collapses experiment looks and caps the list."""
    lines: list[str] = []
    looks = [d for d in rows if d["kind"] == "experiment_look"] if not verbose else []
    shown = [d for d in rows if d not in looks]
    for d in (shown if verbose else shown[:_MAX_ROWS]):
        lines.append(f"{indent}{fmt_minute(d['sim_minute'])} {d['kind']}: {d['summary']}")
        if verbose and d.get("inputs") is not None:
            lines.append(f"{indent}  inputs: {_compact(d['inputs'])}")
    if not verbose and len(shown) > _MAX_ROWS:
        lines.append(f"{indent}(+{len(shown) - _MAX_ROWS} more rows; --verbose or --json)")
    if looks:
        lines.append(f"{indent}({len(looks)} experiment look rows hidden; --verbose or --json)")
    return lines or [f"{indent}(none)"]


def _who(r: dict[str, Any]) -> str:
    v, p = r["version"], r["proposal"]
    wid = r["workflow"]
    if p and p.get("approval"):
        ap = p["approval"]
        return (f"approval {ap['approval_id']} {str(ap['decision']).lower()}, decided_at {ap['decided_at']} by the"
                f" policy approver of {wid}; the sim exposes no approver names")
    if p and p["route"] == "auto":
        aa = ((r.get("policy") or {}).get("auto_apply")) or {}
        return (f"auto-applied under policy {aa.get('allowed_dims', '(dims in the workflow policy)')},"
                f" max_shift {aa.get('max_shift', '?')}; no approval needed")
    if v["actor"] == "supervisor":
        return "supervisor edit (bypasses governance; we never overwrite it silently)"
    return "initial business configuration (no controller change yet)"


def _when(r: dict[str, Any]) -> str:
    v, p = r["version"], r["proposal"]
    txt = f"took effect {fmt_minute(p['applied_minute'] if p and p['applied_minute'] is not None else v['ts_minute'])}"
    txt += f"; we observed the version at {fmt_minute(v['ts_minute'])}"
    if p and p.get("approval") and p["approval"].get("decided_at"):
        ap = p["approval"]
        txt += (f"; approval decided_at {ap['decided_at']} is the sim's clock, observed_at {ap['observed_at']} is the"
                " next 15-min tick that saw it (tick granularity)")
    else:
        txt += " (we poll the sim every 15 sim-minutes, so observed time can lag the real change by up to one tick)"
    return txt


def _next(r: dict[str, Any]) -> str:
    p, rb, later = r["proposal"], r.get("rollback"), r.get("later") or []
    if rb:
        txt = (f"rolled back by v{rb['version']} at {rb['at']} ({rb['kind']}, touching only {', '.join(rb['dims'])}: "
               f"{rb['reason'][:200]})")
    elif later:
        txt = "later changes to these dims: " + "; ".join(f"v{c['version']} {c['at']} ({c['actor']} {c['kind']})"
                                                         for c in later[:3])
    else:
        txt = "no later change to these dims in the recorded run"
    if p:
        verdict = ("rolled back" if rb else "no rollback or harm-stop" if p["state"] in ("CLOSED", "MONITORING")
                   else p["state"])
        txt += f"; monitoring: state {p['state']}, verdict: {verdict}"
    return txt


def header_lines(r: dict[str, Any]) -> list[str]:
    """The plain-English header: at most six lines (title + what, why, who, when, what happened next)."""
    v, p = r["version"], r["proposal"]
    why = r.get("why")
    why_txt = (f"{why['kind']} at {why['at']}: {why['summary']}" if why
               else (f"proposal rationale: {p['rationale']}" if p and p.get("rationale")
                     else "a supervisor changed the config directly; the sim records no reason" if v["actor"] == "supervisor"
                     else "not recorded as a decision"))
    dims = v["changed_dims"] if isinstance(v["changed_dims"], str) else json.dumps(v["changed_dims"])
    return [f"SUMMARY for {r['workflow']} at {r['at']} (plain English; details below)",
            f"  WHAT: v{v['version']} by {v['actor'] or 'initial'}, dims {dims}: {r.get('what', '-')}",
            f"  WHY: {why_txt[:_WHY_CHARS]}",
            f"  WHO: {_who(r)}",
            f"  WHEN: {_when(r)}",
            f"  NEXT: {_next(r)}"]


def _proposal_lines(p: dict, rollback: dict | None, verbose: bool) -> list[str]:
    tv = p.get("tv") or {}
    shift = f"max_shift {max(tv.values()):.3f}" if tv else "max_shift 0"
    lines = [f"  proposal {p['proposal_id']} [{p['route']}] state={p['state']} base=v{p['base_version']}"
             f" applied=v{p['applied_version']} at {fmt_minute(p['applied_minute'])}",
             f"    summary: {p['summary']}", f"    rationale: {p['rationale']}",
             f"    impact={p['impact_score']} affected_leads={p['affected_leads']} {shift}"
             + (f" tv={p['tv']}" if verbose else "")]
    if verbose and p.get("dry_run"):
        lines.append(f"    dry_run: {_compact(p['dry_run'])}")
    ap = p.get("approval")
    if ap:
        lines.append(f"    approval {ap['approval_id']}: {ap['decision']} (decided_at {ap['decided_at']},"
                     f" observed_at {ap['observed_at']}) by {ap['approver']}; the sim exposes no approver names")
        pol = ap.get("policy")
        if pol:
            lines.append(f"    policy: approver hours {pol['approver_hours']}, latency {pol['approver_latency_minutes']} min,"
                         f" ttl {pol['proposal_ttl_hours']} h, max {pol['max_requests_per_24h']} requests/24h;"
                         f" objective {pol['objective']}")
    elif p["route"] == "auto":
        lines.append("    auto-applied under the workflow's auto_apply policy (no approval)")
    lines.append("  proposal lifecycle:")
    for e in p["events"]:
        extra = e["payload"].get("note", "") if isinstance(e["payload"], dict) else e["payload"]
        if "decided_at" in e:
            extra = f"decided_at {fmt_minute(e['decided_at'])}, observed_at {fmt_minute(e['observed_at'])} {extra or ''}"
        if e["event"] == "CLOSED" and rollback:
            extra = (f"monitoring window over (MONITORING -> CLOSED); rolled back by v{rollback['version']} at"
                     f" {rollback['at']}: {rollback['kind']}, {rollback['reason'][:200]}")
        lines.append(f"    {fmt_minute(e['ts_minute'])} {e['event']} {extra or ''}".rstrip())
    if p.get("superseded_by"):
        lines.append(f"    superseded-by proposals: {p['superseded_by']}")
    return lines


def _alloc_text(cfg: dict, verbose: bool) -> str:
    alloc = cfg["allocations"]
    if not verbose:
        alloc = json.loads(json.dumps(alloc), parse_float=lambda x: round(float(x), 3))
    return json.dumps(alloc, sort_keys=True)[:400]


def render_at(r: dict[str, Any], verbose: bool = False) -> str:
    v = r["version"]
    if v is None:
        return "\n".join([f"why {r['workflow']} at {r['at']}", "  no config version recorded"])
    lines = header_lines(r) + ["", f"why {r['workflow']} at {r['at']}"]
    lines.append(f"  active version v{v['version']} by {v['actor'] or 'initial'} (observed"
                 f" {fmt_minute(v['ts_minute'])}, dims {v['changed_dims']})")
    cfg = r["config"]
    if cfg:
        lines.append(f"    prompt {cfg['prompt_version']}, cohorts {cfg['cohorts']}, allocations "
                     f"{_alloc_text(cfg, verbose)}")
    p = r["proposal"]
    if p:
        lines += _proposal_lines(p, r.get("rollback"), verbose)
    elif v["actor"] == "supervisor":
        lines.append("  made by the client's supervisor (bypasses governance; never silently overwritten)")
    elif v["actor"] is None:
        lines.append("  initial business configuration (no controller change yet)")
    lines.append("  our decisions for this change:")
    lines += _decision_lines(r["decisions"], verbose=verbose)
    if r["evidence"]:
        lines.append("  evidence (newest detector/test rows in the 24h before the proposal):")
        lines += _decision_lines(r["evidence"], verbose=verbose)
    lines.append("  post-apply monitoring:")
    lines += _decision_lines(r["monitoring"], verbose=verbose)
    nxt = r["next_change"]
    if nxt:
        nv = nxt["version"]
        lines.append(f"  next change to these dims: v{nv['version']} ({nv['actor']}) at {fmt_minute(nv['ts_minute'])},"
                     f" dims {nv['changed_dims']}, proposal {nv['proposal_id']}: {nxt['proposal_summary']}")
    else:
        lines.append("  no later change to these dims within the recorded run")
    return "\n".join(lines)


def range_header(r: dict[str, Any]) -> list[str]:
    ev = r["events"]
    kinds = Counter(e["type"] for e in ev)
    actors = Counter(m.group(1) for e in ev if e["type"] == "config_observed" and (m := _ACTOR.search(e["text"] or "")))
    rolled = {k: n for k, n in kinds.items() if "rollback" in k or "revert" in k}
    top = ", ".join(f"{k} x{n}" for k, n in kinds.most_common(5)) or "none"
    return [f"SUMMARY for {r['workflow']} from {r['from']} to {r['to']} (plain English; details below)",
            f"  WHAT: {sum(actors.values())} config versions observed ({dict(actors) or 'none'}); "
            f"{kinds.get('proposal_applied', 0) + kinds.get('proposal_auto_applied', 0)} proposals applied",
            f"  WHY: most frequent events: {top}",
            f"  WHO: {kinds.get('proposal_approved', 0)} approved, {kinds.get('proposal_rejected', 0)} rejected,"
            f" {kinds.get('proposal_auto_applied', 0)} auto-applied, {actors.get('supervisor', 0)} supervisor edits",
            "  WHEN: event times are sim time; a version's time is the 15-min tick that observed it",
            f"  NEXT: rollbacks / reverts in range: {rolled or 'none'}"]


def render_range(r: dict[str, Any], verbose: bool = False) -> str:
    lines = range_header(r) + ["", f"timeline {r['workflow']} from {r['from']} to {r['to']} (exclusive)"]
    n0 = len(lines)
    looks = 0
    for e in r["events"]:
        if e["type"] == "experiment_look" and not verbose:
            looks += 1
            continue
        lines.append(f"  {fmt_minute(e['minute'])}  {e['type']}: {e['text']}")
        if verbose and e.get("inputs") is not None:
            lines.append(f"      inputs: {_compact(e['inputs'])}")
    if looks:
        lines.append(f"  ({looks} experiment look rows hidden; --verbose or --json)")
    if len(lines) == n0:
        lines.append("  (no events)")
    return "\n".join(lines)


def render_lead(r: dict[str, Any], verbose: bool = False) -> str:
    lead = r["lead"]
    if lead is None:
        return f"lead {r['lead_id']}: not found in the store"
    lines = [f"lead {lead['lead_id']} person {lead['person_id']} workflow {lead['workflow_id']} campaign"
             f" {lead['campaign_id']} (created {fmt_minute(lead['ts_minute'])}, cell {lead['cell']})"
             + (f", day {r['day']}" if r["day"] is not None else "")]
    for c in r["calls"]:
        lines.append(f"  {fmt_minute(c['ts_minute'])} attempt {c['attempt_no']} welcome {c['welcome']} voice"
                     f" {c['voice']} DID {c['caller_id']} prompt {c['prompt']} cohort {c['cohort_id']}"
                     f" connected={bool(c['connected'])} engaged={bool(c['engaged'])}")
        lines.append(f"    allocation from config v{c['config_version']}; prompt from config"
                     f" v{c['prompt_active_version']} active at the call time")
    if not r["calls"]:
        lines.append("  (no calls)")
    for ver, basis in sorted(r["versions"].items()):
        lines.append(f"  decisions behind config v{ver}:")
        if basis is None:
            lines.append("    (version not in our store)")
            continue
        v = basis["version"]
        lines.append(f"    by {v['actor'] or 'initial'} at {fmt_minute(v['ts_minute'])} dims {v['changed_dims']}")
        if basis["proposal"]:
            p = basis["proposal"]
            lines.append(f"    proposal {p['proposal_id']}: {p['summary']}")
            lines.append(f"    rationale: {p['rationale']}")
            ap = p.get("approval")
            if ap:
                lines.append(f"    approval {ap['approval_id']} {ap['decision']} decided_at {ap['decided_at']}"
                             f" observed_at {ap['observed_at']} by {ap['approver']}")
        lines += _decision_lines(basis["decisions"], "    ", verbose)
    return "\n".join(lines)
