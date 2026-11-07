"""C14 alert rules on synthetic store states: each rule fires, carries an action, dedupes, and clears."""
import json

from controller import alerts, metrics
from controller.store import Store

DAY = 1440


def codes(store, severity=None):
    sql = "SELECT code FROM alerts" + (f" WHERE severity='{severity}'" if severity else "")
    return [r[0] for r in store.conn.execute(sql)]


def tick(store, now, **kw):
    return alerts.apply(store, now, alerts.evaluate(store, now, **kw))


def add_proposal(store, pid, state, wid="wf", apply_by=None, expires=None, body=None, approval_id=None, route="approval"):
    store.conn.execute(
        "INSERT INTO proposals(proposal_id, workflow_id, kind, state, approval_id, base_version, created_minute, body, route,"
        " priority, plan_seq, apply_by_minute, expires_minute) VALUES (?,?,?,?,?,1,0,?,?,0,1,?,?)",
        (pid, wid, "k", state, approval_id, json.dumps(body or {}), route, apply_by, expires))


def add_call(store, event_id, minute, wid="wf"):
    store.conn.execute(
        "INSERT INTO calls(event_id, workflow_id, ts_minute, day, hour, lid, attempt_no, config_version, cohort_id, welcome,"
        " voice, caller_id, prompt, connected, engaged) VALUES (?,?,?,?,?,1,1,1,'default','W1','V1','D1','P1',1,0)",
        (event_id, wid, minute, minute // DAY, minute % DAY // 60))
    _cube_call(store, wid, minute, "W1", 1)


def _cube_call(store, wid, minute, welcome, connected):
    """Ingest keeps the hourly cube in step with calls; the per-tick alert rules read the cube, not calls."""
    store.conn.execute("INSERT INTO cube VALUES (?,?,?,0,?,'V1','D1','P1',1,?,0,0,0) ON CONFLICT DO UPDATE SET"
                       " dials=dials+1, connects=connects+excluded.connects",
                       (wid, minute // DAY, minute % DAY // 60, welcome, connected))


def test_apply_by_near_pages_once_then_clears_and_refires(store):
    add_proposal(store, "wf:1:x", "APPROVED", apply_by=1000)
    assert tick(store, 960) == []  # 40 min left: not yet
    assert [a.code for a in tick(store, 975)] == ["apply_by_near"]
    assert tick(store, 990) == []  # same condition, deduped
    store.conn.execute("UPDATE proposals SET state='APPLIED'")
    tick(store, 995)  # condition cleared
    store.conn.execute("UPDATE proposals SET state='APPROVED'")
    assert [a.code for a in tick(store, 1000)] == ["apply_by_near"]
    assert codes(store, "page") == ["apply_by_near", "apply_by_near"]


def test_every_page_message_names_a_command(store):
    add_proposal(store, "wf:1:x", "APPROVED", apply_by=1000)
    fresh = tick(store, 990, credits=1.0)
    assert fresh and all("python -m controller" in a.message for a in fresh)


def test_credits_below_incident_reserve(store):
    store.set_meta("workflow_policy:a", "{}")
    store.set_meta("workflow_policy:b", "{}")
    assert tick(store, 10, credits=60.0) == []  # reserve is 2 x 25 = 50
    assert [a.code for a in tick(store, 20, credits=49.0)] == ["credits_low"]
    assert tick(store, 30, credits=40.0) == []  # deduped while it persists


def test_ingest_stalled_needs_silence_where_yesterday_had_calls(store):
    for i in range(30):
        add_call(store, i, DAY + 650 + i)  # yesterday afternoon traffic
    now = 2 * DAY + 600 + 200  # 200 min after yesterday's busy window, today silent since 09:00
    add_call(store, 1000, now - 400)
    assert "ingest_stalled" in [a.code for a in tick(store, now)]


def test_overnight_silence_is_not_a_stall(store):
    for i in range(30):
        add_call(store, i, DAY + 600 + i)
    add_call(store, 1000, 2 * DAY + 20 * 60)  # last call 20:00
    assert tick(store, 3 * DAY + 3 * 60) == []  # 03:00: yesterday had no calls in 00:00-03:00


def test_wall_lag_pages_even_without_history(store):
    add_call(store, 1, 100)
    assert [a.code for a in tick(store, 110, wall_lag_s=900.0)] == ["ingest_stalled"]


def test_repeated_5xx(store):
    metrics.ensure(store)
    for minute, n in ((0, 0), (30, 1), (60, 4)):
        store.conn.execute("INSERT INTO metrics_ext(sim_minute, credits, in_flight, decisions_total, c4xx, c409, c403, c5xx)"
                           " VALUES (?,100,0,0,0,0,0,?)", (minute, n))
    assert [a.code for a in tick(store, 60)] == ["sim_5xx"]


def test_fatigue_rejection_and_403_page_once(store):
    add_proposal(store, "wf:1:x", "REJECTED", approval_id="ap1")
    store.conn.execute("INSERT INTO proposal_events(approval_id, proposal_id, workflow_id, ts_minute, event, payload)"
                       " VALUES ('ap1','wf:1:x','wf',5,'rejected',?)", (json.dumps(["fatigue limit exceeded"]),))
    store.set_meta("client_error_totals", json.dumps({"403": 1}))
    got = sorted(a.code for a in tick(store, 10))
    assert got == ["fatigue_rejection", "http_403"]
    assert tick(store, 20) == []
    assert "judgement_rejection" not in codes(store)  # a hygiene rejection is a bug, not a judgement call


def test_cohort_mismatch_pages_per_workflow_day(store):
    store.add_alert(100, "error", "cohort_mismatch", "lead x", "wf")
    store.add_alert(101, "error", "cohort_mismatch", "lead y", "wf")
    assert [a.code for a in tick(store, 120)] == ["cohort_mismatch"]
    store.add_alert(130, "error", "cohort_mismatch", "lead z", "wf")
    assert tick(store, 140) == []


def test_judgement_rejection_is_digest_only_and_once(store):
    add_proposal(store, "wf:2:x", "REJECTED", approval_id="ap2")
    store.conn.execute("INSERT INTO proposal_events(approval_id, proposal_id, workflow_id, ts_minute, event, payload)"
                       " VALUES ('ap2','wf:2:x','wf',5,'rejected',?)", (json.dumps(["not now"]),))
    assert [(a.severity, a.code) for a in tick(store, 10)] == [("digest", "judgement_rejection")]
    assert tick(store, 20) == []


def test_weak_base_did_digest(store):
    store.add_decision(50, "did_weak_base", "WEAK BASE D3: pickup 0.60x expected", "wf")
    assert [(a.severity, a.code) for a in tick(store, 60)] == [("digest", "weak_base_did")]
    assert tick(store, 70) == []


def test_starved_experiment_digest_and_clears(store):
    st = {"phase": "running", "start_minute": 0, "arms": {"W2": {"state": "active"}, "W3": {"state": "active"}}}
    store.conn.execute("INSERT INTO detector_state VALUES ('experiments','wf:welcome_message',?)", (json.dumps(st),))
    for i in range(150):
        store.conn.execute(
            "INSERT INTO calls(event_id, workflow_id, ts_minute, day, hour, lid, attempt_no, config_version, cohort_id, welcome,"
            " voice, caller_id, prompt, connected, engaged) VALUES (?,?,?,0,10,1,1,1,'d','W2','V1','D1','P1',1,0)",
            (i, "wf", 10))
        _cube_call(store, "wf", 10, "W2", 1)
    got = tick(store, 3 * DAY)
    assert [a.code for a in got] == ["starved_experiment"] and "W3" in got[0].message and "W2" not in got[0].message


def _cube(store, day, pickup_connects, wid="wf"):
    for hour in range(9, 21):
        store.conn.execute("INSERT INTO cube VALUES (?,?,?,0,'W1','V1','D1','P1',?,?,?,0,0)",
                           (wid, day, hour, 200, pickup_connects, pickup_connects // 2))


def test_kpi_drift_fires_when_unattributed_and_not_when_a_change_explains_it(store):
    store.conn.execute("INSERT INTO config_versions(workflow_id, version, actor, ts_minute) VALUES ('wf',1,'x',0)")
    for d in range(1, 4):
        _cube(store, d, 60)
    _cube(store, 4, 90)  # pickup 0.45 vs 0.30, engagement unchanged
    got = tick(store, 5 * DAY + 60)
    assert [a.code for a in got] == ["kpi_drift"] and "pickup" in got[0].message
    assert tick(store, 5 * DAY + 120) == []  # evaluated once per day
    # the same shift on another workflow with a config change that day is attributable: no alert
    for d in range(1, 4):
        _cube(store, d, 60, "wf2")
    _cube(store, 4, 90, "wf2")
    store.conn.execute("INSERT INTO config_versions(workflow_id, version, actor, ts_minute) VALUES ('wf2',1,'x',0)")
    store.conn.execute("INSERT INTO config_versions(workflow_id, version, actor, ts_minute) VALUES ('wf2',2,'controller_auto',?)",
                       (4 * DAY + 500,))
    assert tick(store, 5 * DAY + 180) == []


def test_kpi_drift_quiet_on_stable_data(store):
    store.conn.execute("INSERT INTO config_versions(workflow_id, version, actor, ts_minute) VALUES ('wf',1,'x',0)")
    for d in range(1, 5):
        _cube(store, d, 60)
    assert tick(store, 5 * DAY + 60) == []


def test_dedupe_state_survives_a_restart(tmp_path):
    path = tmp_path / "a.db"
    with Store(path) as s:
        add_proposal(s, "wf:1:x", "APPROVED", apply_by=1000)
        assert len(tick(s, 990)) == 1
    with Store(path) as s:
        assert tick(s, 995) == []


def test_status_render_on_empty_and_recorded_store(store):
    assert metrics.render(metrics.collect(store)) == "no ticks recorded yet"
    store.add_metric(1500, 120, 0.5, 1.5, 2.0, 15)
    add_proposal(store, "wf:1:x", "APPROVED", apply_by=1560)
    metrics.record(store, 1500, 88.0, metrics.add_counters(store, {"5xx": 2, "409": 1, "4xx": 1}))
    out = metrics.render(metrics.collect(store))
    assert "credits remaining 88.00" in out and "approvals in flight 1" in out
    assert "60 sim min from now" in out and "5xx 2" in out and "409 1" in out
    assert metrics.add_counters(store, {"5xx": 1})["5xx"] == 3  # cumulative across processes


def test_client_counts_local_error_statuses(client):
    import pytest as _pytest

    from controller.client import NotFound
    with _pytest.raises(NotFound):
        client.get_config("no_such_workflow")
    assert client.counters.get("4xx") == 1 and "5xx" not in client.counters
