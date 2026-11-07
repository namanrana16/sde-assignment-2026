"""DID detector + producer against the real sim: a did_flagged incident is detected and evacuated."""
import json

import pytest

from controller import settings
from controller.desired import Registry
from controller.detectors import did, did_data
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client, when

DAY = 1440
BAD_KINDS = {"apply_conflict", "approval_lapsed", "auto_apply_rejected"}


@pytest.fixture()
def flagged_world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DID", True)
    world = build(tmp_path, ("ks_cancel",), days=4, history_days=6,
                  incidents=[{"type": "did_flagged", "workflow": "ks_cancel", "did": "D2",
                              "at": "D2 10:00", "at_minute": when(2, 10), "base_health": 0.35}])
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "t.db")
    reg = Registry()
    did.register(reg)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    did_data.forget()
    yield world, client, store, ctl
    store.close()
    http.__exit__(None, None, None)


def run_to(ctl, client, minute):
    while client.state()["sim_minute"] < minute:
        done, _ = ctl.tick()
        if done:
            break


def test_flagged_did_is_detected_and_evacuated_without_hygiene_errors(flagged_world):
    world, client, store, ctl = flagged_world
    run_to(ctl, client, when(2, 9))
    flagged_before = store.conn.execute("select count(*) from decisions where kind='did_flagged'").fetchone()[0]
    assert flagged_before == 0  # no false flag on a healthy fleet during history + day 0-1
    run_to(ctl, client, when(2, 14))
    row = store.conn.execute("select sim_minute, inputs from decisions where kind='did_flagged'").fetchone()
    assert row is not None, "D2 never flagged"
    assert when(2, 10) <= row[0] <= when(2, 13, 0)  # within ~3 hours of onset
    step = json.loads(row[1])["step"]
    assert step["did"] == "D2" and step["p"] < 0.001 and step["drop"] > settings.DID_FLAG_MIN_DROP
    cfg = client.get_config("ks_cancel")["config"]
    assert cfg["allocations"]["default"]["caller_id"].get("D2", 0.0) == pytest.approx(0.0)
    run_to(ctl, client, when(3, 12))  # observed share must wash out of the trailing window before it closes
    inc = store.conn.execute("select state, kind from incidents").fetchall()
    assert ("closed", "did_flagged") in inc
    # steps respected max_shift 0.2 and the policy: no graded refusals/conflicts/lapses
    kinds = [json.loads(l)["kind"] for l in open(world.run_dir / "sim_log.jsonl")]
    assert not (BAD_KINDS & set(kinds))
    # every evaluation left a decisions row with its inputs
    ev = store.conn.execute("select inputs from decisions where kind='did_eval' order by id desc limit 1").fetchone()
    assert "curve" in json.loads(ev[0]) and "status" in json.loads(ev[0])
    # no flagged DID is ever probed again within the run
    run_to(ctl, client, when(3, 20))
    cfg = client.get_config("ks_cancel")["config"]
    assert cfg["allocations"]["default"]["caller_id"].get("D2", 0.0) == pytest.approx(0.0)


def test_healthy_fleet_probes_the_unused_did_and_spreads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DID", True)
    world = build(tmp_path, ("ks_cancel",), days=3, history_days=6)
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "h.db")
    reg = Registry()
    did.register(reg)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    did_data.forget()
    run_to(ctl, client, when(0, 6))
    alloc = client.get_config("ks_cancel")["config"]["allocations"]["default"]["caller_id"]
    assert 0 < alloc.get("D3", 0) <= 0.05  # probe at the cap, not more
    run_to(ctl, client, when(2, 6))
    alloc = client.get_config("ks_cancel")["config"]["allocations"]["default"]["caller_id"]
    assert all(v == pytest.approx(1 / 3, abs=0.02) for v in alloc.values()) and len(alloc) == 3
    assert store.conn.execute("select count(*) from decisions where kind='did_flagged'").fetchone()[0] == 0
    kinds = [json.loads(l)["kind"] for l in open(world.run_dir / "sim_log.jsonl")]
    assert not (BAD_KINDS & set(kinds))
    store.close()
    http.__exit__(None, None, None)


def test_persistently_weak_did_is_never_ramped_and_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DID", True)
    world = build(tmp_path, ("ks_cancel",), days=5, history_days=6, did_base_health={"D3": 0.4})
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "w.db")
    reg = Registry()
    did.register(reg)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    did_data.forget()
    peak = 0.0
    try:
        while client.state()["sim_minute"] < when(4, 20):
            ctl.tick()
            peak = max(peak, client.get_config("ks_cancel")["config"]["allocations"]["default"]["caller_id"].get("D3", 0.0))
        assert peak <= 0.06  # probe cap only: a weak base never earns more traffic
        weak = store.conn.execute("select inputs from decisions where kind='did_weak_base'").fetchall()
        assert weak, "weak base never recognised"
        assert client.get_config("ks_cancel")["config"]["allocations"]["default"]["caller_id"].get("D3", 0.0) == 0.0
        assert store.conn.execute("select count(*) from decisions where kind='did_flagged'").fetchone()[0] == 0
    finally:
        store.close()
        http.__exit__(None, None, None)


def test_cold_start_without_history_makes_no_changes_until_data_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DID", True)
    world = build(tmp_path, ("ks_cancel",), days=3, history_days=0)
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "c.db")
    reg = Registry()
    did.register(reg)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    did_data.forget()
    try:
        run_to(ctl, client, when(0, 8))
        assert client.get_config("ks_cancel")["version"] == 1  # nothing known yet: nothing changed
        run_to(ctl, client, when(2, 0))
        assert store.conn.execute("select count(*) from alerts where severity='error'").fetchone()[0] == 0
    finally:
        store.close()
        http.__exit__(None, None, None)
