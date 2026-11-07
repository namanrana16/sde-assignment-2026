"""Review-2 observability fixes: read-only safety, per-tick query cost, alert classification and isolation."""
import json
import os
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from controller import __main__ as cli
from controller import alerts, metrics, person_analysis, ui, view_data
from controller.__main__ import main as cli_main
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client, when

DAY = 1440
HYGIENE_REASONS = [
    "change_summary, diff, dry_run and rationale must all be non-empty",
    "affected_leads must be a non-negative integer",
    "impact_score must be in [0,1]",
    "prompt changes need evidence.validation_run_id for this workflow and prompt",
    "validation pass rate 0.500 below 0.8",
    "approver fatigue: more than 3 requests in 24h for this workflow - batch your changes",
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("obs2")
    world = build(tmp, ("nw_renewal",), days=4, history_days=6)
    client, http = make_client(world)
    store = Store(tmp / "state" / "t.db")
    Controller(client, store, stop_at_minute=when(3, 0)).run()
    path = store.path
    store.close()
    http.__exit__(None, None, None)
    return path


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    yield s
    s.close()


def add_call(store, event_id, minute, wid="wf", welcome="W1", connected=1):
    store.conn.execute(
        "INSERT INTO calls(event_id, workflow_id, ts_minute, day, hour, lid, attempt_no, config_version, cohort_id, welcome,"
        " voice, caller_id, prompt, connected, engaged) VALUES (?,?,?,?,?,1,1,1,'default',?,'V1','D1','P1',?,0)",
        (event_id, wid, minute, minute // DAY, minute % DAY // 60, welcome, connected))
    store.conn.execute(
        "INSERT INTO cube VALUES (?,?,?,0,?,'V1','D1','P1',1,?,0,0,0) ON CONFLICT DO UPDATE SET dials=dials+1,"
        " connects=connects+excluded.connects", (wid, minute // DAY, minute % DAY // 60, welcome, connected))


def add_rejection(store, pid, text, approval="ap"):
    store.conn.execute(
        "INSERT INTO proposals(proposal_id, workflow_id, kind, state, approval_id, base_version, created_minute, body, route,"
        " priority, plan_seq) VALUES (?,?,?,?,?,1,0,'{}','approval',0,1)", (pid, "wf", "k", "REJECTED", approval))
    store.conn.execute("INSERT INTO proposal_events(approval_id, proposal_id, workflow_id, ts_minute, event, payload)"
                       " VALUES (?,?,'wf',5,'rejected',?)", (approval, pid, json.dumps([text])))


def tick(store, now, **kw):
    return alerts.apply(store, now, alerts.evaluate(store, now, **kw))


# 1 ---------------------------------------------------------------------------------------------------------------
def test_readonly_view_survives_an_empty_exp_day_cache(db, tmp_path):
    copy = tmp_path / "c.db"
    shutil.copy(db, copy)
    rw = Store(copy)
    rw.conn.execute("DELETE FROM exp_day_cache")
    rw.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rw.close()
    ro = view_data.open_readonly(copy)
    try:
        view_data.build(ro, "nw_renewal")
    finally:
        ro.close()
    r = TestClient(ui.create_app(copy)).get("/wf/nw_renewal")
    assert r.status_code == 200


# 2 ---------------------------------------------------------------------------------------------------------------
def test_per_tick_queries_never_full_scan_calls(store):
    for i in range(40):
        add_call(store, i + 1, DAY + 650 + i)
    add_call(store, 100, 2 * DAY + 100)
    st = {"phase": "running", "start_minute": 0, "arms": {"W2": {"state": "active"}}}
    store.conn.execute("INSERT INTO detector_state VALUES ('experiments','wf:welcome_message',?)", (json.dumps(st),))
    seen: list[str] = []
    store.conn.set_trace_callback(seen.append)
    now = 2 * DAY + 600 + 400
    alerts.apply(store, now, alerts.evaluate(store, now))
    with store.transaction():
        metrics.record(store, now, 10.0, {})
    store.conn.set_trace_callback(None)
    touching = [s for s in seen if " calls" in s.lower() and s.strip().upper().startswith("SELECT")]
    assert touching, "expected the per-tick path to read calls at least for the newest minute"
    for sql in touching:
        plan = " | ".join(r[3] for r in store.conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "SCAN calls" not in plan, f"{sql}\n{plan}"


def test_stall_still_detected_from_cube_volume(store):
    for i in range(30):
        add_call(store, i + 1, DAY + 650 + i)
    add_call(store, 1000, 2 * DAY + 200)
    now = 2 * DAY + 600 + 200
    assert "ingest_stalled" in [a.code for a in tick(store, now)]


def test_starved_experiment_checked_once_per_sim_day_and_stays_active(store):
    st = {"phase": "running", "start_minute": 0, "arms": {"W2": {"state": "active"}, "W3": {"state": "active"}}}
    store.conn.execute("INSERT INTO detector_state VALUES ('experiments','wf:welcome_message',?)", (json.dumps(st),))
    for i in range(150):
        add_call(store, i + 1, 10, welcome="W2")
    assert [a.code for a in tick(store, 3 * DAY)] == ["starved_experiment"]
    seen: list[str] = []
    store.conn.set_trace_callback(seen.append)
    assert tick(store, 3 * DAY + 5) == []  # same day: throttled, and the condition is not re-fired
    store.conn.set_trace_callback(None)
    assert not [s for s in seen if "FROM cube" in s and "GROUP BY" in s.upper()]  # the starved roll-up
    assert "starved_experiment" in [a.code for a in alerts.evaluate(store, 3 * DAY + 10)]  # still reported as active
    assert tick(store, 4 * DAY + 5) == []  # next day recomputes, condition persists: deduped


# 3 ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reason", HYGIENE_REASONS)
def test_every_hygiene_rejection_pages_and_none_is_digested(store, reason):
    add_rejection(store, "wf:1:x", reason)
    got = [(a.severity, a.code) for a in tick(store, 10)]
    assert len(got) == 1 and got[0][0] == "page", got


def test_judgement_rejection_digested_never_paged(store):
    add_rejection(store, "wf:1:x", "approver declined (judgement call)")
    assert [(a.severity, a.code) for a in tick(store, 10)] == [("digest", "judgement_rejection")]


# 4 ---------------------------------------------------------------------------------------------------------------
class _Client:
    counters: dict = {}


def test_one_failing_rule_does_not_disable_the_rest_or_metrics(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(alerts, "weak_base", boom)
    store.add_decision(50, "did_weak_base", "WEAK BASE D3", "wf")
    store.add_metric(1, 0, 0.0, 0.0, 0.0, 5)
    obs = alerts.TickObserver(_Client(), store)
    fresh = obs.after_tick({"credits_remaining": 77.0}, 100)
    assert "observability_rule_failed" in [a.code for a in fresh]
    assert store.conn.execute("SELECT COUNT(*) FROM metrics_ext").fetchone()[0] == 1
    failed = store.conn.execute("SELECT severity, message FROM alerts WHERE code='observability_rule_failed'").fetchall()
    assert len(failed) == 1 and failed[0][0] == "digest" and "weak_base" in failed[0][1]
    assert obs.after_tick({"credits_remaining": 77.0}, 200) == []  # once per day


def test_other_rules_still_fire_when_one_fails(store, monkeypatch):
    monkeypatch.setattr(alerts, "weak_base", lambda *a, **k: 1 / 0)
    store.set_meta("workflow_policy:a", "{}")
    got = [a.code for a in alerts.evaluate(store, 10, credits=1.0)]
    assert "credits_low" in got and "observability_rule_failed" in got


# 5 ---------------------------------------------------------------------------------------------------------------
class _Locked:
    def close(self):
        pass

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


def test_rows_propagates_locked_but_maps_missing_table_to_empty(store):
    assert view_data.rows(store, "SELECT * FROM no_such_table_xyz") == []
    store.conn = _Locked()
    with pytest.raises(sqlite3.OperationalError):
        view_data.rows(store, "SELECT 1")


def test_ui_reports_503_retry_when_the_database_is_locked(db, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(view_data, "workflow_ids", locked)
    r = TestClient(ui.create_app(db), raise_server_exceptions=False).get("/wf/nw_renewal")
    assert r.status_code == 503 and "retry" in r.text.lower()


# 6 ---------------------------------------------------------------------------------------------------------------
def test_latest_db_counts_the_wal_mtime(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    assert cli._latest_db(str(tmp_path)) == b
    wal = tmp_path / "a.db-wal"
    wal.write_bytes(b"x")
    os.utime(wal, (3000, 3000))
    assert cli._latest_db(str(tmp_path)) == a


# 7 ---------------------------------------------------------------------------------------------------------------
def test_failing_person_analysis_is_attempted_once_per_sim_day(store, monkeypatch):
    monkeypatch.setattr(person_analysis.settings, "ENABLE_PERSON_ANALYSIS", True, raising=False)
    n = []

    def boom(*a, **k):
        n.append(1)
        raise RuntimeError("bad")
    monkeypatch.setattr(person_analysis, "run", boom)
    with pytest.raises(RuntimeError):
        person_analysis.maybe_run_daily(store, 2 * DAY)
    assert person_analysis.maybe_run_daily(store, 2 * DAY + 30) is None
    assert len(n) == 1


# 8 ---------------------------------------------------------------------------------------------------------------
def test_why_uses_a_read_only_store(db, monkeypatch):
    def no_rw(*a, **k):
        raise AssertionError("why must not open a read-write Store")
    monkeypatch.setattr(cli, "Store", no_rw)
    assert cli_main(["why", "--db", str(db), "--workflow", "nw_renewal", "--at", "Day 2 12:00"]) == 0


def test_person_analysis_is_read_only_unless_persist(db, tmp_path):
    copy = tmp_path / "p.db"
    shutil.copy(db, copy)

    def count():
        c = sqlite3.connect(copy)
        try:
            return c.execute("SELECT COUNT(*) FROM decisions WHERE kind='person_analysis'").fetchone()[0]
        finally:
            c.close()
    before = count()
    assert cli_main(["person-analysis", "--db", str(copy)]) == 0
    assert count() == before
    assert cli_main(["person-analysis", "--db", str(copy), "--persist"]) == 0
    assert count() > before


# 9 ---------------------------------------------------------------------------------------------------------------
def test_wf_with_garbage_at_is_400(db):
    r = TestClient(ui.create_app(db), raise_server_exceptions=False).get("/wf/nw_renewal?at=garbage")
    assert r.status_code == 400


# 10 --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("content", [b"", b"this is not sqlite at all, just text padding " * 40])
@pytest.mark.parametrize("cmd", ["status", "view"])
def test_foreign_file_gives_message_not_traceback(tmp_path, capsys, cmd, content):
    f = tmp_path / "x.db"
    f.write_bytes(content)
    argv = [cmd, "--db", str(f)] + (["--workflow", "w"] if cmd == "view" else [])
    assert cli_main(argv) == 1
    assert "cannot open" in capsys.readouterr().out


# 11 --------------------------------------------------------------------------------------------------------------
def _approved(store, apply_by):
    store.conn.execute(
        "INSERT INTO proposals(proposal_id, workflow_id, kind, state, base_version, created_minute, body, route,"
        " priority, plan_seq, apply_by_minute) VALUES ('wf:1:x','wf','k','APPROVED',1,0,'{}','approval',0,1,?)", (apply_by,))


def test_apply_by_near_stops_at_the_deadline_and_pages_lapse_risk_once(store):
    _approved(store, 1000)
    assert [a.code for a in tick(store, 980)] == ["apply_by_near"]
    assert [a.code for a in tick(store, 1005)] == ["apply_by_passed"]
    assert tick(store, 1010) == []
    assert not [r for r in store.conn.execute("SELECT message FROM alerts") if "-" in r[0].split("sim min")[0][-4:]]
