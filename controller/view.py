"""One-screen text supervisor view (C11): ``python -m controller view --workflow W [--at "Day N HH:MM"] [--weekly]``."""
from __future__ import annotations

from . import view_data as vd
from .audit import fmt_minute


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def signed(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _experiment_lines(e: dict) -> list[str]:
    look = f"look {e['looks']}/{e['max_looks']}" + (" (all looks used, no winner)" if e["exhausted"] else "")
    bounds = e["last_look"].get("boundaries") or {}
    lines = [f"  {e['dim']}: {e['phase']}, control {e['control']}, {look}"
             + (f", started {fmt_minute(e['started'])}" if e["started"] is not None else "")]
    shares = e["live_shares"]
    for arm in sorted(shares, key=lambda a: (-shares[a], a)):
        st = e["arm_states"].get(arm) or ("control" if arm == e["control"] else "-")
        a = e["arms"].get(arm)
        text = f"    {arm:<10} share {shares[arm] * 100:5.1f}%  {st:<12}"
        if a and a["engagement"] is not None:
            lo, hi = a["ci"]
            text += f" n={a['connects']:<6} engagement {pct(a['engagement'])} [{pct(lo)}, {pct(hi)}]"
            if arm != e["control"]:
                li = a["lift_ci"]
                text += f"  lift vs control {signed(a['lift'])}" + (f" [{signed(li[0])}, {signed(li[1])}]" if li else "")
                if arm in bounds:
                    text += f"  boundary z>{bounds[arm]:.2f}"
        else:
            text += " no connects yet"
        lines.append(text)
    return lines


def person_lines(p: dict) -> list[str]:
    return [f"Per-person call time (analysis only) - as of {fmt_minute(p['minute'])}, never pushed to config",
            f"  eligible persons {p['eligible']}, would qualify {p['qualifying']} "
            f"({p['qualifying_data_driven']} from person data, {p['qualifying_on_prior_alone']} on the cohort prior alone)",
            f"  data weight {pct(p['mean_data_weight'])}: the posterior is mostly the cohort prior (prior = {p['prior_strength']:g} dials per block)",
            f"  expected pickup lift if deployed, ignoring capacity: {signed(p['expected_lift_ignoring_capacity'])} per qualifying person "
            f"(cohort prior {signed(p['lift_from_cohort_prior'])}, person data {signed(p['lift_from_person_data'])}); "
            f"workflow upper bound {signed(p['workflow_lift_upper_bound'])}",
            f"  null check: with no person effect the same rule qualifies {p['null_qualifying']} persons "
            f"(lift {signed(p['null_expected_lift'])}) - compare with {p['qualifying']}",
            f"  {p['note']}"]


def render(d: dict) -> str:
    out = [f"=== {d['workflow']}  as of {d['now_text']} ===", "", "WHAT'S BEING TESTED"]
    exps = [e for e in d["experiments"] if e["phase"] in ("pending", "running")]
    for e in exps:
        out += _experiment_lines(e)
    if not exps:
        out.append("  no experiment running")
    if d["did_probes"]:
        out.append("  caller-ID (DID) probes: " + ", ".join(
            f"{p['did']} {p['share'] * 100:.1f}% [{p['status']}]" for p in d["did_probes"]))
    out += ["", "WHAT'S WINNING"]
    for w in d["winners"]:
        out.append(f"  {w['dim']}: {w['winner']} promoted (replaced {w['old_control']}, {w['scope']}, since "
                   f"{fmt_minute(w['since'])}); 10% holdout stays on {w['old_control']}")
        if w["checked"]:
            out.append(f"    decay monitor: CUSUM S={w['cusum_S']} over {w['cusum_blocks']} blocks"
                       f"{' ALARM' if w['cusum_alarm'] else ''}; holdout recent lift {signed(w['holdout_recent_lift'])}"
                       f" (n={w['holdout_connects']})" + ("; ROLLBACK PROPOSED" if w["rollback"] else ""))
        else:
            out.append("    decay monitor: no check yet")
    if not d["winners"]:
        out.append("  nothing promoted yet")
    out += ["", "WHAT WE NEED FROM YOU"]
    for a in d["approvals"]:
        when = f"apply by {fmt_minute(a['apply_by_minute'])}" if a["state"] == "APPROVED" else f"expires {fmt_minute(a['expires_minute'])}"
        out.append(f"  [{a['state']}] {a['summary'] or a['kind']}")
        out.append(f"      impact {a['impact_score']}, affected leads {a['affected_leads']}, {when}  ({a['approval_id'] or 'not filed'})")
    if not d["approvals"]:
        out.append("  nothing waiting on you")
    out += ["", "WHAT CHANGED TODAY"]
    for c in d["changes"]:
        out.append(f"  v{c['version']} {c['at']} by {c['actor']}: {', '.join(c['changed_dims']) or '-'} - {c['reason']}")
        out.append(f"      why: {c['why']}")
    if not d["changes"]:
        out.append("  no config changes today")
    out += ["", "OPEN INCIDENTS & ALERTS"]
    for i in d["incidents"]:
        out.append(f"  incident #{i['id']} {i['kind']} since {i['opened']} {i['detail']}")
    al = d["alerts"]
    for a in al["page"]:
        out.append(f"  PAGE   {fmt_minute(a['sim_minute'])} {a['code']}: {a['message']}")
    for a in al["digest"]:
        out.append(f"  digest {fmt_minute(a['sim_minute'])} {a['code']}: {a['message']}")
    if al["other"]:
        out.append("  other (counts, last 24h): " + ", ".join(f"{o['code']} x{o['count']}" for o in al["other"]))
    if not (d["incidents"] or al["page"] or al["digest"] or al["other"]):
        out.append("  none")
    k = d["kpis"]
    out += ["", f"KPIs  day {k['day']} so far vs trailing 3 days (pickup and engagement are separate)",
            f"  pickup      {pct(k['pickup']['today'])} vs {pct(k['pickup']['trailing'])}   (dials {k['pickup']['dials_today']} vs {k['pickup']['dials_trailing']})",
            f"  engagement  {pct(k['engagement']['today'])} vs {pct(k['engagement']['trailing'])}   (connects {k['engagement']['connects_today']} vs {k['engagement']['connects_trailing']})"]
    if d.get("person"):
        out += ["", *person_lines(d["person"])]
    return "\n".join(out)


def render_weekly(items: list[dict], now: int, wid: str | None) -> str:
    head = f"Operational auto-applies (caller_id / schedule), last 7 days to {fmt_minute(now)}" + (f", {wid}" if wid else "")
    out = [head, ""]
    for a in items:
        out.append(f"  {a['at']}  {a['workflow_id']}  v{a['applied_version']}  {', '.join(a['dims'])}  [{a['proposal_id']}]")
        out.append(f"      reason: {a['reason']}")
        if a.get("rationale"):
            out.append(f"      evidence: {a['rationale']}")
    if not items:
        out.append("  none in this period")
    return "\n".join(out)
