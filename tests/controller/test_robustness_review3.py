"""Review 3 robustness fixes: tick-step isolation, short-ttl routine filing, apply-window reachability,
refile cooldown after repeated rejections, wrapped windows, vanished approvals, tenant-keyed soft caps."""
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sim.server
from controller import client as c
from controller import fatigue, settings
from controller import proposal_repo as pr
from controller.client import SimClient
from controller.loop import Controller, Windows
from controller.planner import Planner
from controller.store import Store
from sim.scenario import load_scenario
from sim.world import World
from tests.controller.helpers import make_controller, proposal_states, uniform_hours

ROOT = Path(__file__).resolve().parents[2]
NW_RENEWAL = {"approver_hours": [10, 19], "approver_latency_minutes": [30, 240], "proposal_ttl_hours": 24,
              "max_requests_per_24h": 3}
NW_KYC = {"approver_hours": [10, 18], "approver_latency_minutes": [60, 480], "proposal_ttl_hours": 24,
          "max_requests_per_24h": 2}
KESTREL = {"approver_hours": [10, 19], "approver_latency_minutes": [20, 120], "proposal_ttl_hours": 12,
           "max_requests_per_24h": 4}


def at(day, hh, mm=0):
    return day * 1440 + hh * 60 + mm


def _short_world(tmp_path, days=2, mutate=None, **top):
    sc = load_scenario(str(ROOT / "scenarios" / "public_02_incidents.yaml"))
    sc.update(days=days, history_days=2, incidents=[], credits=300)
    sc.update(top)
    sc["tenants"] = [t for t in sc["tenants"] if t["workflows"][0]["id"] == "nw_renewal"]
    for t in sc["tenants"]:
        for w in t["workflows"]:
            w["dials_per_hour"] = 60
            w["daily_batch"]["size"] = 300
            if mutate:
                mutate(w)
    world = World(copy.deepcopy(sc), tmp_path / "run")
    sim.server._world = world
    return world


# ---------------------------------------------------------------- 1. tick step isolation
def test_a_raising_planner_does_not_stop_the_loop_and_is_alerted(tmp_path, monkeypatch):
    _short_world(tmp_path)

    def boom(self, now):
        raise KeyError("unusual hidden shape")

    monkeypatch.setattr(Planner, "plan", boom)
    with TestClient(sim.server.app) as http:
        store = Store(tmp_path / "state" / "t.db")
        try:
            ctl = Controller(SimClient(http=http, sleep=lambda s: None), store)
            result = ctl.run()
            assert result.finished
            rows = store.conn.execute("select message from alerts where code='tick_step_failed'").fetchall()
            assert rows and "plan_and_submit" in rows[0][0]
            assert len(rows) <= 2 + 1  # once per sim day, not per tick
        finally:
            store.close()


def test_a_raising_reconcile_and_apply_are_isolated_too(tmp_path, monkeypatch):
    _short_world(tmp_path)
    from controller.governance import Governance

    def boom(self, now):
        raise RuntimeError("sqlite exploded")

    monkeypatch.setattr(Governance, "reconcile", boom)
    monkeypatch.setattr(Governance, "apply_approved", boom)
    with TestClient(sim.server.app) as http:
        store = Store(tmp_path / "state" / "t.db")
        try:
            ctl = Controller(SimClient(http=http, sleep=lambda s: None), store)
            monkeypatch.setattr(Governance, "reconcile", lambda self, now: None)  # startup needs a clean one
            ctl.startup()
            monkeypatch.setattr(Governance, "reconcile", boom)
            for _ in range(2000):
                done, _ = ctl.tick()
                if done:
                    break
            assert done
            steps = {r[0].split(":")[0] for r in store.conn.execute(
                "select message from alerts where code='tick_step_failed'")}
            assert {"reconcile", "apply_approved"} <= steps
        finally:
            store.close()


# ---------------------------------------------------------------- 2. routine at short ttl
def test_routine_files_at_the_days_best_minutes_when_ttl_is_short():
    gov = {**NW_RENEWAL, "proposal_ttl_hours": 3}
    best = fatigue.best_daily_probability(gov)
    assert settings.FILE_MIN_P_ROUTINE_FLOOR <= best < settings.FILE_MIN_P_ROUTINE
    lo, hi = gov["approver_hours"]
    minutes = range(at(1, lo), at(1, hi), 15)
    filed = [m for m in minutes if fatigue.filing_window_ok(m, gov, True, "x")[0]]
    assert filed
    assert all(fatigue.decision_probability(m, gov) >= 0.95 * best for m in filed)


def test_routine_never_files_when_best_probability_is_below_the_routine_floor():
    gov = {**NW_KYC, "proposal_ttl_hours": 2}
    assert fatigue.best_daily_probability(gov) < settings.FILE_MIN_P_ROUTINE_FLOOR
    lo, hi = gov["approver_hours"]
    assert not any(fatigue.filing_window_ok(m, gov, True, "x")[0] for m in range(at(1, lo), at(1, hi), 15))


# ---------------------------------------------------------------- 3. apply window on the tick grid
def test_decision_probability_includes_the_chance_the_tick_sees_the_decision_in_time():
    gov = {**NW_RENEWAL, "apply_window_hours": 0.5}
    full = fatigue.decision_probability(at(1, 10), NW_RENEWAL)
    assert full == 1.0
    p = fatigue.decision_probability(at(1, 10), gov, tick=60)
    assert p == pytest.approx(31 / 60)  # delays 0..30 of 0..59 minutes land inside the window
    assert fatigue.decision_probability(at(1, 10), gov, tick=15) == 1.0  # window >= tick: inert


