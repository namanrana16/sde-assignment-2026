"""DID review round 2: no peerless promotion, health guard on every non-safety move, one probe per change,
small max_shift never stalls a safety evacuation."""
import pytest

from controller import settings
from controller.desired import ROUTE_APPROVAL, ROUTE_AUTO, PlanContext
from controller.detectors import did
from controller.detectors import did_eval as de
from controller.detectors import did_plan as dp
from controller.detectors.fatigue_curve import FatigueCurve

NOW = 5 * 1440 + 600


def _st(**kw):
    st = {"flagged": {}, "weak": {}, "tested": {}, "reprobe": {}, "pending": {}, "weak_days": {}}
    st.update(kw)
    return st


def _peer(did_, q, n=400):
    return de.PeerResult(did_, n, int(n * 0.3), n * 0.3, q, 0.0, 10.0, 10.0)


def _ev(options, peers=None):
    ev = de.Evaluation(NOW // 60, FatigueCurve.flat(), {d: 10.0 for d in options})
    ev.total_dials = {d: 500 for d in options}
    ev.first_hour = {d: 10 for d in options}
    ev.peers = peers or {}
    return ev


def _produce(store, cfg_alloc, options, st, ev, auto_shift=None):
    store.store_config_body("w", 1, "controller", {"allocations": {"default": {"caller_id": cfg_alloc}},
                                                   "cohorts": [], "prompt_version": "P1", "schedule": {}})
    wf = {"options": {"caller_id": options},
          "governance": {"auto_apply": {"enabled": auto_shift is not None, "allowed_dims": ["caller_id"],
                                        "max_shift": auto_shift or 0.0}}}
    ctx = PlanContext(store, None, {"w": wf}, NOW)
    did.MEMO[(str(store.path), "w")] = (NOW, ev, st)
    try:
        return did.did_producer(ctx, "w")
    finally:
        did.MEMO.pop((str(store.path), "w"), None)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DID", True)


# ---- 1 (reverted: HEAD status semantics kept); evacuation onto a peerless DID
def test_flagged_did_is_evacuated_onto_a_peerless_untested_did(store):
    ev = _ev(["D1", "D2"])
    st = _st(flagged={"D2": {"minute": NOW - 60, "q_est": 0.5}})
    out = _produce(store, {"D1": 0.5, "D2": 0.5}, ["D1", "D2"], st, ev)
    assert out and out[0].patch["allocations"]["default"]["caller_id"] == {"D1": pytest.approx(1.0)}


# ---- 2: health guard on every non-safety move, any route
def test_approval_routed_spread_onto_a_poor_tested_did_is_refused_and_logged(store):
    ev = _ev(["D1", "D2"], {"D1": _peer("D1", 1.0), "D2": _peer("D2", 0.5)})
    st = _st(tested={"D1": 1, "D2": 1})
    assert _produce(store, {"D1": 1.0}, ["D1", "D2"], st, ev) == []
    row = store.conn.execute("select kind, summary from decisions where kind='did_move_blocked'").fetchone()
    assert row and "D2" in row[1]


def test_approval_routed_spread_onto_an_equal_did_is_allowed(store):
    ev = _ev(["D1", "D2"], {"D1": _peer("D1", 1.0), "D2": _peer("D2", 1.0)})
    out = _produce(store, {"D1": 1.0}, ["D1", "D2"], _st(tested={"D1": 1, "D2": 1}), ev)
    assert len(out) == 1 and out[0].route_preference == ROUTE_APPROVAL


def test_unknown_did_quality_is_the_conservative_prior_not_one():
    ev = _ev(["D1", "D2"])
    h = dp.expected_health(ev, {"D2": 1.0}, {})
    assert h == pytest.approx(settings.DID_H_FLOOR)


# ---- 3: one probing DID per change
def test_one_untested_and_two_reprobes_only_the_untested_gains_traffic():
    status = {"D1": dp.TESTED, "D2": dp.UNTESTED, "D3": dp.REPROBE, "D4": dp.REPROBE}
    out = dp.target_allocation({"D1": 1.0}, status, 1.0)
    assert set(out) == {"D1", "D2"} and out["D2"] == pytest.approx(dp.probe_cap(1.0))


def test_two_reprobes_without_untested_only_one_gains_traffic():
    status = {"D1": dp.TESTED, "D3": dp.REPROBE, "D4": dp.REPROBE}
    out = dp.target_allocation({"D1": 1.0}, status, 1.0)
    assert set(out) == {"D1", "D3"} and out["D3"] <= dp.reprobe_cap(1.0) + 1e-9


def test_a_reprobe_already_holding_share_is_not_grown_by_a_new_probe():
    status = {"D1": dp.TESTED, "D2": dp.UNTESTED, "D3": dp.REPROBE}
    out = dp.target_allocation({"D1": 0.99, "D3": 0.005, "D2": 0.0}, status, 1.0)
    assert out["D3"] <= 0.0051


# ---- 4: small max_shift never stalls the evacuation
def test_tiny_max_shift_routes_a_flagged_evacuation_through_approval(store):
    ev = _ev(["D1", "D2"], {"D1": _peer("D1", 1.0)})
    st = _st(tested={"D1": 1}, flagged={"D2": {"minute": NOW - 60, "q_est": 0.5}})
    out = _produce(store, {"D1": 0.9, "D2": 0.1}, ["D1", "D2"], st, ev, auto_shift=0.005)
    assert len(out) == 1 and out[0].route_preference == ROUTE_APPROVAL
    assert out[0].patch["allocations"]["default"]["caller_id"].get("D2", 0.0) == pytest.approx(0.0)


def test_normal_max_shift_keeps_the_auto_route_for_evacuation(store):
    ev = _ev(["D1", "D2"], {"D1": _peer("D1", 1.0)})
    st = _st(tested={"D1": 1}, flagged={"D2": {"minute": NOW - 60, "q_est": 0.5}})
    out = _produce(store, {"D1": 0.5, "D2": 0.5}, ["D1", "D2"], st, ev, auto_shift=0.2)
    assert len(out) == 1 and out[0].route_preference == ROUTE_AUTO
