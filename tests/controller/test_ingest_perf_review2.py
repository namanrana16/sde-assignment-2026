"""History fast path (no calls_lead index while ingesting history), crash safety, lead cache, tick observer phase."""
import pytest
import sim.server
from fastapi.testclient import TestClient
from sim.world import World

from controller import ingest as ingest_mod
from controller.client import SimClient
from controller.ingest import Ingestor, mirror_configs
from controller.loop import Controller
from controller.store import Store
from .conftest import build_world

SMALL_CHUNK = 4000


@pytest.fixture()
def small_chunk_client(tmp_path, monkeypatch):
    """A sim whose history is split into several small chunks (the real threshold is 250k events)."""
    orig = World.emit

    def emit(self, ev):
        orig(self, ev)
        if len(self._buf) >= SMALL_CHUNK:
            self._flush_chunk()

    monkeypatch.setattr(World, "emit", emit)
    build_world(tmp_path)
    with TestClient(sim.server.app) as http:
        yield SimClient(http=http, sleep=lambda s: None)


def has_index(store, name="calls_lead"):
    return store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                              (name,)).fetchone() is not None


def snapshot(store):
    c = store.conn
    return (c.execute("select count(*), sum(connected), sum(engaged), sum(lid) from calls").fetchone(),
            c.execute("select count(*), sum(dials), sum(connects), sum(engaged), sum(sum_adherence) from cube").fetchone(),
            c.execute("select count(*), sum(lid) from leads").fetchone(), store.get_cursor())


def full_run(client, tmp_path):
    st = Store(tmp_path / "ref" / "r.db")
    mirror_configs(client, st, [w["workflow_id"] for w in client.workflows()])
    Ingestor(client, st).sync()
    snap = snapshot(st)
    st.close()
    return snap


def test_fresh_store_has_no_index_until_history_done(client, store):
    assert not has_index(store)
    assert store.get_meta("calls_index_built") is None
    Ingestor(client, store).sync()
    assert has_index(store)
    assert store.get_meta("calls_index_built") == "1"


def test_restart_mid_history_is_exactly_once_and_builds_index(small_chunk_client, store, tmp_path):
    client = small_chunk_client
    ref = full_run(client, tmp_path)
    chunks = client.list_chunks("history")
    assert len(chunks) >= 2, "need several history chunks to crash in the middle"
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    ing = Ingestor(client, store)
    for meta in chunks[:2]:  # stop after k chunks: simulated crash
        ing.process_chunk(meta)
    path = store.path
    store.close()
    store = Store(path)  # restart
    assert not has_index(store), "reopening mid-history must not recreate the index"
    assert store.get_meta("calls_index_built") is None
    Ingestor(client, store).sync()
    assert has_index(store)
    assert store.get_meta("calls_index_built") == "1"
    assert snapshot(store) == ref
    store.close()
    store = Store(path)  # restart after completion keeps the index
    assert has_index(store)
    store.close()


def test_crash_after_last_history_chunk_before_index(client, store):
    chunks = client.list_chunks("history")
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    ing = Ingestor(client, store)
    for meta in chunks:
        ing.process_chunk(meta)
    path = store.path
    store.close()
    store = Store(path)
    assert not has_index(store)
    Ingestor(client, store).sync()
    assert has_index(store)
    store.close()


def test_events_never_ingested_without_index(client, store):
    seen = []
    orig = Ingestor.process_chunk

    def spy(self, meta):
        seen.append((meta["chunk"], has_index(self.store)))
        return orig(self, meta)

    while not client.advance(60)["new_chunks"]:
        pass
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    hist = {m["chunk"] for m in client.list_chunks("history")}
    Ingestor.process_chunk = spy
    try:
        Ingestor(client, store).sync()
    finally:
        Ingestor.process_chunk = orig
    live = [idx for chunk, idx in seen if chunk not in hist]
    assert live and all(live)
    assert not any(idx for chunk, idx in seen if chunk in hist)


def test_legacy_db_with_index_is_marked_built(tmp_path):
    p = tmp_path / "legacy.db"
    st = Store(p)
    st.build_calls_index()
    st.conn.execute("DELETE FROM meta WHERE key='calls_index_built'")
    st.close()
    st = Store(p)
    assert st.get_meta("calls_index_built") == "1" and has_index(st)
    st.close()


def test_autocheckpoint_restored_after_history(client, store):
    from controller import settings
    Ingestor(client, store).sync()
    assert store.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == settings.WAL_AUTOCHECKPOINT_PAGES


def test_lead_cache_evicts_half_not_everything(client, store, monkeypatch):
    monkeypatch.setattr(ingest_mod.settings, "LEAD_CACHE_MAX", 10)
    ing = Ingestor(client, store)
    for i in range(10):
        ing._remember_lead(f"L{i}", i, 0)
    ing._remember_lead("L10", 10, 0)
    assert len(ing._leads) == 6 and "L10" in ing._leads and "L9" in ing._leads
    assert "L0" not in ing._leads


def test_observer_phase_is_recorded(client, store):
    ctl = Controller(client, store, stop_at_minute=0)
    ctl.startup()
    ctl.tick()
    row = store.conn.execute("SELECT observer_s FROM tick_phases ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and row[0] is not None and row[0] >= 0
