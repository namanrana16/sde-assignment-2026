"""Regression tests for the Phase 1 review findings (A1-A8)."""
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import sim.server
from controller.desired import Registry
from controller import client as c
from controller import settings
from controller.ingest import IngestIntegrityError, Ingestor, mirror_configs, ts_to_minute
from controller.loop import Controller, Windows, choose_step
from controller.store import Store, _Txn
from controller.world_id import identify_world, open_world_store
from tests.controller.conftest import build_world


def make(handler):
    http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return c.SimClient(http=http, sleep=lambda s: None)


# ---------------------------------------------------------------- A1
CALLS = [
    ("request_approval", ({"proposal_id": "p"},)),
    ("apply_approval", ("wf", "apr_1")),
    ("put_config", ("wf", {"proposal_id": "p"})),
    ("suggest_fixes", ("wf", "problem")),
    ("run_validation", ("wf", "P1", 100)),
]


@pytest.mark.parametrize("method,args", CALLS)
@pytest.mark.parametrize("failure", ["transport", "5xx"])
def test_non_idempotent_calls_are_never_retried(method, args, failure):
    seen = []

    def handler(req):
        seen.append(req)
        if failure == "transport":
            raise httpx.ReadTimeout("lost")
        return httpx.Response(503, text="x")

    with pytest.raises(c.OutcomeUnknown) as exc:
        getattr(make(handler), method)(*args)
    assert len(seen) == 1 and exc.value.path


def test_non_idempotent_4xx_still_typed():
    cl = make(lambda r: httpx.Response(409, json={"error": "duplicate_proposal_id"}))
    with pytest.raises(c.DuplicateProposal):
        cl.request_approval({"proposal_id": "p"})


# ---------------------------------------------------------------- A2
def test_advance_recovery_waits_for_stable_minute():
    minutes = iter([0, 15, 30, 30, 30])  # before, then still moving, then stable
    posted = []

    def handler(req):
        if req.url.path == "/sim/advance":
            posted.append(1)
            raise httpx.ReadTimeout("lost")
        m = next(minutes)
        return httpx.Response(200, json={"sim_minute": m, "ts": "t", "finished": False})

    res = make(handler).advance(60)
    assert res["recovered"] and res["sim_minute"] == 30 and len(posted) == 1


def test_advance_recovery_not_applied_raises():
    def handler(req):
        if req.url.path == "/sim/advance":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"sim_minute": 15, "ts": "t", "finished": False})

    with pytest.raises(c.AdvanceNotApplied):
        make(handler).advance(15)


def test_settle_is_time_bounded(monkeypatch):
    monkeypatch.setattr(settings, "ADVANCE_SETTLE_MAX_S", 0.0)
    n = iter(range(0, 1000, 15))
    cl = make(lambda r: httpx.Response(200, json={"sim_minute": next(n), "ts": "t", "finished": False}))
    assert cl.settled_state()["sim_minute"] >= 0  # returns the last observation instead of hanging


# ---------------------------------------------------------------- A3
def test_sim_minute_regression_means_new_world(client, tmp_path):
    store, _, _ = open_world_store(client, tmp_path / "state")
    store.set_meta("sim_minute", "999999")
    store.add_alert(0, "info", "marker", "m")
    path = store.path
    store.close()
    store2, _, fresh = open_world_store(client, tmp_path / "state")
    assert fresh and store2.count("alerts") == 0 and list(path.parent.glob("*.old"))
    store2.close()


def test_chunk_count_regression_means_new_world(client, tmp_path):
    store, _, _ = open_world_store(client, tmp_path / "state")
    store.set_meta("chunk_count", "10000")
    store.close()
    _, _, fresh = open_world_store(client, tmp_path / "state")
    assert fresh


def test_chunk0_difference_means_new_world(client, tmp_path):
    store, _, _ = open_world_store(client, tmp_path / "state")
    store.set_meta("chunk0_until_ts", "1999-01-01T00:00:00+05:30")
    store.close()
    assert open_world_store(client, tmp_path / "state")[2]


def test_progress_forward_is_not_a_new_world(client, tmp_path):
    store, _, _ = open_world_store(client, tmp_path / "state")
    store.close()
    client.advance(15)
    assert not open_world_store(client, tmp_path / "state")[2]


def test_no_history_world_hashes_first_events_chunk(tmp_path):
    build_world(tmp_path / "w", history_days=0)
    with TestClient(sim.server.app) as http:
        cl = c.SimClient(http=http)
        before = identify_world(cl)
        assert before.anchor_chunk is None
        store, _, _ = open_world_store(cl, tmp_path / "state")
        store.close()
        for _ in range(40):
            cl.advance(15)
        after = identify_world(cl)
        assert after.anchor_chunk == 0 and after.line_hash  # events chunk 0 is hashed
        store2, _, fresh = open_world_store(cl, tmp_path / "state")  # same DB name, not fresh
        assert not fresh and store2.path == store.path
        store2.set_meta("chunk0_line_hash", "0" * 16)
        store2.close()
        assert open_world_store(cl, tmp_path / "state")[2]  # different chunk-0 hash: new world


# ---------------------------------------------------------------- A4
def test_approval_decided_events_are_idempotent(client, store):
    ing = Ingestor(client, store)
    ev = {"event_id": 77, "approval_id": "apr_1", "workflow_id": "w", "status": "approved",
          "ts": "2027-01-04T10:00:00+05:30", "reasons": []}
    ing._on_approval(ev)
    ing._on_approval(ev)
    assert store.count("proposal_events") == 1


