"""Apply-time veto of ramp / drop / promotion stages, stepped rollback, and the post-edit sample floor (units on a store)."""
import json
import random

import pytest

from controller import experiments as ex, exp_state as xs, settings
from controller.desired import PlanContext
from controller.store import Store

WID = "w"
DAY = 1440


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EXPERIMENTS", True)


def add_calls(store, arm, n_leads, rate, minute, welcome_col=True, seed=0, lid0=0, version=1, per_lead=3):
    rng = random.Random(seed)
    base = store.count("calls")
    for i in range(n_leads):
        for j in range(per_lead):
            store.conn.execute(
                "INSERT INTO calls(event_id, workflow_id, ts_minute, day, hour, lid, attempt_no, config_version, cohort_id,"
                " welcome, voice, caller_id, prompt, connected, engaged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (base + i * per_lead + j + 1, WID, minute + j, (minute + j) // DAY, 12, lid0 + i, 1, version, "default",
                 arm, "V1", "D1", "P1", 1, int(rng.random() < rate)))


def ctx_at(store, now, desired_kind_wf=None):
    wf = {"workflow_id": WID, "objective": {"primary": "engagement_rate"}, "options": {"welcome_message": ["W1", "W2"]},
          "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["welcome_message"], "max_shift": 0.2}}}
    return PlanContext(store, None, {WID: wf}, now)


def put_state(store, desired, promo=None, phase="running"):
    st = {"phase": phase, "control": "W1", "options": ["W1", "W2"], "excluded": [], "shares": {"W2": 0.2}, "arms": {"W2": {"state": "active"}},
          "family": ["W2"], "start_minute": 0, "desired": desired, "ramped": [], "untested": [], "history": [], "scopes": {},
          "pre_promo": {"shares": {"W2": 0.075}, "arms": {"W2": {"state": "active"}}}}
    if promo:
        st["promo"] = promo
    xs.save(store, WID, "welcome_message", st)
    return st


def desired(kind, **kw):
    d = {"weights": {"W2": 0.5, "W1": 0.5}, "cohort_weights": {}, "kind": kind, "summary": "s", "rationale": "r", "severe": False,
         "then": "promoted", "evidence": {}, "impact": None, "prev": {"shares": {"W2": 0.075}, "arms": {"W2": {"state": "active"}},
                                                                    "ramped": [], "untested": [], "family": ["W2"], "phase": "running"}}
    d.update(kw)
    return d


PROMO = {"winner": "W2", "old_control": "W1", "scope": "workflow", "cells": None, "cohorts": None, "stage": 1}


def recent_calls(store, winner_rate, control_rate, now):
    add_calls(store, "W2", 400, winner_rate, now - 600, seed=1)
    add_calls(store, "W1", 400, control_rate, now - 600, seed=2, lid0=10_000)


def test_promotion_stage_is_withheld_without_a_positive_lower_bound(store):
    now = 3 * DAY
    add_calls(store, "W2", 60, 0.50, now - 600, seed=1)  # no real lift on a thin window: lb80 not above 0
    add_calls(store, "W1", 60, 0.50, now - 600, seed=2, lid0=10_000)
    put_state(store, desired("experiment_promote"), PROMO)
    reason = ex.experiments_veto(ctx_at(store, now), WID, "experiment_promote")
    assert reason and "80% lower bound" in reason
    st = xs.load(store, WID, "welcome_message")
    assert st["desired"] is None and st["phase"] == "running" and "promo" not in st and st["shares"] == {"W2": 0.075}
    assert store.conn.execute("select count(*) from decisions where kind='experiment_apply_withheld'").fetchone()[0] == 1


def test_clearly_better_winner_passes_the_veto(store):
    now = 3 * DAY
    recent_calls(store, 0.62, 0.48, now)
    put_state(store, desired("experiment_promote"), PROMO)
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_promote") is None
    assert xs.load(store, WID, "welcome_message")["desired"]["kind"] == "experiment_promote"


def test_stage_two_withheld_returns_to_stage_one_without_losing_the_promotion(store):
    now = 3 * DAY
    recent_calls(store, 0.45, 0.50, now)
    put_state(store, desired("experiment_stage2", weights={"W2": 0.9, "W1": 0.1}), dict(PROMO), phase="promoted")
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_stage2")
    st = xs.load(store, WID, "welcome_message")
    assert st["desired"] is None and st["phase"] == "promoted" and st["promo"]["stage"] == 1 and st["promo"]["stage2_withheld"] == now


def test_ramp_withheld_when_the_arm_has_gone_bad_and_applied_when_fine(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.40, now - 2 * DAY, seed=1, per_lead=3)
    add_calls(store, "W1", 300, 0.55, now - 2 * DAY, seed=2, lid0=10_000)
    d = desired("experiment_update", weights={"W2": 0.2, "W1": 0.8}, evidence={"ramp": ["W2"], "drop": []})
    put_state(store, d)
    reason = ex.experiments_veto(ctx_at(store, now), WID, "experiment_update")
    assert reason and "ramp W2" in reason
    assert xs.load(store, WID, "welcome_message")["shares"] == {"W2": 0.075}


def test_drop_withheld_when_the_arm_recovered(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.55, now - 2 * DAY, seed=1)
    add_calls(store, "W1", 300, 0.50, now - 2 * DAY, seed=2, lid0=10_000)
    d = desired("experiment_update", weights={"W1": 1.0}, evidence={"ramp": [], "drop": ["W2"]})
    put_state(store, d)
    reason = ex.experiments_veto(ctx_at(store, now), WID, "experiment_update")
    assert reason and "drop W2" in reason


def test_drop_goes_ahead_when_still_bad_and_harm_stop_or_rollback_is_never_vetoed(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.40, now - 2 * DAY, seed=1)
    add_calls(store, "W1", 300, 0.55, now - 2 * DAY, seed=2, lid0=10_000)
    put_state(store, desired("experiment_update", weights={"W1": 1.0}, evidence={"ramp": [], "drop": ["W2"]}))
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_update") is None
    put_state(store, desired("experiment_rollback", weights={"W1": 1.0}, severe=True), PROMO, phase="promoted")
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_rollback") is None
    assert ex.experiments_veto(ctx_at(store, now), WID, "did_rebalance") is None


def test_a_promotion_superseded_by_a_rollback_is_never_applied(store):
    now = 3 * DAY
    recent_calls(store, 0.6, 0.45, now)
    put_state(store, desired("experiment_rollback", weights={"W1": 1.0}, severe=True), PROMO, phase="promoted")
    reason = ex.experiments_veto(ctx_at(store, now), WID, "experiment_stage2")
    assert reason and "no longer wanted" in reason


def test_rollback_steps_back_within_max_shift_when_auto_applied():
    cfg = {"allocations": {"default": {"welcome_message": {"W2": 0.5, "W1": 0.5}}}}
    d = {"weights": {"W1": 1.0}, "cohort_weights": {}}
    patch = ex.stepped_patch(cfg, "welcome_message", d, 0.2)
    step = patch["allocations"]["default"]["welcome_message"]
    assert step["W2"] == pytest.approx(0.31, abs=1e-6) and step["W1"] == pytest.approx(0.69, abs=1e-6)  # 0.19 < 0.2
    full = ex.stepped_patch(cfg, "welcome_message", d, None)
    assert full["allocations"]["default"]["welcome_message"] == {"W1": 1.0}
    last = ex.stepped_patch({"allocations": {"default": {"welcome_message": {"W2": 0.1, "W1": 0.9}}}}, "welcome_message", d, 0.2)
    assert last["allocations"]["default"]["welcome_message"] == {"W1": 1.0}


def test_start_ignores_pre_edit_calls_after_a_supervisor_interruption(store):
    add_calls(store, "W2", 50, 0.5, 100, seed=1)  # before the edit
    add_calls(store, "W2", 50, 0.5, 2 * DAY + 30, seed=2, lid0=1000)  # after the edit
    st = {"start_minute": None, "family": ["W2"], "data_since": 2 * DAY}
    c = ctx_at(store, 3 * DAY)
    assert ex._start(c, st, WID, "welcome_message") == 2 * DAY + 30
    st2 = {"start_minute": None, "family": ["W2"]}
    assert ex._start(c, st2, WID, "welcome_message") == 100


def test_interruption_does_not_re_enter_an_arm_that_was_behind_the_new_control(store):
    add_calls(store, "W2", 200, 0.40, 100, seed=1)  # old arm W2 clearly below the new control W3
    add_calls(store, "W3", 200, 0.52, 100, seed=2, lid0=10_000)
    add_calls(store, "W4", 10, 0.5, 100, seed=3, lid0=20_000)  # thin arm: stays eligible
    st = {"start_minute": 100, "options": ["W1", "W2", "W3", "W4"]}
    assert ex._below_new_control(ctx_at(store, 3 * DAY), st, WID, "welcome_message", "W3", []) == ["W2"]
    assert ex._below_new_control(ctx_at(store, 3 * DAY), st, WID, "welcome_message", "W3", ["W2"]) == []
    assert ex._below_new_control(ctx_at(store, 3 * DAY), {"start_minute": None, "options": ["W2"]}, WID,
                                 "welcome_message", "W3", []) == []


def promoted_state(store, stage, minute):
    promo = dict(PROMO, stage=stage, minute=minute - DAY, stage_minute=minute, seq={"start": minute, "ts": []})
    st = put_state(store, None, promo, phase="promoted")
    st["start_minute"] = 60
    xs.save(store, WID, "welcome_message", st)
    return st


def add_ticked(store, arm, rate, minute, n_leads=500, seed=0, lid0=0, version=1):
    add_calls(store, arm, n_leads, rate, minute, seed=seed, lid0=lid0, version=version)


def test_stage_one_winner_that_holds_for_a_day_is_proposed_for_stage_two(store):
    now = 4 * DAY
    stage_min = now - DAY - 60
    promoted_state(store, 1, stage_min)
    add_ticked(store, "W2", 0.60, now - 800, seed=1)
    add_ticked(store, "W1", 0.48, now - 800, seed=2, lid0=10_000)
    st = xs.load(store, WID, "welcome_message")
    ex._decay_check(ctx_at(store, now), st, WID, "welcome_message")
    d = st["desired"]
    assert d and d["kind"] == "experiment_stage2" and d["weights"] == {"W2": 0.9, "W1": 0.1} and not d["severe"]


def test_stage_one_does_not_advance_before_24_hours(store):
    now = 4 * DAY
    stage_min = now - 600
    promoted_state(store, 1, stage_min)
    add_ticked(store, "W2", 0.60, stage_min + 30, seed=1)
    add_ticked(store, "W1", 0.48, stage_min + 30, seed=2, lid0=10_000)
    st = xs.load(store, WID, "welcome_message")
    ex._decay_check(ctx_at(store, now), st, WID, "welcome_message")
    assert st["desired"] is None


def test_a_winner_that_turned_worse_than_its_control_is_rolled_back_severely(store):
    now = 4 * DAY
    stage_min = now - DAY
    promoted_state(store, 1, stage_min)
    add_ticked(store, "W2", 0.40, stage_min + 30, n_leads=700, seed=1)
    add_ticked(store, "W1", 0.55, stage_min + 30, n_leads=700, seed=2, lid0=10_000)
    st = xs.load(store, WID, "welcome_message")
    ex._decay_check(ctx_at(store, now), st, WID, "welcome_message")
    d = st["desired"]
    assert d and d["kind"] == "experiment_rollback" and d["severe"] and d["weights"] == {"W1": 1.0}
    assert "sequential" in json.dumps(st["decay_status"])


def test_a_rollback_replaces_a_pending_stage_two_request(store):
    now = 4 * DAY
    stage_min = now - DAY
    st = promoted_state(store, 1, stage_min)
    st["desired"] = desired("experiment_stage2", weights={"W2": 0.9, "W1": 0.1})
    add_ticked(store, "W2", 0.40, stage_min + 30, n_leads=700, seed=1)
    add_ticked(store, "W1", 0.55, stage_min + 30, n_leads=700, seed=2, lid0=10_000)
    ex._decay_check(ctx_at(store, now), st, WID, "welcome_message")
    assert st["desired"]["kind"] == "experiment_rollback"


def test_on_applied_records_stage_and_restarts_the_monitoring_epoch(store):
    st = put_state(store, desired("experiment_stage2", weights={"W2": 0.9, "W1": 0.1}), dict(PROMO, minute=100, stage_minute=100), phase="promoted")
    ex._on_applied(ctx_at(store, 5 * DAY), st, WID, "welcome_message", 4000)
    assert st["promo"]["stage"] == 2 and st["promo"]["stage_minute"] == 4000 and st["promo"]["minute"] == 100
    assert st["promo"]["seq"] == {"start": 4000, "ts": []} and st["desired"] is None
    st2 = put_state(store, desired("experiment_promote"), {"winner": "W2", "old_control": "W1"}, phase="running")
    ex._on_applied(ctx_at(store, 5 * DAY), st2, WID, "welcome_message", 5000)
    assert st2["promo"]["stage"] == 1 and st2["promo"]["minute"] == 5000
