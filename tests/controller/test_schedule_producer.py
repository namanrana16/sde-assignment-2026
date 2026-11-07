"""B3: producer routing rules, using persisted state only."""
from types import SimpleNamespace

from controller import schedule
from controller.detectors.common import save_state
from controller.desired import ROUTE_APPROVAL, ROUTE_AUTO, PlanContext
from controller.store import Store

HOURS = {str(h): 1.0 for h in range(9, 21)}


def wf(auto):
    return {"workflow_id": "w", "dialling_window": [9, 21],
            "governance": {"auto_apply": {"enabled": auto, "allowed_dims": ["schedule"] if auto else [], "max_shift": 0.2}}}


def cfg(cohorts=(), tables=None):
    return {"cohorts": list(cohorts), "allocations": {"default": {}}, "prompt_version": "P1",
            "schedule": {"hour_weights": tables or {}, "lead_preferred_hour": {}}}


def setup(tmp_path, config, state, auto):
    store = Store(tmp_path / "s.db")
    store.store_config_body("w", 1, "x", config)
    save_state(store, schedule.DETECTOR, "w", state)
    return PlanContext(store, None, {"w": wf(auto)}, 100)


PLAN = {"cohorts": [{"id": "a", "where": {"age_band": ["60+"]}}], "tables": {"a": HOURS, "default": HOURS},
        "estimate": {"rel_lift": 0.01, "pickup_old": 0.3, "pickup_new": 0.303, "engagement_drop": 0.0},
        "blocked": False, "max_drop": 0.02}


def test_day0_bundle_is_always_an_approval_even_where_schedule_is_auto(tmp_path):
    ctx = setup(tmp_path, cfg(), {"plan": PLAN}, auto=True)
    [ch] = schedule.schedule_producer(ctx, "w")
    assert ch.kind == "schedule_bundle" and ch.route_preference == ROUTE_APPROVAL
    assert set(ch.patch) == {"cohorts", "hour_weights"}


def test_refresh_is_auto_where_schedule_allowed_and_approval_otherwise(tmp_path):
    state = {"plan": PLAN, "applied": {"tables": {"a": {str(h): 1.1 for h in range(9, 21)}}}}
    live = cfg(PLAN["cohorts"], state["applied"]["tables"])
    [auto] = schedule.schedule_producer(setup(tmp_path, live, state, auto=True), "w")
    assert auto.kind == "schedule_refresh" and auto.route_preference == ROUTE_AUTO
    [appr] = schedule.schedule_producer(setup(tmp_path / "b", live, state, auto=False), "w")
    assert appr.route_preference == ROUTE_APPROVAL


def test_supervisor_schedule_is_never_overwritten(tmp_path):
    state = {"plan": PLAN, "applied": {"tables": {"a": HOURS}}}
    live = cfg(PLAN["cohorts"], {"a": {str(h): 2.0 for h in range(9, 21)}})  # someone else's table
    assert schedule.schedule_producer(setup(tmp_path, live, state, auto=True), "w") == []


def test_blocked_or_immaterial_plans_propose_nothing(tmp_path):
    blocked = dict(PLAN, blocked=True)
    tiny = dict(PLAN, estimate=dict(PLAN["estimate"], rel_lift=0.001))
    assert schedule.schedule_producer(setup(tmp_path, cfg(), {"plan": blocked}, True), "w") == []
    live = cfg(PLAN["cohorts"], {})
    assert schedule.schedule_producer(setup(tmp_path / "c", live, {"plan": tiny}, True), "w") == []  # refresh floor 0.5%


def test_rollback_drops_the_tables_we_applied(tmp_path):
    tables = {"a": HOURS, "default": HOURS}
    state = {"plan": PLAN, "applied": {"tables": tables, "status": schedule.ROLLBACK}}
    [ch] = schedule.schedule_producer(setup(tmp_path, cfg(PLAN["cohorts"], tables), state, True), "w")
    assert ch.kind == "schedule_rollback" and ch.patch == {"hour_weights": {"a": None, "default": None}}
