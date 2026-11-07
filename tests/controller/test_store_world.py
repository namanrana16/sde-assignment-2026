import pytest
from fastapi.testclient import TestClient

import sim.server
from controller.client import SimClient
from controller.world_id import compute_fingerprint, db_path, identify_world, open_world_store
from tests.controller.conftest import build_world


def test_fingerprint_depends_on_all_parts():
    base = compute_fingerprint("s", "line", 1, 10)
    assert base == compute_fingerprint("s", "line", 1, 10)
    assert base != compute_fingerprint("s2", "line", 1, 10)
    assert base != compute_fingerprint("s", "other", 1, 10)
    assert base != compute_fingerprint("s", "line", 1, 11)
    assert compute_fingerprint("s", None, None, None) != base


def test_identify_and_db_path(client, tmp_path):
    w = identify_world(client)
    assert w.anchor_chunk is not None
    name = db_path(tmp_path, w).name
    assert name.startswith(w.scenario) and w.fingerprint[:8] in name


def test_different_world_opens_new_db(tmp_path):
    paths = []
    for history_days in (2, 3):
        build_world(tmp_path / f"w{history_days}", history_days=history_days)
        with TestClient(sim.server.app) as http:
            store, _, fresh = open_world_store(SimClient(http=http), tmp_path / "state")
            paths.append(store.path)
            assert fresh
            store.close()
    assert paths[0] != paths[1]


def test_no_history_world_has_stable_fingerprint(tmp_path):
    build_world(tmp_path / "w", history_days=0)
    with TestClient(sim.server.app) as http:
        w = identify_world(SimClient(http=http))
    assert w.anchor_chunk is None
    assert w.fingerprint == compute_fingerprint(w.scenario, None, None, None)


def test_restarted_sim_archives_stale_db(client, tmp_path):
    store, _, _ = open_world_store(client, tmp_path / "state")
    store.set_cursor(3, 10 ** 9)  # cursor ahead of the sim: sim must have restarted
    store.add_alert(0, "info", "marker", "m")
    path = store.path
    store.close()
    store2, _, fresh2 = open_world_store(client, tmp_path / "state")
    assert fresh2 and store2.get_cursor().last_event_id == 0 and store2.path == path
    assert store2.count("alerts") == 0
    assert list(path.parent.glob("*.old"))
    store2.close()


def test_reopen_existing_not_fresh(client, tmp_path):
    s, _, _ = open_world_store(client, tmp_path / "state")
    s.close()
    s2, _, fresh = open_world_store(client, tmp_path / "state")
    assert not fresh
    s2.close()


def test_store_helpers(store):
    assert store.get_meta("schema_version") == "1"
    assert store.get_cursor().last_event_id == 0
    store.set_cursor(2, 50)
    assert store.get_cursor().last_chunk == 2
    store.store_config_body("w", 1, "init", {"cohorts": [], "x": 1})
    store.upsert_config_event("w", 1, "supervisor", 5, ["voice"], None, None)
    assert store.config_body("w", 1)["x"] == 1 and store.has_config_body("w", 1)
    assert not store.has_config_body("w", 2)
    store.add_decision(1, "k", "s", "w", {"a": 1})
    store.add_alert(1, "warn", "c", "m")
    store.add_metric(1, 2, 0.1, 0.2, 0.3, 15)
    assert store.last_metric()["events_ingested"] == 2
    assert store.count("decisions") == 1
    with pytest.raises(ValueError):
        store.count("x; drop")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.add_alert(1, "warn", "rolled", "back")
            raise RuntimeError
    assert store.count("alerts") == 1


def test_all_spec_tables_exist(store):
    names = {r[0] for r in store.conn.execute("select name from sqlite_master where type='table'")}
    expected = ("meta ingest_cursor leads calls cube config_versions cohort_defs experiments arms "
                "proposals proposal_events decisions incidents validation_runs llm_calls jobs "
                "detector_state alerts metrics").split()
    assert set(expected) <= names
    assert store.conn.execute("pragma journal_mode").fetchone()[0] == "wal"
