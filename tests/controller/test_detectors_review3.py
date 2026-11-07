"""Review round 3: per-workflow isolation (did, schedule), schedule over a business-default table,
unreachable validation bar, the sim's tick in the halluc audit row."""
from controller import incidents, schedule, settings
from controller.desired import PlanContext
from controller.detectors import did, halluc as hl
from controller.detectors.common import save_state
from tests.controller.test_halluc_units import FakeClient
from tests.controller.test_schedule_producer import HOURS, PLAN, cfg, wf


def _alerts(store, code):
    return store.conn.execute("SELECT workflow_id FROM alerts WHERE code=?", (code,)).fetchall()


# ------------------------------------------------------------------ 1. per-workflow isolation
def test_did_detector_isolates_a_raising_workflow(store, monkeypatch):
    seen = []

    def evaluate(ctx, wid):
        seen.append(wid)
        if wid == "a":
            raise KeyError("boom")
        return None

    monkeypatch.setattr(did, "_evaluate", evaluate)
    opts = {"options": {"caller_id": ["d1", "d2"]}}
    ctx = PlanContext(store, None, {"a": opts, "b": opts}, 100)
    did.did_detector(ctx)
    did.did_detector(ctx)  # same sim day: the alert is deduped
    assert seen == ["a", "b", "a", "b"]
    assert _alerts(store, "did_workflow_error") == [("a",)]


def test_schedule_detector_isolates_a_raising_workflow(store, monkeypatch):
    seen = []

    def ready(st, wid, now):
        seen.append(wid)
        if wid == "a":
            raise ValueError("boom")
        return False

    monkeypatch.setattr(schedule, "_ready", ready)
    for wid in ("a", "b"):
        store.store_config_body(wid, 1, "business_default", cfg())
    ctx = PlanContext(store, None, {"a": wf(False), "b": wf(False)}, 100)
    schedule.schedule_detector(ctx)
    schedule.schedule_detector(ctx)
    assert seen == ["a", "b", "a", "b"]
    assert _alerts(store, "schedule_workflow_error") == [("a",)]


# ------------------------------------------------------------------ 2. business-default tables
def _versions(store, rows):
    for v, actor, body in rows:
        store.store_config_body("w", v, actor, body)
        store.upsert_config_event("w", v, actor, v * 10, ["schedule"], None, None)


def _producer(store):
    return schedule.schedule_producer(PlanContext(store, None, {"w": wf(True)}, 100), "w")


def test_business_default_tables_are_refreshable(store):
    save_state(store, schedule.DETECTOR, "w", {"plan": PLAN})
    _versions(store, [(1, "business_default", cfg(PLAN["cohorts"], {"default": HOURS}))])
    [ch] = _producer(store)
    assert ch.kind == "schedule_refresh"


def test_supervisor_tables_stay_untouched(store):
    save_state(store, schedule.DETECTOR, "w", {"plan": PLAN})
    _versions(store, [(1, "business_default", cfg(PLAN["cohorts"], {})),
                      (2, "supervisor", cfg(PLAN["cohorts"], {"default": HOURS}))])
    assert _producer(store) == []


def test_supervisor_edit_over_business_default_tables_stays_untouched(store):
    save_state(store, schedule.DETECTOR, "w", {"plan": PLAN})
    _versions(store, [(1, "business_default", cfg(PLAN["cohorts"], {"default": HOURS})),
                      (2, "supervisor", cfg(PLAN["cohorts"], {"default": {str(h): 2.0 for h in range(9, 21)}}))])
    assert _producer(store) == []


def test_rollback_restores_the_business_default_tables(store):
    ours = {"default": {str(h): 1.2 for h in range(9, 21)}}
    state = {"plan": PLAN, "applied": {"tables": ours, "status": schedule.ROLLBACK, "prior_tables": {"default": HOURS}}}
    save_state(store, schedule.DETECTOR, "w", state)
    _versions(store, [(1, "business_default", cfg(PLAN["cohorts"], {"default": HOURS})),
                      (2, "controller", cfg(PLAN["cohorts"], ours))])
    [ch] = _producer(store)
    assert ch.kind == "schedule_rollback" and ch.patch == {"hour_weights": {"default": HOURS}}


