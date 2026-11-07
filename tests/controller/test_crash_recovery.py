"""Fault injection (CRASH_AT) at every named point, then restart and reconcile (R9)."""
import pytest

from controller import faults
from controller import proposal_repo as pr
from controller.desired import ROUTE_AUTO, DesiredChange, Priority
from controller.faults import crash_point, parse_spec
from controller.ingest import Ingestor, mirror_configs
from controller.loop import Controller
from controller.store import Store
from tests.controller.helpers import (SimulatedCrash, arm_crash, disarm_crash, make_controller,
                                      proposal_states, registry_of, run_until, sim_kinds, uniform_hours)

BAD = {"apply_conflict", "approval_lapsed", "auto_apply_rejected"}


@pytest.fixture(autouse=True)
def _reset_faults(monkeypatch):
    disarm_crash(monkeypatch)
    yield
    disarm_crash(monkeypatch)


def restart(client, store, *producers):
    """A new process: fresh Controller (all in-memory state gone) on the same SQLite file."""
    store.close()
    fresh = Store(store.path)
    ctl = Controller(client, fresh, registry=registry_of(*producers))
    ctl.startup()
    return ctl, fresh


def test_fault_spec_parsing_and_counting():
    assert parse_spec("after-advance-before-ingest:3") == ("after-advance-before-ingest", 3)
    assert parse_spec("mid-ingest-txn") == ("mid-ingest-txn", 1) and parse_spec(None) is None
    codes = []
    faults.reset(codes.append)
    for _ in range(3):
        crash_point("x", spec="x:2")
    crash_point("other", spec="x:2")
    assert codes == [faults.CRASH_EXIT_CODE]  # only the 2nd hit of the named point
    assert set(faults.POINTS) == {"mid-ingest-txn", "after-submit-before-record",
                                  "after-apply-before-record", "after-advance-before-ingest",
                                  "after-credit-call-before-record", "before-apply-request"}


