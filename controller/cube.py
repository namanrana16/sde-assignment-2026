"""Query and roll-up helpers over the ``cube`` table."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .cohorts import FeatureVocab, matches
from .store import Store

GROUP_COLUMNS = ("day", "hour", "cell", "welcome", "voice", "caller_id", "prompt")


@dataclass(frozen=True)
class CubeRow:
    key: tuple
    dials: int
    connects: int
    engaged: int
    flags: int
    sum_adherence: float

    @property
    def pickup(self) -> float | None:
        return self.connects / self.dials if self.dials else None

    @property
    def engagement(self) -> float | None:
        return self.engaged / self.connects if self.connects else None

    @property
    def flag_rate(self) -> float | None:
        return self.flags / self.connects if self.connects else None


def cells_matching(store: Store, vocab: FeatureVocab, workflow_id: str,
                   where: Mapping[str, Sequence[str]]) -> list[int]:
    """Cell codes present for the workflow whose features satisfy a cohort predicate."""
    rows = store.conn.execute("SELECT DISTINCT cell FROM leads WHERE workflow_id=?",
                              (workflow_id,)).fetchall()
    return [c for (c,) in rows if matches(where, vocab.decode(c))]


def rollup(store: Store, workflow_id: str, by: Sequence[str] = ("day",),
           day_min: int | None = None, day_max: int | None = None,
           variant: Mapping[str, str] | None = None,
           cells: Sequence[int] | None = None) -> list[CubeRow]:
    """Aggregate the cube. ``by`` columns must be from ``GROUP_COLUMNS``.

    ``variant`` filters on welcome/voice/caller_id/prompt; ``cells`` restricts to cohort cells.
    """
    for col in by:
        if col not in GROUP_COLUMNS:
            raise ValueError(f"cannot group by {col!r}")
    where, args = ["workflow_id=?"], [workflow_id]
    if day_min is not None:
        where.append("day>=?")
        args.append(day_min)
    if day_max is not None:
        where.append("day<=?")
        args.append(day_max)
    for col, val in (variant or {}).items():
        if col not in ("welcome", "voice", "caller_id", "prompt"):
            raise ValueError(f"cannot filter on {col!r}")
        where.append(f"{col}=?")
        args.append(val)
    if cells is not None:
        if not cells:
            return []
        where.append(f"cell IN ({','.join('?' * len(cells))})")
        args.extend(cells)
    select = ", ".join(by) + ", " if by else ""
    sql = (f"SELECT {select}SUM(dials), SUM(connects), SUM(engaged), SUM(flags), SUM(sum_adherence)"
           f" FROM cube WHERE {' AND '.join(where)}" + (f" GROUP BY {', '.join(by)} ORDER BY {', '.join(by)}" if by else ""))
    n = len(by)
    return [CubeRow(tuple(r[:n]), *r[n:]) for r in store.conn.execute(sql, args).fetchall()
            if r[n] is not None]


def totals(store: Store, workflow_id: str, **kw) -> CubeRow:
    """Single aggregate row (zeros when nothing matches)."""
    rows = rollup(store, workflow_id, by=(), **kw)
    return rows[0] if rows else CubeRow((), 0, 0, 0, 0, 0.0)
