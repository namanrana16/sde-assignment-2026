"""Per-lead, per-arm counts for the experiment tests, straight from the ``calls`` table (R7: our own store)."""
from __future__ import annotations

import json
from typing import Sequence

from . import settings
from .exp_rules import ArmData
from .store import Store

DAY = settings.MINUTES_PER_DAY
_COLUMN = {"welcome_message": "welcome", "voice": "voice"}


def column(dim: str) -> str:
    return _COLUMN[dim]


def _cell_filter(cells: Sequence[int] | None) -> tuple[str, list]:
    if cells is None:
        return "", []
    if not cells:
        return " AND 0", []
    if all(c < 0 for c in cells):  # negative codes (-1 - cell) mean "every cell EXCEPT these" (overridden cohorts)
        return (f" AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.lid=calls.lid AND l.cell IN ({','.join('?' * len(cells))}))",
                [-1 - c for c in cells])
    # EXISTS = one primary-key probe per call row; the old ``lid IN (SELECT lid FROM leads WHERE cell IN ..)`` scanned
    # every lead of every workflow (about 1.5 s per query at 2.3M leads) and dominated the end-of-day look
    return (f" AND EXISTS (SELECT 1 FROM leads l WHERE l.lid=calls.lid AND l.cell IN ({','.join('?' * len(cells))}))",
            list(cells))


def _group(store: Store, sql: str, args: list) -> dict[str, dict[int, tuple[float, float]]]:
    out: dict[str, dict[int, tuple[float, float]]] = {}
    for arm, lid, n, x in store.conn.execute(sql, args):
        out.setdefault(arm, {})[lid] = (float(n), float(x))
    return out


_STRATUM = "(day*100000 + config_version)"  # one stratum per day and live config version (= share regime)


def _group_strata(store: Store, sql: str, args: list) -> dict[str, dict[str, dict[int, tuple[float, float]]]]:
    out: dict[str, dict[str, dict[int, tuple[float, float]]]] = {}
    for arm, stratum, lid, n, x in store.conn.execute(sql, args):
        out.setdefault(arm, {}).setdefault(str(stratum), {})[lid] = (float(n), float(x))
    return out


def _flatten(strata: dict[str, dict[int, tuple[float, float]]]) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    for rows in strata.values():
        for lid, (n, x) in rows.items():
            a = out.get(lid, (0.0, 0.0))
            out[lid] = (a[0] + n, a[1] + x)
    return out


def _scan(store: Store, wid: str, col: str, day_sql: str, day_args: list, start_minute: int,
          cf: str, cargs: list) -> tuple[list, list]:
    base = f" FROM calls WHERE workflow_id=? AND {day_sql} AND ts_minute>=?{cf}"
    args = [wid, *day_args, start_minute, *cargs]
    eng = store.conn.execute(f"SELECT {col}, {_STRATUM}, lid, COUNT(*), SUM(engaged){base} AND connected=1"
                             f" GROUP BY {col}, {_STRATUM}, lid", args).fetchall()
    pick = store.conn.execute(f"SELECT {col}, lid, COUNT(*), SUM(connected){base} GROUP BY {col}, lid", args).fetchall()
    return eng, pick


def _day_counts(store: Store, wid: str, col: str, day: int, cells: Sequence[int] | None, cf: str,
                cargs: list) -> tuple[list, list]:
    """Arm counts of ONE completed day, computed once and cached (a finished day never changes)."""
    key = json.dumps(sorted(cells)) if cells is not None else "-"
    row = store.conn.execute("SELECT payload FROM exp_day_cache WHERE workflow_id=? AND day=? AND col=? AND cells=?",
                             (wid, day, col, key)).fetchone()
    if row is not None:
        data = json.loads(row[0])
        return data["e"], data["p"]
    eng, pick = _scan(store, wid, col, "day=?", [day], day * DAY, cf, cargs)
    if not getattr(store, "readonly", False):  # the view/UI connection is read-only: compute, do not cache
        store.conn.execute("INSERT OR REPLACE INTO exp_day_cache(workflow_id, day, col, cells, payload) VALUES (?,?,?,?,?)",
                           (wid, day, col, key, json.dumps({"e": eng, "p": pick})))
    return eng, pick


