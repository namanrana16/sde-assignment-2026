"""Read-only data layer for the supervisor view (C11): ``python -m controller view`` and ``controller ui``.

Everything here reads OUR store only (R7) through a read-only connection, so it is safe beside a live controller
(WAL). :func:`build` returns plain dicts; :mod:`controller.view` renders text and :mod:`controller.ui` HTML.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import cube, settings
from .audit import fmt_minute
from .store import Store

DAY = settings.MINUTES_PER_DAY
DIMS = ("welcome_message", "voice")
IN_FLIGHT = ("INTENT", "PENDING", "APPROVED")
Z95 = 1.96


class ReadOnlyStore(Store):
    """A :class:`Store` over a ``mode=ro`` connection: no schema creation, no writes."""

    readonly = True  # derived-cache writers (exp_data) check this and skip their write

    def __init__(self, path: str | Path) -> None:  # noqa: super().__init__ would open read-write
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                    timeout=settings.SQLITE_BUSY_TIMEOUT_MS / 1000)
        self.conn.execute("PRAGMA query_only=ON")


def open_readonly(path: str | Path) -> ReadOnlyStore:
    return ReadOnlyStore(path)


_MISSING_SCHEMA = re.compile(r"no such (table|column)", re.IGNORECASE)
NEWEST_CALL_WINDOW = 1000  # event_ids are assigned in ingestion order, so the newest minute is in the last few rows


def newest_call_minute(store: Store) -> int | None:
    """Newest stored call minute without scanning ``calls`` (no ts_minute index; ~7M rows on a long run).

    Ingestion order is time order, so the max over the last ``NEWEST_CALL_WINDOW`` primary-key rows is the global max.
    """
    r = rows(store, "SELECT MAX(ts_minute) AS m FROM calls WHERE event_id > (SELECT MAX(event_id) FROM calls) - ?",
             (NEWEST_CALL_WINDOW,))
    return r[0]["m"] if r and r[0]["m"] is not None else None


def rows(store: Store, sql: str, args: tuple = ()) -> list[dict]:
    try:
        cur = store.conn.execute(sql, args)
    except sqlite3.OperationalError as exc:
        if _MISSING_SCHEMA.search(str(exc)):  # table/column not there yet (older DB): show nothing, do not crash
            return []
        raise  # "database is locked" and friends must not look like "nothing waiting on you"
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def now_minute(store: Store) -> int:
    """The "now" of the view: the newest sim minute, but not past the newest stored call + 1.

    Overnight (and after the run finishes) nothing new arrives; "today" then stays the last operating day instead of
    becoming an empty midnight day.
    """
    tick = rows(store, "SELECT MAX(sim_minute) AS m FROM metrics")
    t = tick[0]["m"] if tick and tick[0]["m"] is not None else None
    c = newest_call_minute(store)
    if t is None:
        return int(c or 0)
    return int(t if c is None else min(t, c + 1))


def workflow_ids(store: Store) -> list[str]:
    ids = {r["workflow_id"] for r in rows(store, "SELECT DISTINCT workflow_id FROM config_versions")}
    ids |= {k.split(":", 1)[1] for (k,) in store.conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'workflow_policy:%'")}
    return sorted(ids)


def _latest_config(store: Store, wid: str) -> tuple[int, dict] | None:
    r = rows(store, "SELECT version, body FROM config_versions WHERE workflow_id=? ORDER BY version DESC LIMIT 1", (wid,))
    if not r or not r[0]["body"]:
        return None
    return r[0]["version"], json.loads(r[0]["body"])


def _state(store: Store, detector: str, key: str) -> dict:
    r = rows(store, "SELECT value FROM detector_state WHERE detector=? AND key=?", (detector, key))
    return json.loads(r[0]["value"]) if r else {}


def wilson(x: float, n: float) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    d = 1 + Z95 ** 2 / n
    centre = (p + Z95 ** 2 / (2 * n)) / d
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


# ------------------------------------------------------------------ (a) what is being tested
def _arm_stats(store: Store, wid: str, dim: str, st: dict, now: int) -> dict[str, dict]:
    """Per-arm connects, engagement (Wilson 95% CI) and lift vs control (clustered log-ratio CI)."""
    from . import exp_data, exp_rules  # the controller's own analysis code: same numbers the looks use
    start = st.get("start_minute")
    if start is None:
        return {}
    data = exp_data.load_arms(store, wid, dim, start, now)
    control = st["control"]
    arms = [control] + [a for a in sorted(st.get("arms", {}))]
    summary = exp_rules.summarise(data, control, [a for a in arms if a != control])
    out: dict[str, dict] = {}
    for arm in arms:
        d = data.get(arm, exp_rules.EMPTY)
        n = sum(v[0] for v in d.engaged.values())
        x = sum(v[1] for v in d.engaged.values())
        lo, hi = wilson(x, n)
        rec = {"connects": int(n), "engagement": (x / n) if n else None, "ci": (lo, hi) if n else None,
               "lift": None, "lift_ci": None}
        s = summary.get(arm)
        if s and s.get("ok"):
            eff = math.log1p(s["lift"])
            rec["lift"] = s["lift"]
            rec["lift_ci"] = (math.expm1(eff - Z95 * s["se"]), math.expm1(eff + Z95 * s["se"]))
        out[arm] = rec
    return out


def _last_look(store: Store, wid: str, dim: str) -> dict:
    for r in rows(store, "SELECT sim_minute, inputs FROM decisions WHERE workflow_id=? AND kind='experiment_look'"
                  " ORDER BY id DESC LIMIT 12", (wid,)):
        inp = _json(r["inputs"]) or {}
        if inp.get("dim") == dim and inp.get("scope") == "all":
            bounds = {a: rec.get("boundary") for a, rec in (inp.get("records") or {}).items() if rec.get("boundary")}
            return {"minute": r["sim_minute"], "index": inp.get("look_index"), "took_look": inp.get("took_look"),
                    "boundaries": bounds}
    return {}


def experiments(store: Store, wid: str, now: int) -> list[dict]:
    cfg = _latest_config(store, wid)
    out = []
    for dim in DIMS:
        st = _state(store, "experiments", f"{wid}:{dim}")
        if not st:
            continue
        live = ((cfg[1].get("allocations", {}).get("default") or {}).get(dim) or {}) if cfg else {}
        item = {"dim": dim, "phase": st.get("phase"), "control": st.get("control"), "live_shares": live,
                "arm_states": {a: v.get("state") for a, v in (st.get("arms") or {}).items()},
                "looks": (st.get("scopes", {}).get("all") or {}).get("looks", 0),
                "max_looks": len(settings.EXP_LOOK_FRACTIONS), "exhausted": bool(st.get("exhausted")),
                "started": st.get("start_minute"), "last_look": _last_look(store, wid, dim), "arms": {}}
        if st.get("phase") in ("running", "promoted"):
            item["arms"] = _arm_stats(store, wid, dim, st, now)
        out.append(item)
    return out


def did_probes(store: Store, wid: str) -> list[dict]:
    st = _state(store, "did", wid)
    cfg = _latest_config(store, wid)
    shares = ((cfg[1].get("allocations", {}).get("default") or {}).get("caller_id") or {}) if cfg else {}
    out = []
    for did, share in sorted(shares.items()):
        status = next((s for s in ("flagged", "weak", "reprobe", "tested") if did in (st.get(s) or {})), "untested")
        out.append({"did": did, "share": share, "status": status})
    return out


# ------------------------------------------------------------------ (b) what is winning
def winners(store: Store, wid: str) -> list[dict]:
    out = []
    for dim in DIMS:
        st = _state(store, "experiments", f"{wid}:{dim}")
        promo = st.get("promo")
        if not promo or st.get("phase") != "promoted":
            continue
        dec = st.get("decay_status") or {}
        cus, held = dec.get("cusum") or {}, dec.get("holdout") or {}
        out.append({"dim": dim, "winner": promo["winner"], "old_control": promo["old_control"], "scope": promo.get("scope"),
                    "since": promo.get("minute"), "cusum_S": cus.get("S"), "cusum_alarm": cus.get("alarm"),
                    "cusum_blocks": cus.get("blocks"), "holdout_recent_lift": held.get("recent_lift"),
                    "holdout_connects": held.get("holdout_connects"), "rollback": bool(dec.get("rollback")),
                    "checked": bool(dec)})
    return out


# ------------------------------------------------------------------ (c) what we need from you
def approvals(store: Store, wid: str) -> list[dict]:
    out = []
    for r in rows(store, "SELECT proposal_id, kind, state, approval_id, filed_minute, apply_by_minute, expires_minute, body"
                  " FROM proposals WHERE workflow_id=? AND state IN ('INTENT','PENDING','APPROVED')"
                  " AND route='approval' ORDER BY plan_seq", (wid,)):
        body = _json(r.pop("body")) or {}
        out.append({**r, "summary": body.get("summary"), "impact_score": body.get("impact_score"),
                    "affected_leads": body.get("affected_leads"), "dims": body.get("dims")})
    return out


# ------------------------------------------------------------------ (d) what changed today
def changes_today(store: Store, wid: str, now: int) -> list[dict]:
    day = now // DAY
    out = []
    for r in rows(store, "SELECT version, actor, ts_minute, changed_dims, proposal_id FROM config_versions"
                  " WHERE workflow_id=? AND ts_minute>=? AND ts_minute<=? ORDER BY version",
                  (wid, day * DAY, now)):
        prop = rows(store, "SELECT body FROM proposals WHERE proposal_id=?", (r["proposal_id"],)) if r["proposal_id"] else []
        body = _json(prop[0]["body"]) if prop else None
        if body and body.get("summary"):
            reason = body["summary"]
        elif r["actor"] == "supervisor":
            reason = "edited by the client supervisor (not a controller change)"
        else:
            reason = "no proposal recorded for this version"
        at = fmt_minute(r["ts_minute"])
        out.append({**r, "changed_dims": _json(r["changed_dims"]) or [], "reason": reason, "at": at,
                    "why": f'python -m controller why --workflow {wid} --at "{at}"'})
    return out


def weekly_auto_applies(store: Store, wid: str | None, now: int, days: int = 7) -> list[dict]:
    """Operational (caller_id / schedule) auto-applies in the last ``days`` days, for Compliance."""
    since = now - days * DAY
    sql = ("SELECT p.proposal_id, p.workflow_id, p.kind, p.applied_version, p.applied_minute, p.body, c.changed_dims"
           " FROM proposals p LEFT JOIN config_versions c ON c.workflow_id=p.workflow_id AND c.version=p.applied_version"
           " WHERE p.route='auto' AND p.state IN ('AUTO_APPLIED','MONITORING','CLOSED') AND p.applied_minute>=?")
    args: tuple = (since,)
    if wid:
        sql += " AND p.workflow_id=?"
        args += (wid,)
    out = []
    for r in rows(store, sql + " ORDER BY p.applied_minute", args):
        body = _json(r.pop("body")) or {}
        dims = _json(r["changed_dims"]) or body.get("dims") or []
        operational = any(d in ("caller_id", "schedule") or str(d).startswith("schedule") for d in dims)
        if not operational:
            continue
        req = body.get("request") or {}
        out.append({**r, "dims": dims, "at": fmt_minute(r["applied_minute"]), "reason": body.get("summary"),
                    "rationale": req.get("rationale")})
    return out


# ------------------------------------------------------------------ (e) incidents and alerts
def open_incidents(store: Store, wid: str) -> list[dict]:
    out = []
    for r in rows(store, "SELECT id, kind, opened_minute, body FROM incidents WHERE workflow_id=? AND state='open'", (wid,)):
        body = _json(r.pop("body")) or {}
        out.append({**r, "opened": fmt_minute(r["opened_minute"]), "detail": body.get("did") or body.get("prompt")
                    or body.get("live") or ""})
    return out


def alerts(store: Store, wid: str | None, now: int, hours: int = 24) -> dict[str, list[dict]]:
    """Actionable alerts of the last ``hours`` (page/digest from the rule engine); other rows only counted by code."""
    since = now - hours * 60
    sql = "SELECT id, sim_minute, workflow_id, severity, code, message FROM alerts WHERE (sim_minute IS NULL OR sim_minute>=?)"
    args: tuple = (since,)
    if wid:
        sql += " AND (workflow_id=? OR workflow_id IS NULL)"
        args += (wid,)
    page, digest, other = [], [], {}
    for r in rows(store, sql + " ORDER BY id", args):
        if r["sim_minute"] is not None and r["sim_minute"] > now:
            continue
        if r["severity"] == "page":
            page.append(r)
        elif r["severity"] == "digest":
            digest.append(r)
        else:
            other[r["code"]] = other.get(r["code"], 0) + 1
    return {"page": page, "digest": digest, "other": [{"code": c, "count": n} for c, n in sorted(other.items())]}


# ------------------------------------------------------------------ (f) KPIs
def kpis(store: Store, wid: str, now: int) -> dict:
    """Pickup and engagement, today vs the trailing three days: never blended into one number."""
    day = now // DAY
    today = cube.totals(store, wid, day_min=day, day_max=day)
    prior = cube.totals(store, wid, day_min=day - 3, day_max=day - 1)
    return {"day": day,
            "pickup": {"today": today.pickup, "trailing": prior.pickup, "dials_today": today.dials,
                       "dials_trailing": prior.dials},
            "engagement": {"today": today.engagement, "trailing": prior.engagement,
                           "connects_today": today.connects, "connects_trailing": prior.connects}}


def person_analysis(store: Store, wid: str) -> dict | None:
    """Latest per-person call-time analysis of this workflow (C4b, analysis only), from its audit decision row."""
    r = rows(store, "SELECT sim_minute, inputs FROM decisions WHERE kind='person_analysis' AND workflow_id=? "
                    "ORDER BY id DESC LIMIT 1", (wid,))
    inputs = _json(r[0]["inputs"]) if r else None
    if not inputs:
        return None
    n = rows(store, "SELECT COUNT(*) AS n FROM person_analysis WHERE workflow_id=?", (wid,))
    return {**inputs, "minute": r[0]["sim_minute"], "table": "person_analysis", "rows": n[0]["n"] if n else 0}


# ------------------------------------------------------------------ assembly
def health(store: Store, wid: str, now: int) -> str:
    """ok / attention / page for the workflow list badge."""
    al = alerts(store, wid, now)
    if al["page"]:
        return "page"
    if open_incidents(store, wid) or al["digest"] or approvals(store, wid):
        return "attention"
    return "ok"


def build(store: Store, wid: str, now: int | None = None) -> dict:
    now = now_minute(store) if now is None else now
    return {"workflow": wid, "now": now, "now_text": fmt_minute(now), "experiments": experiments(store, wid, now),
            "did_probes": did_probes(store, wid), "winners": winners(store, wid), "approvals": approvals(store, wid),
            "changes": changes_today(store, wid, now), "incidents": open_incidents(store, wid),
            "alerts": alerts(store, wid, now), "kpis": kpis(store, wid, now), "person": person_analysis(store, wid)}