def test_crash_after_submit_before_record_adopts_without_duplicate(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    arm_crash(monkeypatch, "after-submit-before-record:1")
    with pytest.raises(SimulatedCrash):
        run_until(ctl, lambda: False)
    pid = "nw_renewal:1:daily_bundle"
    assert len(world.approvals) == 1  # the POST reached the sim ...
    assert proposal_states(store) == {pid: pr.INTENT}  # ... but we never recorded it
    assert store.conn.execute("select approval_id from proposals").fetchone()[0] is None
    disarm_crash(monkeypatch)
    ctl2, store2 = restart(client, store, uniform_hours())
    st = proposal_states(store2)
    assert st[pid] == pr.PENDING  # adopted by looking the proposal_id up
    assert store2.conn.execute("select approval_id from proposals").fetchone()[0] == next(iter(world.approvals))
    assert len(world.approvals) == 1  # no duplicate request
    assert run_until(ctl2, lambda: proposal_states(store2)[pid] in (pr.APPLIED, pr.MONITORING, pr.REJECTED))
    assert len(world.approvals) == 1 or proposal_states(store2)[pid] == pr.REJECTED
    assert not BAD & set(sim_kinds(world))
    # every sim request id maps to exactly one proposal of ours
    assert len({a["proposal_id"] for a in world.approvals.values()}) == len(world.approvals)


def test_crash_before_send_resends_the_identical_body(client, store, world, monkeypatch):
    """INTENT is durable but nothing reached the sim: restart sends the stored body verbatim."""
    ctl = make_controller(client, store, uniform_hours())
    real = client.request_approval
    sent = []

    def die(body):
        sent.append(body)
        raise SimulatedCrash("before the request left the process")

    monkeypatch.setattr(client, "request_approval", die)
    with pytest.raises(SimulatedCrash):
        run_until(ctl, lambda: False)
    monkeypatch.setattr(client, "request_approval", real)
    assert not world.approvals and proposal_states(store) == {"nw_renewal:1:daily_bundle": pr.INTENT}
    ctl2, store2 = restart(client, store, uniform_hours())
    (a,) = world.approvals.values()
    assert a["proposal_id"] == "nw_renewal:1:daily_bundle"
    assert a["request"]["rationale"] == sent[0]["rationale"] and a["request"]["diff"] == sent[0]["diff"]
    assert proposal_states(store2)["nw_renewal:1:daily_bundle"] in (pr.PENDING, pr.APPROVED, pr.REJECTED)


def test_intent_with_changed_base_is_dropped_not_sent(client, store, world):
    ctl = make_controller(client, store)
    from controller.proposals import build_draft
    from controller.cohorts import VOCAB_META_KEY, FeatureVocab
    wf = ctl.gov.workflows["nw_renewal"]
    ver, cfg = ctl.gov.live("nw_renewal")
    draft = build_draft(store, FeatureVocab.load(store.get_meta(VOCAB_META_KEY)), wf, ver, cfg,
                        [DesiredChange("nw_renewal", "daily_bundle", Priority.ROUTINE,
                                       {"hour_weights": {"default": {str(h): 1 for h in range(9, 21)}}})], 0)
    body = {"request": draft.approval_body("nw_renewal:1:daily_bundle"), "dims": list(draft.dims)}
    row = pr.ProposalRow("nw_renewal:1:daily_bundle", "nw_renewal", "daily_bundle", pr.INTENT, None, ver, 0,
                         "approval", 2, 1, None, None, None, None, None, None, body)
    ctl.gov.repo.create_intent(row, 0)
    new = dict(cfg)
    new["allocations"] = {"default": dict(cfg["allocations"]["default"], caller_id={"D1": 0.4, "D2": 0.4, "D3": 0.2})}
    client.put_config("nw_renewal", {"proposal_id": "z", "base_version": ver, "proposed_config": new, "rationale": "r"})
    ctl2, store2 = restart(client, store)
    assert proposal_states(store2) == {"nw_renewal:1:daily_bundle": pr.STALE} and not world.approvals


def test_crash_after_apply_before_record_does_not_double_apply(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    arm_crash(monkeypatch, "after-apply-before-record:1")
    pid = "nw_renewal:1:daily_bundle"
    try:
        run_until(ctl, lambda: False)
        pytest.skip("approver rejected this id")
    except SimulatedCrash:
        pass
    assert proposal_states(store)[pid] == pr.APPROVED  # applied in the sim, not yet recorded
    (a,) = world.approvals.values()
    assert a["status"] == "applied"
    disarm_crash(monkeypatch)
    ctl2, store2 = restart(client, store, uniform_hours())
    row = ctl2.gov.repo.get(pid)
    assert row.state in (pr.APPLIED, pr.MONITORING) and row.applied_version == 2
    assert sim_kinds(world).count("approval_applied") == 1  # applied exactly once
    assert client.get_config("nw_renewal")["version"] == 2
    assert run_until(ctl2, lambda: client.state()["sim_minute"] >= 1500)
    assert client.get_config("nw_renewal")["version"] == 2 and len(world.approvals) == 1
    assert not BAD & set(sim_kinds(world))


def test_crash_after_auto_submit_adopts_from_config_history(client, store, world, monkeypatch):
    auto = DesiredChange("nw_renewal", "did_spread", Priority.ROUTINE,
                         {"allocations": {"default": {"caller_id": {"D1": 0.4, "D2": 0.4, "D3": 0.2}}}},
                         route_preference=ROUTE_AUTO, summary="spread")
    producer = lambda c, wid: [auto] if wid == "nw_renewal" else []  # noqa: E731
    ctl = make_controller(client, store, producer)
    arm_crash(monkeypatch, "after-submit-before-record:1")
    with pytest.raises(SimulatedCrash):
        run_until(ctl, lambda: False)
    assert client.get_config("nw_renewal")["version"] == 2
    assert proposal_states(store) == {"nw_renewal:1:did_spread": pr.INTENT}
    disarm_crash(monkeypatch)
    ctl2, store2 = restart(client, store, producer)
    row = ctl2.gov.repo.get("nw_renewal:1:did_spread")
    assert row.state in (pr.AUTO_APPLIED, pr.MONITORING) and row.applied_version == 2
    assert run_until(ctl2, lambda: client.state()["sim_minute"] >= 1500)
    assert client.get_config("nw_renewal")["version"] == 2  # not applied twice
    assert store2.count("proposals") == 1 and not BAD & set(sim_kinds(world))


def test_crash_mid_ingest_txn_leaves_no_partial_chunk(client, store, world, monkeypatch):
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    for _ in range(40):  # events exist after the daily batch: history chunk, then an events chunk
        client.advance(15)
    arm_crash(monkeypatch, "mid-ingest-txn:2")
    with pytest.raises(SimulatedCrash):
        Ingestor(client, store).sync()
    first_chunk_cursor = store.get_cursor()
    assert first_chunk_cursor.last_chunk == 0 and store.count("calls") > 0  # chunk 1 fully rolled back
    assert store.count("calls") == store.conn.execute("select sum(dials) from cube").fetchone()[0]
    disarm_crash(monkeypatch)
    ing = Ingestor(client, store)
    ing.sync()
    assert store.get_cursor().last_event_id == world.event_seq
    assert store.count("calls") == store.conn.execute("select sum(dials) from cube").fetchone()[0]
    assert store.count("alerts") == 0


def test_crash_after_advance_before_ingest_resumes_without_double_advance(client, store, world, monkeypatch):
    ctl = make_controller(client, store)
    arm_crash(monkeypatch, "after-advance-before-ingest:5")
    with pytest.raises(SimulatedCrash):
        run_until(ctl, lambda: False)
    minute_at_crash = world.now
    assert store.get_cursor().last_event_id <= world.event_seq
    disarm_crash(monkeypatch)
    ctl2, store2 = restart(client, store)
    assert world.now == minute_at_crash  # startup never advances
    done = run_until(ctl2, lambda: client.state()["finished"], limit=1000)
    assert done
    ctl2.tick()
    assert store2.get_cursor().last_event_id == world.event_seq
    assert store2.count("calls") == store2.conn.execute("select sum(dials) from cube").fetchone()[0]
    cursors = [r[0] for r in store2.conn.execute("select last_event_id from ingest_cursor")]
    assert cursors == [world.event_seq]


def test_startup_reconcile_handles_pending_and_approved(client, store, world):
    """Restart while PENDING: the decision is picked up by the new process and applied once."""
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: pr.PENDING in proposal_states(store).values())
    ctl2, store2 = restart(client, store, uniform_hours())
    assert run_until(ctl2, lambda: any(s in (pr.APPLIED, pr.MONITORING, pr.REJECTED)
                                       for s in proposal_states(store2).values()))
    assert sim_kinds(world).count("approval_applied") <= 1 and not BAD & set(sim_kinds(world))


def test_kill_between_submit_and_job_mark_makes_at_most_one_routine_bundle_per_day(client, store, world, monkeypatch):
    ctl = make_controller(client, store, uniform_hours())
    real_mark = ctl.planner.job_mark

    def die(*a, **k):
        raise SimulatedCrash("killed after submit, before job_mark")

    monkeypatch.setattr(ctl.planner, "job_mark", die)
    with pytest.raises(SimulatedCrash):
        run_until(ctl, lambda: False)
    assert len(world.approvals) == 1
    ctl2, store2 = restart(client, store, uniform_hours())  # fresh process: no job mark persisted
    assert store2.get_job("daily_bundle", "nw_renewal") is None
    now = client.state()["sim_minute"]
    assert not ctl2.planner.job_due("daily_bundle", "nw_renewal", now)  # the INTENT/PENDING row spent it
    day = now // 1440
    for _ in range(400):  # still the same sim day: more ticks must not file a second routine bundle
        if client.state()["sim_minute"] // 1440 != day:
            break
        ctl2.tick()
    assert len([a for a in world.approvals.values() if a["created_minute"] // 1440 == day]) == 1
