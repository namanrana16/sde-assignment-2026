"""A7: a lost state dir must not collide with approvals the sim already holds."""
from controller import proposal_repo as pr
from controller.loop import Controller
from controller.store import Store
from tests.controller.helpers import make_controller, registry_of, run_until, uniform_hours

PID1 = "nw_renewal:1:daily_bundle"


def test_fresh_store_seeds_plan_seq_and_does_not_adopt_an_old_approval(client, store, world, tmp_path):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: len(world.approvals) == 1)
    assert {a["proposal_id"] for a in world.approvals.values()} == {PID1}
    store.close()
    fresh = Store(tmp_path / "lost" / "again.db")  # the state dir was lost
    ctl2 = Controller(client, fresh, registry=registry_of(uniform_hours(hours={str(h): 1 for h in range(10, 20)})))
    ctl2.startup()
    assert ctl2.gov.repo.next_plan_seq("nw_renewal") == 2
    run_until(ctl2, lambda: len(world.approvals) >= 2 or client.state()["sim_minute"] > 1400, limit=300)
    ids = [a["proposal_id"] for a in world.approvals.values()]
    assert len(ids) == len(set(ids))
    assert all(r[0] != PID1 for r in fresh.conn.execute("select proposal_id from proposals"))


def test_adopt_remote_refuses_an_approval_with_a_different_base_version(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: len(world.approvals) == 1)
    (a,) = world.approvals.values()
    gov = ctl.gov
    row = gov.repo.get(PID1)
    # pretend our row (INTENT) targets another base version than the sim's approval under the same id
    store.conn.execute("update proposals set state='INTENT', base_version=?, approval_id=NULL where proposal_id=?",
                       (a["base_version"] + 7, PID1))
    row = gov.repo.get(PID1)
    out = gov._adopt_remote(row, 10)
    assert out.state == pr.STALE
    assert gov.repo.next_plan_seq("nw_renewal") == 2


def _history(*entries):
    return lambda wid: [dict(e) for e in entries] if wid == "nw_renewal" else []


def test_seed_also_reads_auto_applied_ids_from_config_history(client, store, world, monkeypatch):
    """D6: auto-applied ids never appear in /approvals; they live in config history."""
    monkeypatch.setattr(client, "config_history", _history(
        {"version": 2, "proposal_id": "nw_renewal:7:did_spread", "changed_dims": ["allocations"]},
        {"version": 1, "proposal_id": None, "changed_dims": []}))
    ctl = make_controller(client, store, uniform_hours())
    ctl.gov.seed_plan_seqs()
    assert ctl.gov.repo.next_plan_seq("nw_renewal") == 8


def _auto_intent(ctl, store, world):
    assert run_until(ctl, lambda: len(world.approvals) == 1)
    store.conn.execute("update proposals set state='INTENT', route='auto', base_version=1, approval_id=NULL"
                       " where proposal_id=?", (PID1,))
    return ctl.gov.repo.get(PID1)


def test_adopt_auto_refuses_a_history_entry_with_other_dims(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    row = _auto_intent(ctl, store, world)
    monkeypatch.setattr(client, "config_history", _history(
        {"version": 2, "proposal_id": PID1, "changed_dims": ["prompt_version"]}))
    out = ctl.gov._adopt_auto(row, 10)
    assert out.state == pr.STALE and out.state != pr.AUTO_APPLIED
    assert ctl.gov.repo.next_plan_seq("nw_renewal") == 2


def test_adopt_auto_refuses_a_history_entry_at_the_wrong_version(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    row = _auto_intent(ctl, store, world)
    dims = row.body["dims"]
    monkeypatch.setattr(client, "config_history", _history(
        {"version": 5, "proposal_id": PID1, "changed_dims": dims}))
    assert ctl.gov._adopt_auto(row, 10).state == pr.STALE


def test_adopt_auto_accepts_the_matching_history_entry(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    row = _auto_intent(ctl, store, world)
    monkeypatch.setattr(client, "config_history", _history(
        {"version": 2, "proposal_id": PID1, "changed_dims": list(reversed(row.body["dims"]))}))
    out = ctl.gov._adopt_auto(row, 10)
    assert out.state == pr.AUTO_APPLIED and out.applied_version == 2
