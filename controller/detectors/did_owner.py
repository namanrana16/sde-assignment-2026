"""Who last changed an allocation key's caller_id (B1): a supervisor's deliberate allocation is never overridden."""
from __future__ import annotations

import json
from typing import Mapping

from .. import settings
from ..configlib import dist, dist_equal

SUPERVISOR = "supervisor"
_EPS = 1e-12


def clock(minute: int | None) -> str:
    if minute is None:
        return "an unknown time"
    d = settings.MINUTES_PER_DAY
    return f"Day {minute // d} {minute % d // 60:02d}:{minute % 60:02d}"


def _alloc(body: Mapping, key: str) -> dict | None:
    try:
        return dict(dist(body, key, "caller_id"))
    except (KeyError, TypeError):
        return None


def _only_removed(prev: Mapping, cur: Mapping) -> bool:
    """Our own change merely dropped DID(s) from the allocation (a flagged-number evacuation): no new DID appears."""
    return set(cur) < {d for d, v in prev.items() if v > _EPS} and not (set(cur) - set(prev))


def last_editor(store, wid: str, key: str) -> dict | None:
    """``{version, actor, minute, proposal_id}`` of the newest config version that changed ``key``'s caller_id
    (None if it never changed since the first mirrored version). Versions without a body are skipped.
    A controller change that only removed DIDs (our evacuation of a flagged number from a supervisor's allocation)
    does not take ownership away from that supervisor."""
    rows = store.conn.execute("SELECT version, actor, ts_minute, proposal_id, body FROM config_versions"
                              " WHERE workflow_id=? ORDER BY version", (wid,)).fetchall()
    prev: dict | None = None
    out: dict | None = None
    for version, actor, ts, proposal_id, body in rows:
        if not body:
            continue
        cur = _alloc(json.loads(body), key)
        if prev is not None and cur is not None and not dist_equal(prev, cur):
            if not (out and out["actor"] == SUPERVISOR and str(actor).startswith("controller") and _only_removed(prev, cur)):
                out = {"version": version, "actor": actor, "minute": ts, "proposal_id": proposal_id}
        if cur is not None:
            prev = cur
    return out


def supervisor_edit(store, wid: str, key: str) -> dict | None:
    ed = last_editor(store, wid, key)
    return ed if ed and ed["actor"] == SUPERVISOR else None


def minimal_evacuation(cur: Mapping[str, float], bad: set[str], tested: list[str]) -> dict[str, float]:
    """Move only the flagged DIDs' share, onto the DIDs the supervisor already allocated (proportionally),
    preferring tested ones; the supervisor's relative split among the rest is preserved."""
    mass = sum(cur.get(d, 0.0) for d in bad)
    keep = {d: v for d, v in cur.items() if d not in bad and v > _EPS}
    sinks = {d: v for d, v in keep.items() if d in tested} or keep
    if not sinks:
        sinks = {d: 1.0 for d in tested}
    total = sum(sinks.values())
    out = dict(keep)
    for d, v in sinks.items():
        out[d] = out.get(d, 0.0) + mass * v / total
    s = sum(out.values()) or 1.0
    return {d: v / s for d, v in out.items()}
