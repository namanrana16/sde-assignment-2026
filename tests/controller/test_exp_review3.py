"""Experiment review round 3: incumbents at entry, supervisor edits while pending, same-ingest apply, power check,
report-only interactions, sim tick, per-workflow isolation."""
import json

import pytest

from controller import exp_alloc, exp_interactions, experiments as ex, exp_state as xs, settings
from controller.desired import PlanContext
from controller.store import Store
from tests.controller.test_exp_review2 import DIM, WID, cfg, ctx_of, pend, state, wf_of  # noqa: F401
from tests.controller.test_exp_veto import DAY, add_calls, store  # noqa: F401


class FakeClient:
    def __init__(self, end_minute=30 * DAY, tick_minutes=15):
        self._s = {"end_minute": end_minute, "tick_minutes": tick_minutes}

    def state(self):
        return dict(self._s)


def ctx_with(store, now, client, **kw):
    return PlanContext(store, client, {WID: wf_of(**kw)}, now)


def cube_row(store, day, welcome, voice, connects, engaged, cell=0, hour=12):
    store.conn.execute("INSERT INTO cube(workflow_id, day, hour, cell, welcome, voice, caller_id, prompt, dials, connects,"
                       " engaged, flags, sum_adherence) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
                       (WID, day, hour, cell, welcome, voice, "D1", "P1", connects * 2, connects, engaged))


def decisions(store, kind):
    return [json.loads(r[0]) for r in store.conn.execute("select inputs from decisions where kind=? order by id", (kind,))]