def test_window_zero_makes_the_approval_route_unreachable_on_a_coarse_tick():
    gov = {**NW_RENEWAL, "apply_window_hours": 0}
    assert fatigue.decision_probability(at(1, 10), gov, tick=60) == pytest.approx(1 / 60)
    assert fatigue.apply_window_unreachable(gov, 60)
    assert not fatigue.apply_window_unreachable({**NW_RENEWAL, "apply_window_hours": 2}, 15)
    assert not any(fatigue.filing_window_ok(m, gov, False, "x", tick=60)[0] for m in range(at(1, 10), at(1, 19), 15))


def test_unreachable_apply_window_raises_one_alert_and_files_nothing(tmp_path):
    world = _short_world(tmp_path, days=3, tick_minutes=60,
                         mutate=lambda w: w["governance"].update(apply_window_hours=0))
    with TestClient(sim.server.app) as http:
        store = Store(tmp_path / "state" / "t.db")
        try:
            ctl = make_controller(SimClient(http=http, sleep=lambda s: None), store, uniform_hours())
            for _ in range(200):
                done, _ = ctl.tick()
                if done:
                    break
            assert not world.approvals
            n = store.conn.execute("select count(*) from alerts where code='apply_window_unreachable'").fetchone()[0]
            assert 1 <= n <= 3
        finally:
            store.close()


# ---------------------------------------------------------------- 4. refile cooldown after rejections
def test_repeated_rejections_are_not_refiled_every_tick(tmp_path):
    world = _short_world(tmp_path, days=3, mutate=lambda w: w["governance"].update(base_reject_rate=1.0))
    from controller.desired import Priority
    with TestClient(sim.server.app) as http:
        store = Store(tmp_path / "state" / "t.db")
        try:
            ctl = make_controller(SimClient(http=http, sleep=lambda s: None), store,
                                  uniform_hours(kind="incident", priority=Priority.INCIDENT))
            for _ in range(300):
                done, _ = ctl.tick()
                if done:
                    break
            aps = sorted(world.approvals.values(), key=lambda a: a["created_minute"])
            assert len(aps) >= 2
            assert aps[1]["created_minute"] - aps[0]["decide_minute"] <= 30  # a first rejection is refiled promptly
            if len(aps) >= 3:
                assert aps[2]["created_minute"] >= aps[1]["decide_minute"] + settings.REFILE_AFTER_REJECT_MIN
        finally:
            store.close()


# ---------------------------------------------------------------- 5. wrapped windows
def test_active_at_handles_a_wrapped_approver_window():
    wfs = [{"workflow_id": "w", "dialling_window": [9, 21], "governance": {"approver_hours": [22, 6]}}]
    w = Windows.from_workflows(wfs)
    assert w.active_at(at(0, 23)) and w.active_at(at(0, 2))
    assert w.active_at(at(0, 12))
    assert not w.active_at(at(0, 7))


# ---------------------------------------------------------------- 6. vanished approval
def test_a_vanished_approval_alerts_once_per_day_and_expires_the_row(tmp_path, monkeypatch):
    _short_world(tmp_path, days=4)
    with TestClient(sim.server.app) as http:
        store = Store(tmp_path / "state" / "t.db")
        try:
            client = SimClient(http=http, sleep=lambda s: None)
            ctl = make_controller(client, store, uniform_hours())
            for _ in range(400):
                done, _ = ctl.tick()
                if proposal_states(store) or done:
                    break
            pid = "nw_renewal:1:daily_bundle"
            assert proposal_states(store).get(pid) == pr.PENDING

            def missing(aid):
                raise c.NotFound(404, "not_found", aid)

            monkeypatch.setattr(client, "get_approval", missing)
            for _ in range(600):
                done, _ = ctl.tick()
                if proposal_states(store)[pid] == pr.EXPIRED or done:
                    break
            assert proposal_states(store)[pid] == pr.EXPIRED
            n = store.conn.execute("select count(*) from alerts where code='approval_missing'").fetchone()[0]
            assert 1 <= n <= 3
        finally:
            store.close()


# ---------------------------------------------------------------- 7. tenant-keyed soft caps
def test_soft_cap_applies_to_every_workflow_of_the_kestrel_tenant():
    ok, why = fatigue.filing_window_ok(at(0, 10, 30), KESTREL, True, "ks_other", tenant_id="kestrel_finance")
    assert not ok and "11:00" in why
    assert fatigue.filing_window_ok(at(0, 10, 30), KESTREL, True, "ks_other", tenant_id="other_tenant")[0]
    assert fatigue.routine_budget(KESTREL, "ks_other", tenant_id="kestrel_finance") == 2
    assert fatigue.routine_budget({**KESTREL, "max_requests_per_24h": 2}, "ks_other",
                                  tenant_id="kestrel_finance") == 1
    assert fatigue.slot_limit({**KESTREL, "max_requests_per_24h": 5}, 2, "ks_other",
                              tenant_id="kestrel_finance") == 2
    assert fatigue.soft_cap_ignored(KESTREL, "ks_other", tenant_id="kestrel_finance") is None
    assert settings.SOFT_CAPS_BY_TENANT["kestrel_finance"] == settings.SOFT_CAPS["ks_cancel"]
