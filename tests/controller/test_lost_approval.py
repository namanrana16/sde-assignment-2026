"""A1: an approval the sim still shows as ``approved`` was never applied: retry, never abandon."""
import pytest

import sim.server
from controller import client as c
from controller import faults
from controller import proposal_repo as pr
from tests.controller.helpers import (SimulatedCrash, arm_crash, disarm_crash, make_controller,
                                      proposal_states, run_until, sim_kinds, uniform_hours)
from tests.controller.conftest import ROOT
from tests.controller.test_crash_recovery import BAD, restart
from sim.scenario import load_scenario
from sim.world import World

PID = "nw_renewal:1:daily_bundle"


@pytest.fixture()
def world(tmp_path):
    """public_01 with the approver's judgement-call rejection switched off: no seed can reject PID."""
    sc = load_scenario(str(ROOT / "scenarios" / "public_01_dev.yaml"))
    sc["days"], sc["history_days"] = 2, 2
    for t in sc["tenants"]:
        for w in t["workflows"]:
            w["governance"]["base_reject_rate"] = 0.0
    w = World(sc, tmp_path / "run")
    sim.server._world = w
    return w


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    disarm_crash(monkeypatch)
    yield
    disarm_crash(monkeypatch)


def test_before_apply_request_is_a_fault_point():
    assert "before-apply-request" in faults.POINTS


def test_restart_after_apply_intent_without_post_applies_exactly_once(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    arm_crash(monkeypatch, "before-apply-request:1")
    try:
        run_until(ctl, lambda: False)
    except SimulatedCrash:
        pass
    else:
        pytest.fail("the before-apply-request crash point never fired")
    (a,) = world.approvals.values()
    assert a["status"] == "approved"  # the POST never reached the sim
    disarm_crash(monkeypatch)
    ctl2, store2 = restart(client, store, uniform_hours())
    assert ctl2.gov.repo.get(PID).state == pr.APPROVED  # NOT marked stale by the reconcile
    assert run_until(ctl2, lambda: proposal_states(store2)[PID] in (pr.APPLIED, pr.MONITORING))
    assert sim_kinds(world).count("approval_applied") == 1
    assert client.get_config("nw_renewal")["version"] == 2
    assert not BAD & set(sim_kinds(world))


def test_outcome_unknown_with_sim_never_receiving_it_is_applied_on_retry(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    real, calls = client.apply_approval, []

    def lost(wid, aid):
        calls.append(aid)
        if len(calls) == 1:
            raise c.OutcomeUnknown("POST", "/apply", "lost")
        return real(wid, aid)

    monkeypatch.setattr(client, "apply_approval", lost)
    assert run_until(ctl, lambda: proposal_states(store).get(PID) in (pr.APPLIED, pr.MONITORING, pr.REJECTED, pr.STALE,
                                                                  pr.LAPSED))
    assert len(calls) >= 2 and proposal_states(store).get(PID) in (pr.APPLIED, pr.MONITORING)
    assert sim_kinds(world).count("approval_applied") == 1


def test_approved_but_past_apply_by_never_calls_apply(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    calls = []

    def lost(wid, aid):
        calls.append(aid)
        raise c.OutcomeUnknown("POST", "/apply", "lost")

    monkeypatch.setattr(client, "apply_approval", lost)
    assert run_until(ctl, lambda: proposal_states(store).get(PID) in (pr.LAPSED, pr.REJECTED, pr.STALE),
                     limit=2000)
    assert proposal_states(store)[PID] == pr.LAPSED
    n = len(calls)
    assert 1 <= n < 40
    run_until(ctl, lambda: False, limit=20)
    assert len(calls) == n, "/apply called again after apply_by passed"  # spy: 0 calls after lapse
    assert "approval_lapsed" not in sim_kinds(world)
