"""Experiment review round 2: harm-stop weights, auto eligibility, daily-look gate, cohort overrides, edge cases."""
import json
import random

import pytest

from controller import exp_data, exp_decay, exp_stats as es, experiments as ex, exp_state as xs, settings
from controller.cohorts import VOCAB_META_KEY, FeatureVocab
from controller.desired import PlanContext
from tests.controller.test_exp_veto import DAY, WID, add_calls, desired, store  # noqa: F401

DIM = "welcome_message"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EXPERIMENTS", True)


def wf_of(max_shift=0.2, window=(9, 18), options=("W1", "W2", "W3"), enabled=True):
    pol = {"enabled": enabled, "allowed_dims": [DIM]}
    if max_shift is not None:
        pol["max_shift"] = max_shift
    return {"workflow_id": WID, "objective": {"primary": "engagement_rate"}, "options": {DIM: list(options)},
            "dialling_window": list(window), "governance": {"auto_apply": pol}}


def ctx_of(store, now, **kw):
    return PlanContext(store, None, {WID: wf_of(**kw)}, now)


def cfg(store, dist, version=1, actor="controller", cohorts=(), extra_allocs=None):
    allocs = {"default": {DIM: dict(dist)}, **(extra_allocs or {})}
    store.store_config_body(WID, version, actor, {"allocations": allocs, "cohorts": list(cohorts)})
    return version


def state(store, **kw):
    st = {"phase": "running", "control": "W1", "options": ["W1", "W2", "W3"], "excluded": [], "note": "", "not_before": 0,
          "shares": {"W2": 0.075, "W3": 0.075}, "arms": {"W2": {"state": "active"}, "W3": {"state": "active"}},
          "family": ["W2", "W3"], "desired": None, "live": {"W1": 0.85, "W2": 0.075, "W3": 0.075}, "seen_version": 1,
          "start_minute": 0, "scopes": {"all": {"arm_t": {}, "looks": 0}}, "ramped": [], "untested": [], "groups": None,
          "last_look_day": -1, "data_since": 0, "last_check": -10 ** 9, "history": [], "live_cohorts": {}}
    st.update(kw)
    xs.save(store, WID, DIM, st)
    return st


def pend(kind, weights, severe=False, **kw):
    d = {"weights": weights, "cohort_weights": {}, "kind": kind, "summary": "s", "rationale": "r", "severe": severe,
         "then": "running", "evidence": {}, "impact": None, "prev": None}
    d.update(kw)
    return d


# 1 ------------------------------------------------------------------------------------------------------------
def test_harm_stop_weights_come_from_the_live_split_not_the_unapproved_ramp(store):
    now = 3 * DAY + 600
    add_calls(store, "W2", 400, 0.20, now - 600, seed=1)
    add_calls(store, "W1", 400, 0.60, now - 600, seed=2, lid0=10_000)
    add_calls(store, "W3", 400, 0.60, now - 600, seed=3, lid0=20_000)
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075})
    prev = {"shares": {"W2": 0.075, "W3": 0.075}, "arms": {"W2": {"state": "active"}, "W3": {"state": "active"}},
            "ramped": [], "untested": [], "family": ["W2", "W3"], "phase": "running"}
    state(store, shares={"W2": 0.075, "W3": 0.2}, ramped=["W3"],
          desired=pend("experiment_update", {"W1": 0.725, "W2": 0.075, "W3": 0.2}, prev=prev, evidence={"ramp": ["W3"]}))
    c = ctx_of(store, now)
    ex._step(c, WID, DIM, c.workflows[WID])
    st = xs.load(store, WID, DIM)
    d = st["desired"]
    assert d["kind"] == "experiment_harm_stop" and d["severe"]
    assert d["weights"] == {"W1": 0.925, "W3": 0.075}  # W3 stays at its LIVE 7.5%, W2 stopped, no unapproved ramp
    assert st["shares"] == {"W3": 0.075} and st["ramped"] == []
    assert store.conn.execute("select count(*) from decisions where kind='experiment_update_superseded'").fetchone()[0] == 1


# 2 ------------------------------------------------------------------------------------------------------------
def _rollback_state(store):
    cfg(store, {"W1": 0.5, "W2": 0.5})
    state(store, phase="promoted", shares={}, desired=pend("experiment_rollback", {"W1": 1.0}, severe=True, then="closed"))