def load_arms(store: Store, wid: str, dim: str, start_minute: int, now: int,
              cells: Sequence[int] | None = None) -> dict[str, ArmData]:
    """Arm data for calls at or after ``start_minute`` (``recent`` = the last EXP_RECENT_MINUTES).

    Engagement is also returned per stratum (day x the config version live at the call = share regime),
    so contrasts can be Mantel-Haenszel pooled. Days that are complete and wholly inside the window are
    served from ``exp_day_cache`` (identical to a raw scan); only the partial first day and today are scanned,
    so the per-tick cost stops growing with the length of the experiment.
    """
    col = column(dim)
    cf, cargs = _cell_filter(cells)
    today, first = now // DAY, start_minute // DAY
    full = [d for d in range(first, today) if d * DAY >= start_minute]
    rest = [d for d in range(first, today + 1) if d not in set(full)]
    eng_rows: list = []
    pick_rows: list = []
    for d in full:
        e, p = _day_counts(store, wid, col, d, cells, cf, cargs)
        eng_rows += e
        pick_rows += p
    if rest:
        e, p = _scan(store, wid, col, f"day IN ({','.join('?' * len(rest))})", rest, start_minute, cf, cargs)
        eng_rows += e
        pick_rows += p
    eng_s: dict = {}
    for arm, stratum, lid, n, x in eng_rows:
        slot = eng_s.setdefault(arm, {}).setdefault(str(stratum), {})
        a = slot.get(lid, (0.0, 0.0))
        slot[lid] = (a[0] + n, a[1] + x)
    pick: dict = {}
    for arm, lid, n, c in pick_rows:
        slot = pick.setdefault(arm, {})
        a = slot.get(lid, (0.0, 0.0))
        slot[lid] = (a[0] + n, a[1] + c)
    recent_from = max(start_minute, now - settings.EXP_RECENT_MINUTES)
    rec_s = _group_strata(store, f"SELECT {col}, {_STRATUM}, lid, COUNT(*), SUM(engaged) FROM calls WHERE workflow_id=?"
                                 f" AND day>=? AND ts_minute>=?{cf} AND connected=1 GROUP BY {col}, {_STRATUM}, lid",
                          [wid, recent_from // DAY, recent_from, *cargs])
    return {arm: ArmData(_flatten(eng_s.get(arm, {})), _flatten(rec_s.get(arm, {})), pick.get(arm, {}),
                         eng_s.get(arm, {}), rec_s.get(arm, {}))
            for arm in sorted(set(eng_s) | set(pick) | set(rec_s))}


def _load_arms_raw(store: Store, wid: str, dim: str, start_minute: int, now: int,
              cells: Sequence[int] | None = None) -> dict[str, ArmData]:
    """Reference implementation: one scan of ``calls`` (tests compare the cached path against it).

    Arm data for calls at or after ``start_minute`` (``recent`` = the last EXP_RECENT_MINUTES).

    Engagement is also returned per stratum (day x the config version live at the call = share regime),
    so contrasts can be Mantel-Haenszel pooled.
    """
    col = column(dim)
    cf, cargs = _cell_filter(cells)
    base = f" FROM calls WHERE workflow_id=? AND day>=? AND ts_minute>=?{cf}"
    lead = [wid, start_minute // DAY, start_minute, *cargs]
    eng_s = _group_strata(store, f"SELECT {col}, {_STRATUM}, lid, COUNT(*), SUM(engaged){base} AND connected=1"
                                 f" GROUP BY {col}, {_STRATUM}, lid", lead)
    pick = _group(store, f"SELECT {col}, lid, COUNT(*), SUM(connected){base} GROUP BY {col}, lid", lead)
    recent_from = max(start_minute, now - settings.EXP_RECENT_MINUTES)
    rec_s = _group_strata(store, f"SELECT {col}, {_STRATUM}, lid, COUNT(*), SUM(engaged) FROM calls WHERE workflow_id=?"
                                 f" AND day>=? AND ts_minute>=?{cf} AND connected=1 GROUP BY {col}, {_STRATUM}, lid",
                          [wid, recent_from // DAY, recent_from, *cargs])
    return {arm: ArmData(_flatten(eng_s.get(arm, {})), _flatten(rec_s.get(arm, {})), pick.get(arm, {}),
                         eng_s.get(arm, {}), rec_s.get(arm, {}))
            for arm in sorted(set(eng_s) | set(pick) | set(rec_s))}


def first_exposure_minute(store: Store, wid: str, dim: str, options: Sequence[str], since: int = 0) -> int | None:
    """First call served by any of ``options`` (the decay clock of a challenger starts here)."""
    if not options:
        return None
    col = column(dim)
    row = store.conn.execute(f"SELECT MIN(ts_minute) FROM calls WHERE workflow_id=? AND ts_minute>=? AND {col} IN"
                             f" ({','.join('?' * len(options))})", [wid, since, *options]).fetchone()
    return None if row is None or row[0] is None else int(row[0])
