"""Floor fixes C: withheld changes restore state (C1), live-config reconciliation (C2), drop-veto hysteresis (C3),
non-vetoed dims survive a withhold (C4)."""
import pytest

from controller import experiments as ex, exp_reconcile, exp_rules as er, exp_state as xs, settings
from tests.controller.test_exp_veto import (DAY, WID, add_calls, ctx_at, desired, put_state, store)  # noqa: F401

DIM = "welcome_message"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EXPERIMENTS", True)


def _ctx(store, now):
    c = ctx_at(store, now)
    c.workflows[WID]["dialling_window"] = [9, 18]
    return c


def _cfg(store, shares, actor="controller", version=2):
    body = {"allocations": {"default": {DIM: {"W1": round(1 - sum(shares.values()), 6), **shares}}}, "cohorts": []}
    store.store_config_body(WID, version, actor, body)
    return version


def test_set_desired_keeps_the_snapshot_not_the_earlier_desired():
    st = {"desired": {"severe": True, "kind": "experiment_harm_stop"}}
    snap = {"shares": {"W2": 0.2}}
    ex._set_desired(st, {"W1": 1.0}, "experiment_update", "s", "r", prev=snap)
    assert st["desired"]["prev"] == snap and st["desired"]["severe"] is True


def test_withheld_drop_restores_arms_shares_and_ramped_then_next_look_reevaluates(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.55, now - 2 * DAY, seed=1)  # arm recovered: the drop is vetoed at apply
    add_calls(store, "W1", 300, 0.50, now - 2 * DAY, seed=2, lid0=10_000)
    st = put_state(store, None)
    st.update(ramped=["W2"])
    snap = ex._snapshot(st)
    st["shares"], st["arms"] = {}, {"W2": {"state": "dropped"}}  # what _daily_look leaves behind
    ex._set_desired(st, {"W1": 1.0}, "experiment_update", "drop W2", "r", prev=snap, extra={"ramp": [], "drop": ["W2"]})
    xs.save(store, WID, DIM, st)
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_update")
    after = xs.load(store, WID, DIM)
    assert after["desired"] is None and after["shares"] == {"W2": 0.2} and after["arms"] == {"W2": {"state": "active"}}
    assert after["ramped"] == ["W2"]


def test_lost_drop_is_reraised_when_live_still_has_the_dropped_arm(store):
    st = put_state(store, None)
    st.update(shares={}, arms={"W2": {"state": "dropped"}})
    lost = exp_reconcile.lost_change(st, DIM, {"W1": 0.8, "W2": 0.2})
    assert lost["drop"] == ["W2"] and lost["weights"] == {"W1": 1.0} and not lost["severe"]
    assert lost["prev"]["shares"] == {"W2": 0.2} and lost["prev"]["arms"]["W2"]["state"] == "active"


def test_reconcile_is_quiet_when_live_matches_or_a_change_is_in_flight(store):
    st = put_state(store, None)
    assert exp_reconcile.lost_change(st, DIM, {"W1": 0.8, "W2": 0.2}) is None
    st["shares"], st["arms"] = {}, {"W2": {"state": "dropped"}}
    assert exp_reconcile.lost_change(st, DIM, {"W1": 1.0}) is None
    st["desired"] = desired("experiment_update")
    assert exp_reconcile.lost_change(st, DIM, {"W1": 0.8, "W2": 0.2}) is None


