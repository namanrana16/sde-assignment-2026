"""World identity: which SQLite file belongs to the simulator we are talking to."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .client import SimClient
from .store import Store

log = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class WorldId:
    scenario: str
    fingerprint: str
    anchor_chunk: int | None  # chunk 0 (history or events), if any exist yet
    first_event_id: int | None = None
    until_ts: str | None = None
    line_hash: str | None = None  # hash of chunk 0's first line (history or events chunk)


def compute_fingerprint(scenario: str, first_line: str | None, first_event_id: int | None,
                        last_event_id: int | None) -> str:
    """scenario + hash(first line of chunk 0) + its event-id range."""
    line_hash = hashlib.sha256((first_line or "").encode()).hexdigest()[:16] if first_line else "nohist"
    raw = f"{scenario}|{line_hash}|{first_event_id}|{last_event_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def identify_world(client: SimClient) -> WorldId:
    """Chunk numbering is global, so chunk 0 is the first history chunk, or, in a
    no-history world, the first events chunk. Its first line is hashed either way."""
    state = client.state()
    chunks = client.list_chunks("history") + client.list_chunks("events")
    if not chunks:
        return WorldId(state["scenario"], compute_fingerprint(state["scenario"], None, None, None), None)
    first = min(chunks, key=lambda c: c["chunk"])
    line = client.first_line(first["chunk"])
    line_hash = hashlib.sha256(line.encode()).hexdigest()[:16] if line else None
    if first.get("kind") == "history":
        fp = compute_fingerprint(state["scenario"], line, first["first_event_id"], first["last_event_id"])
    else:
        # No-history world: the DB name must be the same before and after the first events
        # chunk exists, so the hash is verified through meta (``regressed``) instead.
        fp = compute_fingerprint(state["scenario"], None, None, None)
    return WorldId(state["scenario"], fp, first["chunk"], first["first_event_id"],
                   first.get("until_ts"), line_hash)


def db_path(state_dir: str | Path, world: WorldId) -> Path:
    return Path(state_dir) / f"{_SAFE.sub('_', world.scenario)}-{world.fingerprint[:8]}.db"


def regressed(store: Store, state: dict, world: WorldId) -> bool:
    """True if the sim is provably not the world we stored (it restarted)."""
    if store.get_cursor().last_event_id > state["last_event_id"]:
        return True
    stored_minute, stored_chunks = store.get_meta("sim_minute"), store.get_meta("chunk_count")
    if stored_minute is not None and state["sim_minute"] < int(stored_minute):
        return True
    if stored_chunks is not None and state["chunks"] < int(stored_chunks):
        return True
    first_id, until = store.get_meta("chunk0_first_event_id"), store.get_meta("chunk0_until_ts")
    if first_id not in (None, "") and world.first_event_id is not None             and int(first_id) != world.first_event_id:
        return True
    if bool(until) and world.until_ts is not None and until != world.until_ts:
        return True
    stored_hash = store.get_meta("chunk0_line_hash")
    return bool(stored_hash) and world.line_hash is not None and stored_hash != world.line_hash


def note_progress(store: Store, state: dict, world: WorldId | None = None) -> None:
    """Persist the monotone markers ``regressed`` compares against."""
    store.set_meta("sim_minute", str(state["sim_minute"]))
    store.set_meta("chunk_count", str(state["chunks"]))
    if world is not None and world.first_event_id is not None:
        store.set_meta("chunk0_first_event_id", str(world.first_event_id))
        store.set_meta("chunk0_until_ts", world.until_ts or "")
        store.set_meta("chunk0_line_hash", world.line_hash or "")


def open_world_store(client: SimClient, state_dir: str | Path) -> tuple[Store, WorldId, bool]:
    """Open (or create) the store for this world.

    Returns ``(store, world, fresh)``. Any regression (sim_minute or chunk count lower
    than stored, last_event_id behind our cursor, chunk 0 different) means the simulator
    restarted as a new world: the stale DB is archived and a new one created.
    """
    world = identify_world(client)
    path = db_path(state_dir, world)
    store = Store(path)
    state = client.state()
    if regressed(store, state, world):
        log.warning("sim state regressed vs stored world; archiving %s", path)
        store.close()
        _archive(path)
        store = Store(path)
    fresh = store.get_meta("fingerprint") is None
    if fresh:
        store.set_meta("fingerprint", world.fingerprint)
        store.set_meta("scenario", world.scenario)
    note_progress(store, state, world)
    return store, world, fresh


def _archive(path: Path) -> None:
    stamp = time.strftime("%Y%m%d%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            src.rename(Path(str(path) + f".{stamp}.old{suffix}"))