@pytest.mark.parametrize("max_shift", [0, None])
def test_zero_or_missing_max_shift_is_not_auto_eligible(store, max_shift):
    _rollback_state(store)
    [ch] = ex.experiments_producer(ctx_of(store, DAY, max_shift=max_shift), WID)
    assert ch.route_preference == "approval"
    assert ch.patch["allocations"]["default"][DIM] == {"W1": 1.0}


def test_positive_max_shift_still_routes_auto_with_a_bounded_step(store):
    _rollback_state(store)
    [ch] = ex.experiments_producer(ctx_of(store, DAY, max_shift=0.2), WID)
    assert ch.route_preference == "auto"
    assert ch.patch["allocations"]["default"][DIM]["W1"] < 1.0


def test_negligible_auto_step_goes_through_approval(store):
    _rollback_state(store)
    [ch] = ex.experiments_producer(ctx_of(store, DAY, max_shift=0.001), WID)
    assert ch.route_preference == "approval"
    assert ch.patch["allocations"]["default"][DIM] == {"W1": 1.0}


# 3 ------------------------------------------------------------------------------------------------------------
def test_daily_look_happens_when_the_dialling_window_ends_at_24(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075})
    state(store, last_look_day=2, last_check=3 * DAY)
    last_tick = 3 * DAY + DAY - settings.TICK_MINUTES
    ex._step(ctx_of(store, last_tick - settings.TICK_MINUTES, window=(9, 24)), WID, DIM, wf_of(window=(9, 24)))
    assert xs.load(store, WID, DIM)["last_look_day"] == 2
    ex._step(ctx_of(store, last_tick, window=(9, 24)), WID, DIM, wf_of(window=(9, 24)))
    assert xs.load(store, WID, DIM)["last_look_day"] == 3
    ex._step(ctx_of(store, last_tick, window=(9, 24)), WID, DIM, wf_of(window=(9, 24)))  # exactly one look per day
    assert store.conn.execute("select count(*) from decisions where kind='experiment_look'").fetchone()[0] <= 1


# 4 ------------------------------------------------------------------------------------------------------------
def _vocab_world(store):
    vocab = FeatureVocab()
    hi = vocab.encode({"lang": "hi", "city_tier": "t1", "age_band": "a", "value_band": "m"})
    en = vocab.encode({"lang": "en", "city_tier": "t1", "age_band": "a", "value_band": "m"})
    store.set_meta(VOCAB_META_KEY, vocab.dump())
    for lid in range(40):
        store.conn.execute("INSERT INTO leads(lid, lead_id, person_id, workflow_id, cell, campaign_id, ts_minute)"
                           " VALUES (?,?,?,?,?,?,0)", (lid, f"l{lid}", f"p{lid}", WID, hi if lid < 20 else en, "c"))
    return hi, en


COHORTS = [{"id": "c_hi", "where": {"lang": ["hi"]}}]


def test_cohort_with_its_own_allocation_is_excluded_from_the_arm_contrast(store):
    hi, en = _vocab_world(store)
    cfg(store, {"W1": 0.85, "W2": 0.15}, cohorts=COHORTS, extra_allocs={"c_hi": {DIM: {"W1": 1.0}}})
    add_calls(store, "W1", 20, 0.9, 100, seed=1, lid0=0)   # control-only cohort: confounds control
    add_calls(store, "W1", 20, 0.5, 100, seed=2, lid0=20)
    add_calls(store, "W2", 20, 0.5, 100, seed=3, lid0=20)
    st = state(store)
    cells = ex._arm_cells(ctx_of(store, DAY), st, WID, DIM)
    data = exp_data.load_arms(store, WID, DIM, 0, DAY, cells)
    assert max(data["W1"].engaged) >= 20 and min(data["W1"].engaged) >= 20  # only the default cohort's leads
    assert all(lid >= 20 for lid in data["W1"].engaged)
    allc = exp_data.load_arms(store, WID, DIM, 0, DAY)
    assert any(lid < 20 for lid in allc["W1"].engaged)
    assert exp_data.load_arms(store, WID, DIM, 0, DAY, cells) == exp_data._load_arms_raw(store, WID, DIM, 0, DAY, cells)


def test_no_override_means_no_cell_filter(store):
    _vocab_world(store)
    cfg(store, {"W1": 0.85, "W2": 0.15}, cohorts=COHORTS)
    assert ex._arm_cells(ctx_of(store, DAY), state(store), WID, DIM) is None