def test_rejected_drop_is_refiled_at_the_next_look_via_reconcile(store):
    ver = _cfg(store, {"W2": 0.2}, version=1)
    st = put_state(store, None)
    st.update(last_check=3 * DAY, last_look_day=3, shares={}, arms={"W2": {"state": "dropped"}}, seen_version=ver, live={"W1": 0.8, "W2": 0.2})
    xs.save(store, WID, DIM, st)
    c = _ctx(store, 3 * DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    d = xs.load(store, WID, DIM)["desired"]
    assert d and d["kind"] == "experiment_update" and d["evidence"]["drop"] == ["W2"] and d["weights"] == {"W1": 1.0}
    assert store.conn.execute("select count(*) from decisions where kind='experiment_reconcile'").fetchone()[0] == 1


def test_supervisor_edit_is_still_an_interruption_not_a_reconcile(store):
    st = put_state(store, None)
    st.update(seen_version=1, live={"W1": 0.8, "W2": 0.2}, shares={"W2": 0.2})
    xs.save(store, WID, DIM, st)
    _cfg(store, {"W2": 0.6}, actor="supervisor", version=2)
    c = _ctx(store, 3 * DAY)
    ex._step(c, WID, DIM, c.workflows[WID])
    after = xs.load(store, WID, DIM)
    assert after["phase"] == xs.PENDING and after["control"] == "W2" and after["history"]
    assert store.conn.execute("select count(*) from decisions where kind='experiment_reconcile'").fetchone()[0] == 0


def test_vetoed_reconcile_falls_back_to_the_live_state_not_the_diverged_one(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.56, now - 2 * DAY, seed=1)
    add_calls(store, "W1", 300, 0.50, now - 2 * DAY, seed=2, lid0=10_000)
    ver = _cfg(store, {"W2": 0.2}, version=1)
    st = put_state(store, None)
    st.update(last_check=3 * DAY, last_look_day=3, shares={}, arms={"W2": {"state": "dropped"}}, ramped=["W2"], seen_version=ver, live={"W1": 0.8, "W2": 0.2})
    xs.save(store, WID, DIM, st)
    c = _ctx(store, now)
    ex._step(c, WID, DIM, c.workflows[WID])
    assert ex.experiments_veto(c, WID, "experiment_update")
    after = xs.load(store, WID, DIM)
    assert after["shares"] == {"W2": 0.2} and after["arms"]["W2"]["state"] == "active" and after["desired"] is None


def _row(z, recent_z, n=400, recent_n=200):
    return {"ok": True, "z": z, "n_arm": n, "recent_n": recent_n, "recent_z": recent_z}


def test_drop_veto_has_hysteresis_on_the_recent_window():
    assert er.drop_holds(_row(-0.2, -1.69), True) and not er.drop_holds(_row(-0.2, -1.31), True)  # flips at the bound
    assert not er.drop_vetoed(_row(-0.2, -1.31), True)  # ... but is not vetoed at apply
    assert er.drop_vetoed(_row(-0.2, -0.4), True)  # recovered above EXP_DROP_VETO_RECOVERED_Z


def test_drop_veto_has_a_margin_on_the_cumulative_z():
    thr = er.drop_threshold(400)
    assert not er.drop_vetoed(_row(thr + 0.3, 0.0), False)
    assert er.drop_vetoed(_row(thr + 0.8, 0.0), False)


def test_nonvetoed_dim_change_is_refiled_after_a_withhold(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.55, now - 2 * DAY, seed=1)  # welcome drop vetoed (recovered)
    add_calls(store, "W1", 300, 0.50, now - 2 * DAY, seed=2, lid0=10_000)
    st = put_state(store, None)
    snap = ex._snapshot(st)
    st["shares"], st["arms"] = {}, {"W2": {"state": "dropped"}}
    ex._set_desired(st, {"W1": 1.0}, "experiment_update", "drop W2", "r", prev=snap, extra={"ramp": [], "drop": ["W2"]})
    xs.save(store, WID, DIM, st)
    other = {"phase": "running", "control": "V1", "options": ["V1", "V2"], "excluded": [], "shares": {}, "arms": {"V2": {"state": "dropped"}},
             "family": ["V2"], "start_minute": 0, "desired": None, "ramped": [], "untested": [], "history": [], "scopes": {}}
    other["desired"] = {**desired("experiment_update", weights={"V1": 1.0}, evidence={"ramp": [], "drop": []}), "prev": None}
    xs.save(store, WID, "voice", other)
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_update")
    # the vetoed dim's state is restored; the other dim's desired change is untouched and still emitted next tick
    assert xs.load(store, WID, "voice")["desired"]["kind"] == "experiment_update"


def test_risk_reducing_bundle_is_not_withheld_for_another_dims_stale_update(store):
    now = 3 * DAY
    add_calls(store, "W2", 300, 0.55, now - 2 * DAY, seed=1)  # welcome drop would be vetoed
    add_calls(store, "W1", 300, 0.50, now - 2 * DAY, seed=2, lid0=10_000)
    st = put_state(store, None)
    snap = ex._snapshot(st)
    st["shares"], st["arms"] = {}, {"W2": {"state": "dropped"}}
    ex._set_desired(st, {"W1": 1.0}, "experiment_update", "drop W2", "r", prev=snap, extra={"ramp": [], "drop": ["W2"]})
    xs.save(store, WID, DIM, st)
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_harm_stop") is None
    assert xs.load(store, WID, DIM)["desired"]["kind"] == "experiment_update"  # untouched, applies with the bundle
    assert store.conn.execute("select count(*) from decisions where kind='experiment_stale_part_applied'").fetchone()[0] == 1
    assert ex.experiments_veto(ctx_at(store, now), WID, "experiment_update")  # a routine bundle is still withheld


def test_ramp_veto_and_look_gate_see_a_bad_recent_window_on_a_thin_arm():
    """C5: a 7.5% arm has ~85 recent connects/day (< EXP_RAMP_MIN_CONNECTS): its decay must still block/veto a ramp."""
    thin_bad = {"ok": True, "z": 0.77, "lift": 0.04, "n_arm": 269, "recent_n": 86, "recent_z": -1.70}
    assert er.ramp_vetoed(thin_bad)
    assert er.ramp_blocked(thin_bad)
    assert not er.ramp_blocked({**thin_bad, "recent_z": -0.4})
    assert not er.ramp_blocked({**thin_bad, "recent_n": 30})  # too few recent connects to call it


def test_daily_look_does_not_ramp_an_arm_whose_recent_window_is_bad():
    from controller.exp_rules import ArmData, daily_look
    import random
    rng = random.Random(3)

    def rows(n, rate, lid0):
        return {lid0 + i: (3, sum(rng.random() < rate for _ in range(3))) for i in range(n)}
    arm = ArmData(engaged=rows(150, 0.55, 0), recent=rows(40, 0.20, 0), pickup={})
    ctl = ArmData(engaged=rows(600, 0.50, 10_000), recent=rows(150, 0.50, 10_000), pickup={})
    out = daily_look({"W1": ctl, "W2": arm}, "W1", ["W2"], 1, 0.05, arm_t={}, looks_taken=0, ramped=[],
                     exposure_days=2.0, max_drop=0.02, test_winner=False)
    assert "W2" not in out.ramp
