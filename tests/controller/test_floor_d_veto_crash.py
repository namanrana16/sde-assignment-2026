"""D4: a crashing apply-time veto hook must not consume an approval; a real veto still does."""
import pytest

import sim.server
from controller import proposal_repo as pr
from controller import settings
from controller.desired import PlanContext, Registry, VetoCrash
from sim.scenario import load_scenario
from sim.world import World
from tests.controller.conftest import ROOT
from tests.controller.helpers import make_controller, proposal_states, run_until, sim_kinds, uniform_hours
PID = "nw_renewal:1:daily_bundle"
DONE = (pr.APPLIED, pr.MONITORING, pr.SUPERSEDED, pr.LAPSED, pr.STALE, pr.REJECTED)


@pytest.fixture()
def world(tmp_path):
    sc = load_scenario(str(ROOT / "scenarios" / "public_01_dev.yaml"))
    sc["days"], sc["history_days"] = 2, 2
    for t in sc["tenants"]:
        for w in t["workflows"]:
            w["governance"]["base_reject_rate"] = 0.0
    w = World(sc, tmp_path / "run")
    sim.server._world = w
    return w


def test_registry_marks_a_crash_but_not_a_real_veto(monkeypatch):
    reg = Registry()
    ctx = PlanContext(None, None, {}, 0)

    def boom(c, w, k):
        raise RuntimeError("x")

    reg.register_veto(lambda c, w, k: "stale")
    assert not isinstance(reg.veto(ctx, "w", "daily_bundle"), VetoCrash)
    reg.clear()
    reg.register_veto(boom)
    monkeypatch.setattr("controller.desired._report_failure", lambda *a, **k: None)  # no store here
    assert isinstance(reg.veto(ctx, "w", "daily_bundle"), VetoCrash)


def _run(ctl, store):
    run_until(ctl, lambda: proposal_states(store).get(PID) in DONE, limit=2000)
    return proposal_states(store)[PID]


def test_veto_crash_twice_then_recovery_still_applies(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    calls = []

    def flaky(row, now):
        calls.append(now)
        return VetoCrash("hook crashed") if len(calls) <= 2 else None

    ctl.gov.veto_check = flaky
    assert _run(ctl, store) in (pr.APPLIED, pr.MONITORING)
    assert sim_kinds(world).count("approval_applied") == 1


def test_veto_crash_beyond_the_retry_cap_withholds(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    calls = []

    def always(row, now):
        calls.append(now)
        return VetoCrash("hook crashed")

    ctl.gov.veto_check = always
    assert _run(ctl, store) == pr.SUPERSEDED
    assert len(calls) == settings.VETO_CRASH_MAX_RETRIES + 1
    assert "approval_applied" not in sim_kinds(world)


def test_a_real_veto_supersedes_immediately(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    calls = []
    ctl.gov.veto_check = lambda row, now: calls.append(now) or "evidence went stale"
    assert _run(ctl, store) == pr.SUPERSEDED and len(calls) == 1