# ------------------------------------------------------------------ 3. unreachable validation bar
WF3 = {"workflow_id": "w", "governance": {"min_validation_pass_rate": 0.995},
       "options": {"prompt_version": ["P1", "P2", "P3"]},
       "objective": {"primary": "engagement_rate", "guardrail": {}}}


def _validate(store, prompt, rate, n=300):
    incidents.store_validation(store, {"validation_run_id": f"v_{prompt}", "workflow_id": "w", "prompt_version": prompt,
                                       "n": n, "pass_rate": rate}, 3, 0)


def _body():
    return hl._new_body("P2", None, {"rate": 0.2, "base_rate": 0.0, "n": 400, "p": 0.0}, {}, "bad_prompt_day0")


def test_no_spend_once_every_catalog_prompt_is_below_the_bar(store):
    for p in ("P1", "P2", "P3"):
        _validate(store, p, 0.97)
    client, body = FakeClient(), _body()
    ctx = PlanContext(store, client, {"w": WF3}, 5000)
    hl.prepare_fix(ctx, "w", body, hl.hd.fresh_state(), "P2")
    assert client.suggest_calls == 0 and client.validations == [] and body["target"] is None
    assert _alerts(store, "prompt_bar_unreachable") == [("w",)]
    assert store.conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='prompt_bar_unreachable'").fetchone()[0] == 1


def test_a_catalog_prompt_without_a_run_keeps_the_search_open(store):
    for p in ("P1", "P2"):
        _validate(store, p, 0.97)  # P3 never validated: a new prompt appeared
    client = FakeClient()
    ctx = PlanContext(store, client, {"w": WF3}, 5000)
    hl.prepare_fix(ctx, "w", _body(), hl.hd.fresh_state(), "P2")
    assert client.suggest_calls == 1
    assert _alerts(store, "prompt_bar_unreachable") == []


def test_one_passing_prompt_is_never_blocked(store):
    _validate(store, "P1", 0.97)
    _validate(store, "P2", 0.97)
    _validate(store, "P3", 1.0)
    ctx = PlanContext(store, FakeClient(), {"w": WF3}, 5000)
    assert not hl._bar_unreachable(ctx, "w", _body(), 0.995)


def test_a_borderline_run_still_gets_its_revalidation(store):
    _validate(store, "P1", 0.993, n=100)  # within 2 se of the bar and small n: one bigger sample may pass
    _validate(store, "P2", 0.97)
    _validate(store, "P3", 0.97)
    ctx = PlanContext(store, FakeClient(), {"w": WF3}, 5000)
    assert not hl._bar_unreachable(ctx, "w", _body(), 0.995)


# ------------------------------------------------------------------ 4. the sim's tick
def test_tick_minutes_comes_from_the_run_not_the_default(store):
    ctx = PlanContext(store, None, {}, 0)
    assert hl._tick_minutes(ctx) == settings.TICK_MINUTES
    store.add_metric(30, 0, 0.0, 0.0, 0.0, 60)  # an idle long step
    store.add_metric(60, 0, 0.0, 0.0, 0.0, 30)
    assert hl._tick_minutes(ctx) == 30


def test_hourly_audit_row_uses_the_sim_tick(store):
    from controller.detectors import halluc_data as hd
    store.add_metric(30, 0, 0.0, 0.0, 0.0, 30)
    st = hd.fresh_state()
    st["first_prompt"], st["base_frozen"], st["base"] = "P1", True, [5000, 50, 4800.0, 2500]
    st["prompts"] = {"P1": {"total": [5000, 50, 4800.0, 2500], "hours": {}, "first_ts": 0, "last_ts": 0}}
    ctx = PlanContext(store, None, {"w": WF3}, 6000 + 20)  # minute 20 of the hour: inside a 30-min tick, not a 15-min one
    hl._production_spike(ctx, "w", st, "P1")
    assert store.conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='halluc_eval'").fetchone()[0] == 1
