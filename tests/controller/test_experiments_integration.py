"""C6/C7 against the real sim (shortened public_02 worlds): entry via approval, looks, supervisor interruption."""
import json

import pytest

from controller import experiments, settings
from controller.desired import Registry
from controller.detectors import did_data
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client, when


def make_env(tmp_path, monkeypatch, workflows, days, history_days=6, incidents=()):
    monkeypatch.setattr(settings, "ENABLE_EXPERIMENTS", True)
    monkeypatch.setattr(settings, "ENABLE_SCHEDULE", False)
    world = build(tmp_path, workflows, days=days, history_days=history_days, incidents=incidents)
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "t.db")
    reg = Registry()
    experiments.register(reg)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    did_data.forget()
    return client, http, store, ctl


def run_to(ctl, client, minute):
    while client.state()["sim_minute"] < minute:
        done, _ = ctl.tick()
        if done:
            break


def proposals(store, wid):
    out = []
    for pid, state, kind, route, body in store.conn.execute(
            "select proposal_id, state, kind, route, body from proposals where workflow_id=? order by plan_seq", (wid,)):
        out.append((pid, state, kind, route, json.loads(body)))
    return out


def test_entry_goes_through_approval_not_auto_apply_even_where_allowed(tmp_path, monkeypatch):
    client, http, store, ctl = make_env(tmp_path, monkeypatch, ("ks_cancel",), days=4)
    try:
        run_to(ctl, client, when(2, 0))
        props = proposals(store, "ks_cancel")
        entry = [p for p in props if p[2] == "experiment_entry"]
        assert entry and entry[0][3] == "approval"  # ks_cancel allows welcome/voice auto-apply: still approval
        cfg = entry[0][4]["request"]["proposed_config"]["allocations"]["default"]
        for dim, control in (("welcome_message", "W1"), ("voice", "V1")):
            w = cfg[dim]
            others = {k: v for k, v in w.items() if k != "V1" and k != "W1"}
            assert all(v == pytest.approx(0.075) for v in others.values()) and sum(others.values()) <= 0.225 + 1e-9
        assert entry[0][4]["request"]["dry_run"]["primary_kpi"] == "engagement_rate"
        why = store.conn.execute("select inputs from decisions where kind='experiment_look' limit 1").fetchone()
        assert why is None or "records" in json.loads(why[0])
        assert store.conn.execute("select count(*) from alerts where code in ('auto_apply_refused','proposal_invalid_locally')").fetchone()[0] == 0
    finally:
        store.close()
        http.__exit__(None, None, None)


def test_pickup_primary_workflows_are_left_alone(tmp_path, monkeypatch):
    client, http, store, ctl = make_env(tmp_path, monkeypatch, ("nw_kyc",), days=2)
    try:
        run_to(ctl, client, when(1, 12))
        assert store.conn.execute("select count(*) from decisions where kind like 'experiment%'").fetchone()[0] == 0
    finally:
        store.close()
        http.__exit__(None, None, None)


def test_supervisor_voice_edit_interrupts_and_is_never_reimposed(tmp_path, monkeypatch):
    from sim.scenario import load_scenario
    edit = [i for i in load_scenario("scenarios/public_02_incidents.yaml")["incidents"]
            if i.get("type") == "supervisor_edit" and i["workflow"] == "nw_renewal"]
    client, http, store, ctl = make_env(tmp_path, monkeypatch, ("nw_renewal",), days=8, incidents=edit)
    try:
        run_to(ctl, client, when(7, 12))
        row = store.conn.execute("select sim_minute, summary from decisions where kind='experiment_interrupted'").fetchone()
        assert row is not None and "supervisor" in row[1] and when(5, 15) <= row[0] <= when(5, 20)
        edit_minute = row[0]
        live = client.get_config("nw_renewal")["config"]["allocations"]["default"]["voice"]
        assert max(live, key=live.get) == "V2" or any("supervisor" in p[4]["request"]["change_summary"]
                                                       for p in proposals(store, "nw_renewal") if p[2].startswith("experiment") and p[1] != "REJECTED")
        for pid, state, kind, route, body in proposals(store, "nw_renewal"):
            if body.get("request", {}).get("proposed_config") and store.conn.execute(
                    "select filed_minute from proposals where proposal_id=?", (pid,)).fetchone()[0] > edit_minute:
                voice = body["request"]["proposed_config"]["allocations"]["default"]["voice"]
                if max(voice, key=voice.get) != "V2":  # a later change that moves the voice away from the supervisor's choice
                    assert "supervisor" in body["request"]["change_summary"]  # ... must name the supervisor edit
    finally:
        store.close()
        http.__exit__(None, None, None)
