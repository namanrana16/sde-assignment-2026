"""B1: the caller-ID producer never silently overrides a supervisor's caller_id allocation."""
import json
from types import SimpleNamespace

import pytest

from controller import settings
from controller.desired import ROUTE_APPROVAL, PlanContext
from controller.detectors import did
from controller.detectors.did_owner import last_editor
from controller.store import Store

WID = "wf"
DAY = 1440


def _cfg(alloc):
    return {"allocations": {"default": {"caller_id": alloc}}, "cohorts": []}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DID", True)
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _versions(store, rows):
    for version, actor, ts, alloc in rows:
        store.upsert_config_event(WID, version, actor, ts, ["caller_id"], None, None)
        store.store_config_body(WID, version, actor, _cfg(alloc))


def _ctx(store, now):
    wf = {"options": {"caller_id": ["D1", "D2", "D3"]},
          "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.2}}}
    return PlanContext(store=store, client=None, workflows={WID: wf}, now=now)


def _fake_eval(monkeypatch, tested=(), flagged=()):
    ev = SimpleNamespace(curve=SimpleNamespace(h=lambda load: 1.0), loads_now={}, shares={}, share_dials=0,
                         first_hour={}, total_dials={}, peers={}, cur_hour=0)
    st = {"flagged": {d: {"minute": 0, "q_est": 0.3} for d in flagged}, "weak": {},
          "tested": {d: 0 for d in tested}, "reprobe": {}, "pending": {}, "weak_days": {}}
    monkeypatch.setattr(did, "_evaluate", lambda ctx, wid: (ev, st))


def test_last_editor_finds_supervisor_then_controller(store):
    _versions(store, [(1, "business_default", None, {"D1": 1.0}), (2, "supervisor", 500, {"D1": 0.8, "D2": 0.2}),
                      (3, "controller_auto", 900, {"D1": 0.8, "D2": 0.2})])  # v3 did not change caller_id
    ed = last_editor(store, WID, "default")
    assert ed["actor"] == "supervisor" and ed["version"] == 2 and ed["minute"] == 500
    _versions(store, [(4, "controller_auto", 1000, {"D1": 0.5, "D2": 0.5})])
    assert last_editor(store, WID, "default")["actor"] == "controller_auto"


def test_supervisor_edit_then_tested_dids_yields_no_change(store, monkeypatch):
    _versions(store, [(1, "business_default", None, {"D1": 1.0}),
                      (2, "supervisor", 500, {"D1": 0.7, "D2": 0.3})])
    _fake_eval(monkeypatch, tested=("D1", "D2", "D3"))
    ctx = _ctx(store, 2 * DAY)
    assert did.did_producer(ctx, WID) == []
    assert store.conn.execute("select count(*) from decisions where kind='did_supervisor_hold'").fetchone()[0] == 1
    did.did_producer(ctx, WID)  # deduped per sim day
    assert store.conn.execute("select count(*) from decisions where kind='did_supervisor_hold'").fetchone()[0] == 1


def test_controller_owned_allocation_still_spreads(store, monkeypatch):
    _versions(store, [(1, "business_default", None, {"D1": 1.0}),
                      (2, "controller_auto", 500, {"D1": 0.9, "D2": 0.1})])
    _fake_eval(monkeypatch, tested=("D1", "D2", "D3"))
    assert did.did_producer(_ctx(store, 2 * DAY), WID)


def test_supervisor_edit_then_flagged_did_evacuates_via_approval_naming_the_edit(store, monkeypatch):
    _versions(store, [(1, "business_default", None, {"D1": 1.0}),
                      (2, "supervisor", 500, {"D1": 0.5, "D2": 0.3, "D3": 0.2})])
    _fake_eval(monkeypatch, tested=("D1", "D3"), flagged=("D2",))
    changes = did.did_producer(_ctx(store, 2 * DAY), WID)
    assert len(changes) == 1
    ch = changes[0]
    assert ch.route_preference == ROUTE_APPROVAL and ch.kind == "did_evacuation"
    step = ch.patch["allocations"]["default"]["caller_id"]
    assert "D2" not in step and step["D1"] > 0.5 and step["D3"] > 0.2  # supervisor's other shares are kept in proportion
    assert "supervisor" in ch.summary and "v2" in ch.summary and "Day 0" in ch.summary
    assert json.dumps(ch.evidence)  # evidence names the edit
    assert ch.evidence["supervisor_edit"]["version"] == 2


def test_our_evacuation_of_a_flagged_did_keeps_supervisor_ownership(store, monkeypatch):
    _versions(store, [(1, "business_default", None, {"D1": 1.0}),
                      (2, "supervisor", 500, {"D1": 0.5, "D2": 0.3, "D3": 0.2}),
                      (3, "controller_approved", 900, {"D1": 0.714, "D3": 0.286})])
    assert last_editor(store, WID, "default")["actor"] == "supervisor"
    _fake_eval(monkeypatch, tested=("D1", "D3"))
    assert did.did_producer(_ctx(store, 2 * DAY), WID) == []