def test_supervisor_edit_of_a_cohort_override_interrupts(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=1, cohorts=COHORTS)
    state(store)
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=2, actor="supervisor", cohorts=COHORTS,
        extra_allocs={"c_hi": {DIM: {"W2": 1.0}}})
    c = ctx_of(store, 3 * DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    after = xs.load(store, WID, DIM)
    assert after["phase"] == xs.PENDING and after["history"]
    assert store.conn.execute("select count(*) from decisions where kind='experiment_interrupted'").fetchone()[0] == 1


def test_controller_cohort_override_is_not_an_interrupt(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=1, cohorts=COHORTS)
    state(store, last_check=3 * DAY, last_look_day=3)
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=2, actor="controller", cohorts=COHORTS,
        extra_allocs={"c_hi": {DIM: {"W2": 1.0}}})
    c = ctx_of(store, 3 * DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    assert xs.load(store, WID, DIM)["phase"] == xs.RUNNING


# 5 ------------------------------------------------------------------------------------------------------------
def test_cusum_with_zero_expected_engagement_does_not_divide_by_zero():
    blocks = [(10.0, 0.0, 0.0, 0.0)] * 6
    out = exp_decay.cusum(blocks, (10.0, 0.0, 0.0, 0.0))
    assert out["alarm"] is False
    out = exp_decay.cusum(blocks)
    assert out["alarm"] is False


# 6 ------------------------------------------------------------------------------------------------------------
def test_contrast_is_bit_identical_for_any_lead_order():
    rng = random.Random(5)
    lids = list(range(300))
    a = {l: (3.0, float(rng.randint(0, 3))) for l in lids}
    c = {l: (3.0, float(rng.randint(0, 3))) for l in lids}
    ref = es.contrast_strata([(a, c)])
    for seed in range(6):
        order = lids[:]
        random.Random(seed).shuffle(order)
        a2, c2 = {l: a[l] for l in order}, {l: c[l] for l in reversed(order)}
        got = es.contrast_strata([(a2, c2)])
        assert got.z == ref.z and got.se == ref.se


# 7 ------------------------------------------------------------------------------------------------------------
def test_redefined_group_scope_is_reset(store, monkeypatch):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, cohorts=COHORTS)
    old = {"sig": "old", "groups": [{"name": "g0", "cohorts": ["c_old"], "cells": [1], "share": 0.5}]}
    st = state(store, groups=old, scopes={"all": {"arm_t": {}, "looks": 2}, "g0": {"arm_t": {"W2": 0.3}, "looks": 3}})
    new = [{"name": "g0", "cohorts": ["c_hi"], "cells": [2], "share": 0.5}]
    monkeypatch.setattr(xs, "analysis_groups", lambda *a, **k: new)
    ex._groups(ctx_of(store, DAY), st, WID, (1, {"cohorts": COHORTS}))
    assert st["scopes"]["g0"] == {"arm_t": {}, "looks": 0}
    assert st["scopes"]["all"]["looks"] == 2


# 8 ------------------------------------------------------------------------------------------------------------
def _unmirrored(store, version, ts):
    store.conn.execute("INSERT INTO config_versions(workflow_id, version, actor, ts_minute, body) VALUES (?,?,?,?,NULL)",
                       (WID, version, "supervisor", ts))


def test_watch_waits_at_a_young_unmirrored_row_then_examines_the_supervisor_edit(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=1)
    _unmirrored(store, 2, 3 * DAY)
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=3)
    st = state(store)
    c = ctx_of(store, 3 * DAY + 10)
    out = ex._watch(c, st, WID, DIM, (3, {"allocations": {"default": {DIM: st["live"]}}}))
    assert out["seen_version"] == 1  # did not advance past the unmirrored row
    store.store_config_body(WID, 2, "supervisor", {"allocations": {"default": {DIM: {"W1": 0.1, "W2": 0.9}}}, "cohorts": []})
    out = ex._watch(c, st, WID, DIM, (3, {"allocations": {"default": {DIM: st["live"]}}}))
    assert out["phase"] == xs.PENDING and out["history"]  # the late-mirrored supervisor edit is examined


def test_watch_skips_a_permanently_unmirrored_row_after_the_grace_bound(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=1)
    _unmirrored(store, 2, 3 * DAY)
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075}, version=3)
    st = state(store)
    c = ctx_of(store, 3 * DAY + ex.UNMIRRORED_GRACE_MINUTES + 1)
    out = ex._watch(c, st, WID, DIM, (3, {"allocations": {"default": {DIM: st["live"]}}}))
    assert out["seen_version"] == 3
    assert store.conn.execute("select count(*) from decisions where kind='experiment_watch_skipped'").fetchone()[0] == 1
