"""Incremental per-prompt flag/adherence/engagement tracker over connected calls (CONTEXT C8, C3).

State per workflow (JSON in ``detector_state``): per-PROMPT rolling hourly buckets
(``prompts[prompt]["hours"][abs_hour] = counts``) plus lifetime totals per prompt. Under
``next_campaign_only`` old and new prompts interleave on the same hour, so calls are bucketed by the
prompt printed on the call, never by "the last prompt seen": the live prompt's bucket is tested
directly and a known-good prompt's counts are never polluted by other prompts. A frozen baseline of
the first prompt (history, or the first connects of a cold start) is kept separately.
``bad`` maps prompt -> {minute, reason}; entries expire and can be lifted (see ``is_bad``).
"""
from __future__ import annotations

from typing import Mapping

from .. import settings
from ..store import Store
from .common import load_state, save_state

DETECTOR = "halluc"
BUCKET_KEEP_HOURS = 96
# counts: [connects, flags, sum_adherence, engaged]


def _add(a: list, b: list) -> list:
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3]]


def _zero() -> list:
    return [0, 0, 0.0, 0]


def fresh_state() -> dict:
    return {"last_event": 0, "first_prompt": None, "base": _zero(), "base_frozen": False,
            "prompts": {}, "bad": {}, "day0": {}}


def _slot(st: dict, prompt: str, first_ts: int) -> dict:
    return st["prompts"].setdefault(prompt, {"total": _zero(), "hours": {}, "first_ts": first_ts, "last_ts": first_ts})


def _apply_group(st: dict, prompt: str, hour: int, counts: list, first_ts: int, last_ts: int) -> None:
    slot = _slot(st, prompt, first_ts)
    slot["total"] = _add(slot["total"], counts)
    slot["last_ts"] = max(slot["last_ts"], last_ts)
    slot["first_ts"] = min(slot["first_ts"], first_ts)
    key = str(hour)
    slot["hours"][key] = _add(slot["hours"].get(key, _zero()), counts)
    if st["first_prompt"] is None:
        st["first_prompt"] = prompt
    if prompt == st["first_prompt"] and not st["base_frozen"]:
        if hour >= 0 and st["base"][0] >= settings.HALLUC_BASELINE_MIN_CONNECTS:
            st["base_frozen"] = True  # history (or the first cold-start connects) is complete
        else:
            st["base"] = _add(st["base"], counts)


def _upgrade(st: dict) -> dict:
    st.setdefault("prompts", {})
    st.setdefault("day0", {})
    if isinstance(st.get("bad"), list):
        st["bad"] = {p: {"minute": 0, "reason": "legacy"} for p in st["bad"]}
    st.setdefault("bad", {})
    return st


def update(store: Store, wid: str) -> dict:
    """Fold calls newer than the stored event id into the state; returns (and saves) it."""
    st = _upgrade(load_state(store, DETECTOR, wid) or fresh_state())
    rows = store.conn.execute(
        "SELECT prompt, day*24+hour AS h, COUNT(*), COALESCE(SUM(halluc_flag),0), COALESCE(SUM(adherence),0.0),"
        " SUM(engaged), MIN(ts_minute), MAX(ts_minute), MAX(event_id) FROM calls"
        " WHERE workflow_id=? AND event_id>? AND connected=1 GROUP BY prompt, h ORDER BY MIN(ts_minute), h",
        (wid, st["last_event"])).fetchall()
    for prompt, hour, n, flags, adh, eng, lo, hi, last_id in rows:
        _apply_group(st, prompt, hour, [n, flags, adh, eng], lo, hi)
        st["last_event"] = max(st["last_event"], last_id)
    if rows:
        newest = max(int(k) for slot in st["prompts"].values() for k in slot["hours"])
        for slot in st["prompts"].values():
            slot["hours"] = {k: v for k, v in slot["hours"].items() if int(k) > newest - BUCKET_KEEP_HOURS}
        save_state(store, DETECTOR, wid, st)
    return st


def counts_since(st: Mapping, prompt: str, since_minute: int, upto_minute: int | None = None) -> list:
    """Counts of one prompt over hours ``>= since_minute // 60`` (and ``<= upto_minute // 60``)."""
    out = _zero()
    slot = st["prompts"].get(prompt)
    if slot is None:
        return out
    lo = since_minute // 60
    hi = None if upto_minute is None else upto_minute // 60
    for k, v in slot["hours"].items():
        if int(k) >= lo and (hi is None or int(k) <= hi):
            out = _add(out, v)
    return out


def window_counts(st: Mapping, prompt: str, now: int) -> list:
    """The prompt's connects/flags/adherence/engaged over the rolling day ending at ``now``."""
    return counts_since(st, prompt, now - settings.HALLUC_WINDOW_MINUTES + 60, now)


def total_counts(st: Mapping, prompt: str) -> list:
    slot = st["prompts"].get(prompt)
    return list(slot["total"]) if slot else _zero()


def is_bad(st: Mapping, prompt: str, now: int) -> bool:
    """A prompt we declared bad stays blacklisted for ``HALLUC_BAD_EXPIRY_MINUTES``."""
    entry = st["bad"].get(prompt)
    return entry is not None and now - entry["minute"] < settings.HALLUC_BAD_EXPIRY_MINUTES


def mark_bad(st: dict, prompt: str, now: int, reason: str) -> None:
    st["bad"][prompt] = {"minute": now, "reason": reason}


def lift_bad(st: dict, prompt: str) -> None:
    st["bad"].pop(prompt, None)


def known_good_candidates(st: Mapping, current: str, now: int) -> list[tuple[str, list]]:
    """Other prompts with enough data (newest use first), excluding ones currently blacklisted."""
    out = [(p, d["total"], d["last_ts"]) for p, d in st["prompts"].items()
           if p != current and not is_bad(st, p, now) and d["total"][0] >= settings.HALLUC_BASELINE_MIN_CONNECTS]
    return [(p, c) for p, c, _ in sorted(out, key=lambda t: -t[2])]


def save(store: Store, wid: str, st: dict) -> None:
    save_state(store, DETECTOR, wid, st)
