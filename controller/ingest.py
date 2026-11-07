"""Chunk ingestion: one SQLite transaction per chunk, exactly-once effects.

History chunks first, then events chunks, in global chunk order. The ``event_id >
last_event_id`` guard makes replaying a chunk a no-op. Inside the chunk transaction the
lines are parsed and flushed in sub-batches: that caps the memory of the parsed event dicts
only; the raw text of one chunk is still held whole (a 54 MB chunk on public_03).

History fast path: while history chunks are ingested the ``calls_lead`` index does not exist
(random-page writes dominated COMMIT) and ``wal_autocheckpoint`` is raised. Once the last
history chunk is committed, ``Store.build_calls_index`` creates the index and sets the
``calls_index_built`` meta flag in one transaction; events (live) chunks are never ingested
before that. A crash mid-history leaves cursor and rows consistent (one transaction per chunk,
synchronous stays NORMAL, so the cursor is exactly as durable as the rows); the restart sees
no flag, resumes history from the cursor and builds the index when it is done.

Limitation: ``GET /config/history`` returns no config bodies, so a config version whose
``config_changed`` event we never saw (e.g. superseded inside one advance) cannot be
backfilled; calls under it skip the cohort self-check and are reported in one summarised
alert plus the ``cohort_check_skipped`` meta counter.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from itertools import islice

from . import settings
from .client import SimClient
from .cohorts import VOCAB_META_KEY, CohortChecker, FeatureVocab
from .faults import crash_point
from .store import Store

log = logging.getLogger(__name__)
_EPOCH_ORD = date.fromisoformat(settings.EPOCH_DATE).toordinal()
_MAX_MISMATCH_ALERTS = 5
# Pages of WAL allowed before an automatic checkpoint during history ingest (about 1 GB at 4 KiB pages).
# Module constant here because settings.py is owned elsewhere; the PM may move it.
HISTORY_WAL_AUTOCHECKPOINT_PAGES = 250_000
_CONFIG_CHANGED_LINE = re.compile(r'^.*"config_changed".*$', re.MULTILINE)

_CALL_SQL = ("INSERT OR IGNORE INTO calls(event_id, workflow_id, ts_minute, day, hour, lid,"
             " attempt_no, config_version, cohort_id, welcome, voice, caller_id, prompt,"
             " connected, engaged, halluc_flag, adherence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
_CUBE_SQL = ("INSERT INTO cube(workflow_id, day, hour, cell, welcome, voice, caller_id, prompt,"
             " dials, connects, engaged, flags, sum_adherence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
             " ON CONFLICT(workflow_id, day, hour, cell, welcome, voice, caller_id, prompt) DO UPDATE"
             " SET dials=dials+excluded.dials, connects=connects+excluded.connects,"
             " engaged=engaged+excluded.engaged, flags=flags+excluded.flags,"
             " sum_adherence=sum_adherence+excluded.sum_adherence")


class IngestIntegrityError(RuntimeError):
    """Rows inserted differ from rows in the batch: abort the chunk transaction."""


@lru_cache(maxsize=settings.TS_CACHE_MAX)
def ts_to_minute(ts: str) -> int:
    """ISO timestamp (IST) to simulated minute since Day 0 00:00 (negative in history)."""
    dt = datetime.fromisoformat(ts)
    return (dt.date().toordinal() - _EPOCH_ORD) * settings.MINUTES_PER_DAY + dt.hour * 60 + dt.minute


@dataclass(frozen=True)
class IngestStats:
    events: int = 0
    calls: int = 0
    chunks: int = 0
    config_changes: int = 0
    wall_s: float = 0.0

    def __add__(self, other: "IngestStats") -> "IngestStats":
        return IngestStats(self.events + other.events, self.calls + other.calls,
                           self.chunks + other.chunks,
                           self.config_changes + other.config_changes,
                           self.wall_s + other.wall_s)


def mirror_configs(client: SimClient, store: Store, workflow_ids: list[str]) -> None:
    """Store the current config body of each workflow (idempotent)."""
    for wid in workflow_ids:
        cfg = client.get_config(wid)
        store.store_config_body(wid, cfg["version"], cfg.get("actor"), cfg["config"])


class _ChunkBatch:
    """Accumulators for one sub-batch."""

    def __init__(self) -> None:
        self.leads: list[tuple] = []
        self.calls: list[tuple] = []
        self.cube: dict[tuple, list] = {}
        self.mismatches: list[tuple] = []
        self.mismatch_count = 0
        self.config_changes = 0


class Ingestor:
    def __init__(self, client: SimClient, store: Store) -> None:
        self.client = client
        self.store = store
        self.vocab = FeatureVocab.load(store.get_meta(VOCAB_META_KEY))
        self._checker = CohortChecker(self.vocab)
        self._leads: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._cohorts: dict[tuple[str, int], list] = {}
        self._next_lid = 0
        self._skipped: dict[tuple[str, int], int] = {}

    # ------------------------------------------------------------ driver
    def sync(self) -> IngestStats:
        """Ingest every chunk after the cursor (history fast path first, then live events)."""
        total = IngestStats()
        if not self.store.calls_index_ready():
            total += self._sync_history()
        cursor = self.store.get_cursor()
        for meta in sorted(self.client.list_chunks("events", cursor.last_chunk),
                           key=lambda m: m["chunk"]):
            total += self.process_chunk(meta)
        return total

    def _sync_history(self) -> IngestStats:
        """History chunks without the calls_lead index and with a large WAL; then build the index."""
        total = IngestStats()
        pending = self.client.list_chunks("history", self.store.get_cursor().last_chunk)
        self.store.set_wal_autocheckpoint(HISTORY_WAL_AUTOCHECKPOINT_PAGES)
        try:
            for meta in sorted(pending, key=lambda m: m["chunk"]):
                total += self.process_chunk(meta)
        finally:
            self.store.set_wal_autocheckpoint(settings.WAL_AUTOCHECKPOINT_PAGES)
        self.store.build_calls_index()
        self.store.checkpoint()
        return total

    def process_chunk(self, meta: dict) -> IngestStats:
        t0 = time.perf_counter()
        cursor = self.store.get_cursor()
        if meta["last_event_id"] <= cursor.last_event_id:
            with self.store.transaction():
                self.store.set_cursor(max(cursor.last_chunk, meta["chunk"]), cursor.last_event_id)
            return IngestStats(chunks=1)
        text = self.client.get_chunk_text(meta["chunk"])
        fetched = self._fetch_changed_configs(text)
        self._skipped = {}
        n_events = n_calls = n_changes = 0
        last_id = cursor.last_event_id
        try:
            with self.store.transaction():
                self._next_lid = self.store.conn.execute(
                    "SELECT COALESCE(MAX(lid), 0) FROM leads").fetchone()[0]
                for cfg in fetched:
                    self.store.store_config_body(cfg["workflow_id"], cfg["version"],
                                                 cfg.get("actor"), cfg["config"])
                stream = io.StringIO(text)
                while True:
                    group = list(islice(stream, settings.INGEST_SUBBATCH_LINES))
                    if not group:
                        break
                    events = [e for e in map(json.loads, filter(str.strip, group))
                              if e["event_id"] > cursor.last_event_id]
                    del group
                    if not events:
                        continue
                    batch = self._apply(events)
                    self._flush(batch)
                    n_events += len(events)
                    n_calls += len(batch.calls)
                    n_changes += batch.config_changes
                    last_id = max(last_id, events[-1]["event_id"])
                self._report_skipped()
                crash_point("mid-ingest-txn")
                self.store.set_cursor(meta["chunk"], last_id)
        except Exception:
            self._leads.clear()
            self._cohorts.clear()
            raise
        return IngestStats(n_events, n_calls, 1, n_changes, time.perf_counter() - t0)

    # ------------------------------------------------------------ helpers
    def _fetch_changed_configs(self, text: str) -> list[dict]:
        """Current bodies for workflows with a config_changed event in this chunk.

        Pre-scans only the matching lines, so the chunk is not fully parsed up front.
        """
        wfs = sorted({json.loads(m.group(0))["workflow_id"]
                      for m in _CONFIG_CHANGED_LINE.finditer(text)})
        return [self.client.get_config(w) for w in wfs]

    def _apply(self, events: list[dict]) -> _ChunkBatch:
        batch = _ChunkBatch()
        next_lid = self._next_lid
        for ev in events:
            kind = ev["type"]
            if kind == "call":
                self._on_call(ev, batch)
            elif kind == "lead":
                next_lid += 1
                self._on_lead(ev, next_lid, batch)
            elif kind == "config_changed":
                self._on_config_changed(ev, batch)
            elif kind == "approval_decided":
                self._on_approval(ev)
            elif kind == "campaign_created":
                self._on_campaign(ev)
        self._next_lid = next_lid
        return batch

    def _on_lead(self, ev: dict, lid: int, batch: _ChunkBatch) -> None:
        cell = self.vocab.encode(ev["features"])
        self._remember_lead(ev["lead_id"], lid, cell)
        batch.leads.append((lid, ev["lead_id"], ev["person_id"], ev["workflow_id"], cell,
                            ev["campaign_id"], ts_to_minute(ev["ts"])))

    def _remember_lead(self, lead_id: str, lid: int, cell: int) -> None:
        if len(self._leads) >= settings.LEAD_CACHE_MAX:  # evict the older half, keep the recent leads
            for _ in range(len(self._leads) // 2):
                self._leads.popitem(last=False)
        self._leads[lead_id] = (lid, cell)

    def _lookup_lead(self, lead_id: str) -> tuple[int, int] | None:
        hit = self._leads.get(lead_id)
        if hit is None:
            row = self.store.conn.execute("SELECT lid, cell FROM leads WHERE lead_id=?",
                                          (lead_id,)).fetchone()
            if row is None:
                return None
            hit = (row[0], row[1])
            self._remember_lead(lead_id, *hit)
        return hit

    def _on_call(self, ev: dict, batch: _ChunkBatch) -> None:
        pending = self._pending_lead_lookup(ev["lead_id"], batch)
        if pending is None:
            self.store.add_alert(ts_to_minute(ev["ts"]), "warn", "call_without_lead",
                                 f"call event {ev['event_id']} references unknown lead {ev['lead_id']}",
                                 ev["workflow_id"])
            return
        lid, cell = pending
        minute = ts_to_minute(ev["ts"])
        day, hour = minute // settings.MINUTES_PER_DAY, (minute % settings.MINUTES_PER_DAY) // 60
        var, qa = ev["variant"], ev.get("qa")
        flag = int(bool(qa["hallucination_flag"])) if qa else None
        adh = qa.get("flow_adherence") if qa else None
        connected, engaged = int(bool(ev["connected"])), int(bool(ev["engaged"]))
        wf, version = ev["workflow_id"], ev["config_version"]
        batch.calls.append((ev["event_id"], wf, minute, day, hour, lid, ev["attempt_no"], version,
                            ev["cohort_id"], var["welcome_message"], var["voice"], var["caller_id"],
                            var["prompt_version"], connected, engaged, flag, adh))
        key = (wf, day, hour, cell, var["welcome_message"], var["voice"], var["caller_id"],
               var["prompt_version"])
        acc = batch.cube.setdefault(key, [0, 0, 0, 0, 0.0])
        acc[0] += 1
        acc[1] += connected
        acc[2] += engaged
        acc[3] += flag or 0
        acc[4] += adh or 0.0
        self._self_check(ev, cell, version, batch)

    def _pending_lead_lookup(self, lead_id: str, batch: _ChunkBatch) -> tuple[int, int] | None:
        hit = self._leads.get(lead_id)
        if hit is not None:
            return hit
        if batch.leads:  # leads of this batch are not yet flushed to SQL
            self._flush_leads(batch)
        return self._lookup_lead(lead_id)

    def _flush_leads(self, batch: _ChunkBatch) -> None:
        self.store.conn.executemany(
            "INSERT OR IGNORE INTO leads(lid, lead_id, person_id, workflow_id, cell, campaign_id,"
            " ts_minute) VALUES (?,?,?,?,?,?,?)", batch.leads)
        batch.leads = []

    def _cohorts_for(self, wf: str, version: int) -> list | None:
        key = (wf, version)
        got = self._cohorts.get(key)
        if got is None:
            body = self.store.config_body(wf, version)
            if body is None:
                return None
            got = body.get("cohorts", [])
            self._cohorts[key] = got
        return got

    def _self_check(self, ev: dict, cell: int, version: int, batch: _ChunkBatch) -> None:
        cohorts = self._cohorts_for(ev["workflow_id"], version)
        if cohorts is None:
            key = (ev["workflow_id"], version)
            self._skipped[key] = self._skipped.get(key, 0) + 1
            return
        want = self._checker.expected(ev["workflow_id"], version, cohorts, cell)
        if want != ev["cohort_id"]:
            batch.mismatch_count += 1
            if len(batch.mismatches) < _MAX_MISMATCH_ALERTS:
                batch.mismatches.append((ts_to_minute(ev["ts"]), ev["workflow_id"], "error",
                                         "cohort_mismatch",
                                         f"event {ev['event_id']}: ours={want} sim={ev['cohort_id']}"
                                         f" (config v{version})"))

    def _on_config_changed(self, ev: dict, batch: _ChunkBatch) -> None:
        minute = ts_to_minute(ev["ts"])
        self.store.upsert_config_event(ev["workflow_id"], ev["version"], ev.get("actor"), minute,
                                       ev.get("changed_dims"), ev.get("proposal_id"),
                                       ev.get("approval_id"))
        self.store.add_decision(minute, "config_observed",
                                f"v{ev['version']} by {ev.get('actor')} changed {ev.get('changed_dims')}",
                                ev["workflow_id"], {"event_id": ev["event_id"],
                                                    "proposal_id": ev.get("proposal_id"),
                                                    "approval_id": ev.get("approval_id")})
        batch.config_changes += 1

    def _on_approval(self, ev: dict) -> None:
        """Idempotent on event_id (UNIQUE); linked to our proposal when we know the approval."""
        self.store.conn.execute(
            "INSERT OR IGNORE INTO proposal_events(event_id, proposal_id, approval_id, workflow_id,"
            " ts_minute, event, payload) VALUES (?, (SELECT proposal_id FROM proposals WHERE"
            " approval_id=?), ?, ?, ?, ?, ?)",
            (ev["event_id"], ev["approval_id"], ev["approval_id"], ev["workflow_id"],
             ts_to_minute(ev["ts"]), ev["status"], json.dumps(ev.get("reasons", []))))

    def _on_campaign(self, ev: dict) -> None:
        self.store.conn.execute(
            "INSERT OR IGNORE INTO campaigns(campaign_id, workflow_id, ts_minute, leads,"
            " config_version_at_creation) VALUES (?,?,?,?,?)",
            (ev["campaign_id"], ev["workflow_id"], ts_to_minute(ev["ts"]), ev["leads"],
             ev["config_version_at_creation"]))

    def _report_skipped(self) -> None:
        """One summarised alert (and a running counter) for self-checks we could not run."""
        if not self._skipped:
            return
        total = sum(self._skipped.values())
        done = int(self.store.get_meta("cohort_check_skipped") or 0)
        self.store.set_meta("cohort_check_skipped", str(done + total))
        detail = ", ".join(f"{w} v{v}: {n}" for (w, v), n in sorted(self._skipped.items()))
        self.store.add_alert(None, "warn", "cohort_check_skipped",
                             f"{total} calls skipped the cohort self-check (config body unknown; "
                             f"/config/history has no bodies): {detail}")

    def _flush(self, batch: _ChunkBatch) -> None:
        conn = self.store.conn
        if batch.leads:
            self._flush_leads(batch)
        before = conn.total_changes
        conn.executemany(_CALL_SQL, batch.calls)
        inserted = conn.total_changes - before
        if inserted != len(batch.calls):  # cube increments must match rows really inserted
            raise IngestIntegrityError(
                f"inserted {inserted} call rows for a batch of {len(batch.calls)}")
        conn.executemany(_CUBE_SQL, [k + tuple(v) for k, v in batch.cube.items()])
        if batch.mismatches:
            summary = list(batch.mismatches)
            if batch.mismatch_count > len(summary):
                summary.append((summary[-1][0], summary[-1][1], "error", "cohort_mismatch_total",
                                f"{batch.mismatch_count} mismatches in chunk"))
            self.store.add_alerts(summary)
        if self.vocab.dirty:
            self.store.set_meta(VOCAB_META_KEY, self.vocab.dump())
            self.vocab.dirty = False