def test_batch_with_already_inserted_calls_aborts(client, store):
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    ing = Ingestor(client, store)
    meta = client.list_chunks("history")[0]
    ing.process_chunk(meta)
    snapshot = store.conn.execute("select sum(dials) from cube").fetchone()[0]
    store.set_cursor(-1, 0)  # force a replay past the event_id guard
    with pytest.raises(IngestIntegrityError):
        Ingestor(client, store).process_chunk(meta)
    assert store.conn.execute("select sum(dials) from cube").fetchone()[0] == snapshot


def test_ts_cache_is_bounded():
    assert ts_to_minute.cache_info().maxsize == settings.TS_CACHE_MAX


def test_subbatches_give_identical_result(tmp_path, monkeypatch):
    def run(sub, name):
        build_world(tmp_path / name)
        monkeypatch.setattr(settings, "INGEST_SUBBATCH_LINES", sub)
        with TestClient(sim.server.app) as http:
            cl = c.SimClient(http=http)
            st = Store(tmp_path / name / "t.db")
            mirror_configs(cl, st, [w["workflow_id"] for w in cl.workflows()])
            Ingestor(cl, st).sync()
            out = (st.conn.execute("select count(*), sum(engaged) from calls").fetchone(),
                   st.conn.execute("select count(*), sum(dials) from cube").fetchone(),
                   st.conn.execute("select count(*) from leads").fetchone())
            st.close()
            return out

    assert run(37, "a") == run(50_000, "b")


def test_cohort_check_skips_are_summarised(client, store):
    ing = Ingestor(client, store)  # no config bodies mirrored at all
    ing.sync()
    rows = store.conn.execute("select message from alerts where code='cohort_check_skipped'").fetchall()
    assert 1 <= len(rows) <= len(client.list_chunks("history"))
    assert int(store.get_meta("cohort_check_skipped")) > 0
    assert store.count("alerts") == len(rows)  # summarised, no per-call alerts


# ---------------------------------------------------------------- A5
class _FakeConn:
    def __init__(self, fail_commit=True, fail_rollback=False):
        self.log, self.fail_commit, self.fail_rollback = [], fail_commit, fail_rollback

    def execute(self, sql):
        self.log.append(sql)
        if sql == "COMMIT" and self.fail_commit:
            raise sqlite3.OperationalError("disk I/O error")
        if sql == "ROLLBACK" and self.fail_rollback:
            raise sqlite3.OperationalError("no transaction")


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_commit_failure_rolls_back_and_reraises_original(rollback_fails):
    conn = _FakeConn(fail_rollback=rollback_fails)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        with _Txn(conn):
            pass
    assert conn.log == ["BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"]


def test_txn_exit_returns_none_and_store_is_a_context_manager(tmp_path):
    assert _Txn(_FakeConn(fail_commit=False)).__exit__(None, None, None) is None
    with Store(tmp_path / "s.db") as st:
        st.add_alert(1, "info", "c", "m")
    with pytest.raises(sqlite3.ProgrammingError):
        st.count("alerts")  # closed


# ---------------------------------------------------------------- A6
def test_windows_missing_approver_hours_keeps_idle_step():
    wfs = [{"dialling_window": [9, 21], "governance": {"approver_hours": [10, 19]}},
           {"dialling_window": [10, 20], "governance": {}}]
    w = Windows.from_workflows(wfs)
    assert not w.unknown and choose_step(3 * 60, 10 * 1440, w, False) == 60
    assert Windows.from_workflows([{"governance": {}}]).unknown  # no dialling window: stay active


def test_choose_step_uses_sim_tick():
    w = Windows(((9, 21),))
    assert choose_step(12 * 60, 10 * 1440, w, False, tick=30) == 30
    assert choose_step(3 * 60, 10 * 1440, w, False, tick=30) == 60


def test_finished_from_advance_is_reported(client, store, monkeypatch):
    def finished(minutes):
        raise c.Finished(409, "finished", {})

    monkeypatch.setattr(client, "advance", finished)
    result = Controller(client, store, stop_at_minute=None, registry=Registry()).run()
    assert result.finished and result.ticks == 1


def test_lag_metric_is_non_negative_and_small(client, store):
    Controller(client, store, stop_at_minute=30, registry=Registry()).run()
    lags = [r[0] for r in store.conn.execute("select lag_s from metrics")]
    assert lags and all(0 <= lag < 60 for lag in lags)


# ---------------------------------------------------------------- A8
def test_history_and_events_share_global_numbering_and_resume(client, store, world):
    hist = client.list_chunks("history")
    for _ in range(40):
        client.advance(15)
    events = client.list_chunks("events")
    assert hist and events
    both = sorted(hist + events, key=lambda m: m["chunk"])
    numbers = [m["chunk"] for m in both]
    assert numbers == list(range(len(numbers)))  # one global sequence, no restart at the boundary
    assert max(m["chunk"] for m in hist) < min(m["chunk"] for m in events)
    assert all(a["last_event_id"] < b["first_event_id"] for a, b in zip(both, both[1:]))

    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    for meta in hist:  # crash exactly on the history/events boundary
        Ingestor(client, store).process_chunk(meta)
    assert store.get_cursor().last_chunk == max(m["chunk"] for m in hist)
    stats = Ingestor(client, store).sync()  # a restarted ingestor resumes with the first events chunk
    assert stats.chunks == len(events)
    assert store.get_cursor().last_chunk == numbers[-1]
    assert store.get_cursor().last_event_id == world.event_seq