# 1 ------------------------------------------------------------------------------------------------------------
def test_entry_keeps_an_incumbent_split_and_is_not_harmful(store):
    cfg(store, {"W1": 0.5, "W2": 0.5})
    c = ctx_of(store, DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    d = xs.load(store, WID, DIM)["desired"]
    w = d["weights"]
    assert w["W2"] >= 0.5 - 1e-9                       # the incumbent is never lowered
    assert w["W1"] >= settings.EXP_MIN_CONTROL_SHARE   # control floor respected
    assert w["W3"] == pytest.approx(settings.EXP_ENTRY_SHARE)  # the new challenger is carved from the top option only
    assert w["W1"] == pytest.approx(0.5 - settings.EXP_ENTRY_SHARE)
    effect = {"W1": 1.0, "W2": 1.2, "W3": 1.0}         # sim truth: expected engagement is linear in the arm mix
    old = 0.5 * effect["W1"] + 0.5 * effect["W2"]
    new = sum(p * effect[o] for o, p in w.items())
    assert (new - old) / old >= -0.01


def test_entry_with_a_full_control_floor_enters_no_new_challenger():
    live = {"W1": 0.45, "W2": 0.3, "W3": 0.25}
    shares, incumbents, untested = exp_alloc.entry_plan(live, "W1", ["W1", "W2", "W3", "W4"], [])
    assert shares == {"W2": 0.3, "W3": 0.25} and incumbents == shares and untested == ["W4"]
    s2, _, un2 = exp_alloc.entry_plan({"W1": 0.5, "W2": 0.5}, "W1", ["W1", "W2", "W3", "W4"], [])
    assert s2 == {"W2": 0.5, "W3": 0.075} and un2 == ["W4"]


def test_100_percent_default_entry_is_unchanged():
    shares, inc, un = exp_alloc.entry_plan({"W1": 1.0}, "W1", ["W1", "W2", "W3", "W4", "W5"], [])
    assert shares == exp_alloc.entry_shares(["W2", "W3", "W4"]) and inc == {} and un == ["W5"]


def test_ramp_never_squeezes_an_incumbent_below_its_entry_share():
    out = exp_alloc.next_shares({"W2": 0.5, "W3": 0.075}, ["W3"], [], [], floors={"W2": 0.5})
    assert out["W2"] == 0.5 and out["W3"] >= 0.075


# 2 ------------------------------------------------------------------------------------------------------------
def test_supervisor_edit_while_pending_blocks_reentry_for_the_cooldown(store):
    cfg(store, {"W1": 1.0}, version=1)
    c = ctx_of(store, DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    assert xs.load(store, WID, DIM)["desired"]  # entry filed, awaiting approval
    cfg(store, {"W1": 0.5, "W2": 0.5}, version=2, actor="supervisor")
    c = ctx_of(store, DAY + 30)
    ex._step(c, WID, DIM, c.workflows[WID])
    st = xs.load(store, WID, DIM)
    assert st["desired"] is None and st["not_before"] == DAY + 30 + settings.EXP_SUPERVISOR_COOLDOWN_MINUTES
    c = ctx_of(store, DAY + 600)
    ex._step(c, WID, DIM, c.workflows[WID])
    assert xs.load(store, WID, DIM)["desired"] is None
    assert decisions(store, "experiment_supervisor_edit_pending")
    c = ctx_of(store, DAY + 30 + settings.EXP_SUPERVISOR_COOLDOWN_MINUTES)
    ex._step(c, WID, DIM, c.workflows[WID])
    w = xs.load(store, WID, DIM)["desired"]["weights"]
    assert w["W2"] >= 0.5 - 1e-9


# 3 ------------------------------------------------------------------------------------------------------------
def test_applied_version_and_a_supervisor_edit_in_one_ingest(store):
    cfg(store, {"W1": 1.0}, version=1)
    c = ctx_of(store, DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    mine = xs.load(store, WID, DIM)["desired"]["weights"]
    cfg(store, mine, version=2, actor="controller")
    cfg(store, {"W1": 0.5, "W2": 0.5}, version=3, actor="supervisor")
    c = ctx_of(store, DAY + 60)
    ex._step(c, WID, DIM, c.workflows[WID])
    st = xs.load(store, WID, DIM)
    assert len(decisions(store, "experiment_applied")) == 1
    assert len(decisions(store, "experiment_interrupted")) == 1
    assert st["phase"] == xs.PENDING and st["desired"] is None and st["control"] in ("W1", "W2")


# 4 ------------------------------------------------------------------------------------------------------------
def test_entry_is_skipped_when_no_challenger_can_reach_a_test(store):
    cfg(store, {"W1": 1.0})
    for day in range(3):
        cube_row(store, day, "W1", "V1", 20, 8)  # ~4 dials/hour
    c = ctx_with(store, 3 * DAY, FakeClient(end_minute=10 * DAY))
    ex._step(c, WID, DIM, c.workflows[WID])
    st = xs.load(store, WID, DIM)
    assert st["desired"] is None and st["not_before"] == 3 * DAY + DAY
    assert decisions(store, "experiment_entry_skipped")
    assert not decisions(store, "experiment_entry")


def test_entry_proceeds_on_public_scale_volume(store):
    cfg(store, {"W1": 1.0})
    for day in range(3):
        cube_row(store, day, "W1", "V1", 4000, 1600)
    c = ctx_with(store, 3 * DAY, FakeClient(end_minute=10 * DAY))
    ex._step(c, WID, DIM, c.workflows[WID])
    assert xs.load(store, WID, DIM)["desired"]


def test_entry_is_skipped_when_cohort_overrides_cover_every_cell(store, monkeypatch):
    cfg(store, {"W1": 1.0})
    for day in range(3):
        cube_row(store, day, "W1", "V1", 4000, 1600, cell=5)
    monkeypatch.setattr(xs, "override_cells", lambda *a, **k: [5])
    c = ctx_with(store, 3 * DAY, FakeClient(end_minute=10 * DAY))
    ex._step(c, WID, DIM, c.workflows[WID])
    assert xs.load(store, WID, DIM)["desired"] is None and decisions(store, "experiment_entry_skipped")


# 5 ------------------------------------------------------------------------------------------------------------
def _look_world(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075})
    add_calls(store, "W2", 60, 0.5, 100, seed=1)
    add_calls(store, "W1", 60, 0.5, 100, seed=2, lid0=10_000)
    for day in range(3):
        for w in ("W1", "W2"):
            for v in ("V1", "V2"):
                cube_row(store, day, w, v, 200, 80 + (30 if (w, v) == ("W2", "V2") else 0))
    state(store, last_check=3 * DAY)


def _run_look(store):
    c = ctx_of(store, 3 * DAY + 1100)
    ex._step(c, WID, DIM, c.workflows[WID])
    st = xs.load(store, WID, DIM)
    return json.dumps({k: v for k, v in st.items() if k != "last_check"}, sort_keys=True)


def test_interactions_table_is_in_the_look_row_and_never_changes_decisions(store, tmp_path, monkeypatch):
    _look_world(store)
    with_it = _run_look(store)
    row = [d for d in decisions(store, "experiment_look") if d.get("scope") == "all"][0]
    inter = row["interactions"]
    assert len(inter["table"]) == 4 and inter["df"] == 1 and inter["chi2"] > 0
    cell = [t for t in inter["table"] if (t["welcome"], t["voice"]) == ("W2", "V2")][0]
    assert cell["connects"] == 600 and cell["engaged"] == 330 and cell["z"] > 0
    other = Store(tmp_path / "other" / "o.db")
    try:
        monkeypatch.setattr(exp_interactions, "table", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        _look_world(other)
        without = _run_look(other)
    finally:
        other.close()
    assert with_it == without


def test_interaction_statistic_is_zero_for_additive_data():
    cells = {("W1", "V1"): (100, 40), ("W1", "V2"): (100, 50), ("W2", "V1"): (100, 50), ("W2", "V2"): (100, 60)}
    out = exp_interactions.summarise(cells)
    assert out["chi2"] == pytest.approx(0.0, abs=1e-9)


# 6 ------------------------------------------------------------------------------------------------------------
def test_last_tick_look_uses_the_sim_tick(store):
    cfg(store, {"W1": 0.85, "W2": 0.075, "W3": 0.075})
    state(store, last_look_day=2, last_check=3 * DAY)
    client = FakeClient(tick_minutes=30)
    ex._step(ctx_with(store, 3 * DAY + DAY - 30, client, window=(9, 24)), WID, DIM, wf_of(window=(9, 24)))
    assert xs.load(store, WID, DIM)["last_look_day"] == 3


# 7 ------------------------------------------------------------------------------------------------------------
def test_one_failing_workflow_does_not_stop_the_others(store, monkeypatch):
    wfs = {"a": wf_of(), "b": wf_of()}
    seen = []

    def fake_step(ctx, wid, dim, wf):
        seen.append((wid, dim))
        if wid == "a":
            raise RuntimeError("boom")

    monkeypatch.setattr(ex, "_step", fake_step)
    ex.experiments_detector(PlanContext(store, None, wfs, DAY))
    ex.experiments_detector(PlanContext(store, None, wfs, DAY + 15))
    assert ("b", DIM) in seen
    assert store.conn.execute("select count(*) from alerts where code='experiments_workflow_error'").fetchone()[0] == 1
