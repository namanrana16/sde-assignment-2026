"""C2 + C4a against the real sim: the Day-0 bundle (cohorts + per-cohort tables) goes through one approval."""
import json

import pytest

from controller import settings
from controller.desired import Registry
from controller import schedule
from controller.detectors import did_data
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client, when


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_SCHEDULE", True)
    world = build(tmp_path, ("nw_kyc",), days=4, history_days=8)
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "t.db")
    reg = Registry()
    schedule.register(reg)
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


def test_day0_bundle_defines_cohorts_and_complete_tables_in_one_approval(env):
    world, client, store, ctl = env
    run_to(ctl, client, when(1, 20))
    rows = store.conn.execute("select proposal_id, state, kind from proposals where workflow_id='nw_kyc'").fetchall()
    bundles = [r for r in rows if r[2] == "schedule_bundle"]
    assert len(bundles) == 1 and bundles[0][1] in ("APPLIED", "MONITORING", "CLOSED", "PENDING", "APPROVED", "REJECTED")
    body = json.loads(store.conn.execute("select body from proposals where kind='schedule_bundle'").fetchone()[0])
    cfg = body["request"]["proposed_config"]
    assert body["dims"] == ["cohorts", "schedule"]
    ids = [c["id"] for c in cfg["cohorts"]]
    assert 0 < len(ids) <= 20
    lo, hi = 10, 20
    tables = cfg["schedule"]["hour_weights"]
    assert set(tables) == set(ids) | {"default"}
    for t in tables.values():  # every in-window hour present in every table, never 0, clipped
        assert set(t) == {str(h) for h in range(lo, hi)} and all(0.3 <= w <= 3.0 for w in t.values())
    assert "schedule_guardrail" in body["request"]["dry_run"]
    why = store.conn.execute("select inputs from decisions where kind='schedule_plan'").fetchone()
    assert "estimate" in json.loads(why[0]) and store.conn.execute(
        "select 1 from decisions where kind='cohort_partition'").fetchone()


def test_applied_cohorts_match_the_sim_cohort_ids(env):
    world, client, store, ctl = env
    run_to(ctl, client, when(3, 12))
    assert store.conn.execute("select count(*) from alerts where code='cohort_mismatch'").fetchone()[0] == 0
    sim_ids = {r[0] for r in store.conn.execute("select distinct cohort_id from calls where day>=2")}
    live = {c["id"] for c in client.get_config("nw_kyc")["config"]["cohorts"]}
    assert sim_ids <= live | {"default"}


def test_cold_start_builds_cohorts_from_day0_data_and_proposes_on_day1(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_SCHEDULE", True)
    world = build(tmp_path, ("nw_kyc",), days=3, history_days=0)
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "cold.db")
    try:
        reg = Registry()
        schedule.register(reg)
        ctl = Controller(client, store, registry=reg)
        ctl.startup()
        run_to(ctl, client, when(1, 0))
        assert store.conn.execute("select count(*) from decisions where kind='cohort_partition'").fetchone()[0] == 0
        assert store.conn.execute("select count(*) from proposals").fetchone()[0] == 0  # nothing on Day 0
        run_to(ctl, client, when(1, 12))
        row = store.conn.execute("select sim_minute from decisions where kind='cohort_partition'").fetchone()
        assert row is not None and row[0] >= when(1, 0)  # built from Day-0 data, on Day 1
    finally:
        store.close()
        http.__exit__(None, None, None)
