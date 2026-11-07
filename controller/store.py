"""SQLite store (WAL) with repository-style helpers."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import settings

log = logging.getLogger(__name__)
SCHEMA_VERSION = "1"
CALLS_INDEX = "calls_lead"
CALLS_INDEX_META = "calls_index_built"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_PROPOSAL_COLUMNS = (("route", "TEXT"), ("priority", "INTEGER"), ("plan_seq", "INTEGER"),
                     ("filed_minute", "INTEGER"), ("apply_by_minute", "INTEGER"),
                     ("expires_minute", "INTEGER"), ("supersedes", "TEXT"),
                     ("applied_version", "INTEGER"), ("applied_minute", "INTEGER"))


@dataclass(frozen=True)
class Cursor:
    last_chunk: int
    last_event_id: int


@dataclass(frozen=True)
class ConfigVersion:
    workflow_id: str
    version: int
    actor: str | None
    body: dict | None


class Store:
    """Single-writer SQLite wrapper. Use ``transaction()`` for atomic groups."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None,
                                    timeout=settings.SQLITE_BUSY_TIMEOUT_MS / 1000)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(f"PRAGMA wal_autocheckpoint={settings.WAL_AUTOCHECKPOINT_PAGES}")
        self.conn.execute(f"PRAGMA cache_size=-{settings.SQLITE_CACHE_KB}")
        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self._settle_calls_index_flag()
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _migrate(self) -> None:
        """Bring databases created by earlier phases up to the current schema."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(proposal_events)")}
        if "event_id" not in have:
            self.conn.execute("ALTER TABLE proposal_events ADD COLUMN event_id INTEGER")
        prop = {r[1] for r in self.conn.execute("PRAGMA table_info(proposals)")}
        for col, typ in _PROPOSAL_COLUMNS:
            if col not in prop:
                self.conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} {typ}")
        # a replayed approval_decided event can never insert twice (NULL ids stay distinct)
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS proposal_events_event_id"
                          " ON proposal_events (event_id)")

        phases = {r[1] for r in self.conn.execute("PRAGMA table_info(tick_phases)")}
        if "observer_s" not in phases:
            self.conn.execute("ALTER TABLE tick_phases ADD COLUMN observer_s REAL")

    # ---------------------------------------------------------------- calls_lead index
    def has_calls_index(self) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                                 (CALLS_INDEX,)).fetchone() is not None

    def _settle_calls_index_flag(self) -> None:
        """A database that already has the index (built before this flag existed) is complete.

        A fresh or mid-history database has no index and no flag: the flag is only ever set, in
        the same transaction as CREATE INDEX, by ``build_calls_index``."""
        if self.get_meta(CALLS_INDEX_META) is None and self.has_calls_index():
            self.set_meta(CALLS_INDEX_META, "1")

    def calls_index_ready(self) -> bool:
        return self.get_meta(CALLS_INDEX_META) == "1"

    def build_calls_index(self) -> None:
        """Create calls_lead and flag history complete atomically (DDL is transactional in SQLite)."""
        with self.transaction():
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS {CALLS_INDEX} ON calls (lid)")
            self.set_meta(CALLS_INDEX_META, "1")

    def set_wal_autocheckpoint(self, pages: int) -> None:
        self.conn.execute(f"PRAGMA wal_autocheckpoint={int(pages)}")

    def checkpoint(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def transaction(self) -> "_Txn":
        return _Txn(self.conn)

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ---------------------------------------------------------------- cursor
    def get_cursor(self) -> Cursor:
        row = self.conn.execute(
            "SELECT last_chunk, last_event_id FROM ingest_cursor WHERE id=1").fetchone()
        return Cursor(*row) if row else Cursor(-1, 0)

    def set_cursor(self, last_chunk: int, last_event_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ingest_cursor(id, last_chunk, last_event_id) VALUES (1, ?, ?)",
            (last_chunk, last_event_id))

    # ---------------------------------------------------------------- configs
    def upsert_config_event(self, workflow_id: str, version: int, actor: str | None,
                            ts_minute: int | None, changed_dims: list[str] | None,
                            proposal_id: str | None, approval_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO config_versions(workflow_id, version, actor, ts_minute, changed_dims,"
            " proposal_id, approval_id) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(workflow_id, version) DO UPDATE SET actor=excluded.actor,"
            " ts_minute=excluded.ts_minute, changed_dims=excluded.changed_dims,"
            " proposal_id=excluded.proposal_id, approval_id=excluded.approval_id",
            (workflow_id, version, actor, ts_minute, json.dumps(changed_dims),
             proposal_id, approval_id))

    def store_config_body(self, workflow_id: str, version: int, actor: str | None,
                          body: dict) -> None:
        """Record a mirrored body; keeps any event metadata already present."""
        text = json.dumps(body, sort_keys=True)
        self.conn.execute(
            "INSERT INTO config_versions(workflow_id, version, actor, body) VALUES (?,?,?,?) "
            "ON CONFLICT(workflow_id, version) DO UPDATE SET body=excluded.body,"
            " actor=COALESCE(config_versions.actor, excluded.actor)",
            (workflow_id, version, actor, text))
        self.conn.execute(
            "INSERT OR IGNORE INTO cohort_defs(workflow_id, version, cohorts) VALUES (?,?,?)",
            (workflow_id, version, json.dumps(body.get("cohorts", []))))

    def config_body(self, workflow_id: str, version: int) -> dict | None:
        row = self.conn.execute(
            "SELECT body FROM config_versions WHERE workflow_id=? AND version=?",
            (workflow_id, version)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def has_config_body(self, workflow_id: str, version: int) -> bool:
        return self.config_body(workflow_id, version) is not None

    # ---------------------------------------------------------------- jobs
    def get_job(self, name: str, workflow_id: str = "") -> int | None:
        row = self.conn.execute("SELECT last_run_minute FROM jobs WHERE name=? AND workflow_id=?",
                                (name, workflow_id)).fetchone()
        return row[0] if row else None

    def set_job(self, name: str, workflow_id: str, minute: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO jobs(name, workflow_id, last_run_minute)"
                          " VALUES (?,?,?)", (name, workflow_id, minute))

    # ---------------------------------------------------------------- audit
    def add_decision(self, sim_minute: int, kind: str, summary: str,
                     workflow_id: str | None = None, inputs: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO decisions(sim_minute, workflow_id, kind, summary, inputs) VALUES (?,?,?,?,?)",
            (sim_minute, workflow_id, kind, summary,
             json.dumps(inputs, sort_keys=True) if inputs is not None else None))

    def add_alert(self, sim_minute: int | None, severity: str, code: str, message: str,
                  workflow_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO alerts(sim_minute, workflow_id, severity, code, message) VALUES (?,?,?,?,?)",
            (sim_minute, workflow_id, severity, code, message))

    def add_alerts(self, rows: Iterable[tuple]) -> None:
        self.conn.executemany(
            "INSERT INTO alerts(sim_minute, workflow_id, severity, code, message) VALUES (?,?,?,?,?)",
            list(rows))

    def add_metric(self, sim_minute: int, events: int, ingest_s: float, lag_s: float,
                   tick_s: float, step: int) -> None:
        self.conn.execute(
            "INSERT INTO metrics(sim_minute, events_ingested, ingest_wall_s, lag_s,"
            " tick_wall_s, step_minutes) VALUES (?,?,?,?,?,?)",
            (sim_minute, events, ingest_s, lag_s, tick_s, step))

    def last_metric(self) -> dict | None:
        cur = self.conn.execute("SELECT * FROM metrics ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def count(self, table: str) -> int:
        if not table.isidentifier():
            raise ValueError(table)
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class _Txn:
    """``with store.transaction():`` -> BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self._conn.execute("ROLLBACK")
            return None
        try:
            self._conn.execute("COMMIT")
        except Exception:
            try:  # leave the connection usable; the original COMMIT error propagates
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                log.exception("ROLLBACK after failed COMMIT also failed")
            raise
        return None
