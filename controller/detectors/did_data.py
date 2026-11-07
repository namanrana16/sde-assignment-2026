"""Hourly per-DID dial/connect series from the cube, with an incremental per-process cache."""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import cube
from ..store import Store


@dataclass
class DidSeries:
    dials: dict[int, dict[str, int]] = field(default_factory=dict)  # abs_hour -> did -> dials
    conns: dict[int, dict[str, int]] = field(default_factory=dict)
    complete_day: int | None = None  # every day <= this is final in the cache

    def first_hour(self, did: str) -> int | None:
        hours = [h for h, row in self.dials.items() if row.get(did)]
        return min(hours) if hours else None

    def total_dials(self, did: str) -> int:
        return sum(row.get(did, 0) for row in self.dials.values())

    def window(self, hours: range) -> tuple[dict[str, int], dict[str, int]]:
        """(dials, connects) per DID over the absolute-hour range."""
        d: dict[str, int] = {}
        c: dict[str, int] = {}
        for h in hours:
            for did, n in self.dials.get(h, {}).items():
                d[did] = d.get(did, 0) + n
                c[did] = c.get(did, 0) + self.conns.get(h, {}).get(did, 0)
        return d, c

    def obs(self) -> list[tuple[int, str, int, int]]:
        return [(h, did, n, self.conns.get(h, {}).get(did, 0))
                for h, row in sorted(self.dials.items()) for did, n in sorted(row.items()) if n]


_CACHE: dict[tuple[str, str], DidSeries] = {}


def load_series(store: Store, wid: str, now: int) -> DidSeries:
    """Per-hour, per-DID dials and connects up to ``now`` (completed days are cached)."""
    key = (str(store.path), wid)
    ser = _CACHE.get(key)
    today = now // 1440
    if ser is None or (ser.complete_day is not None and ser.complete_day >= today):
        ser = DidSeries()
        _CACHE[key] = ser
    first = None if ser.complete_day is None else ser.complete_day + 1
    for h in [h for h in ser.dials if first is not None and h // 24 >= first]:
        del ser.dials[h]
        ser.conns.pop(h, None)
    for row in cube.rollup(store, wid, by=("day", "hour", "caller_id"), day_min=first):
        day, hour, did = row.key
        abs_hour = day * 24 + hour
        ser.dials.setdefault(abs_hour, {})[did] = row.dials
        ser.conns.setdefault(abs_hour, {})[did] = row.connects
    ser.complete_day = today - 1
    return ser


def forget(store: Store | None = None) -> None:
    """Drop cached series (tests, or a store being reopened)."""
    for key in [k for k in _CACHE if store is None or k[0] == str(store.path)]:
        del _CACHE[key]
