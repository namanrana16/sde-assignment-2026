"""A8: a persistent submit failure backs off instead of resubmitting a fresh id every tick."""
from controller import client as c
from controller import proposal_repo as pr
from controller import settings
from controller.desired import ROUTE_AUTO, DesiredChange, Priority
from tests.controller.helpers import make_controller, run_until, uniform_hours


def test_persistent_422_on_approval_route_backs_off(client, store, world, monkeypatch):
    calls = []

    def bad(body):
        calls.append(body["proposal_id"])
        raise c.Unprocessable(422, "bad", "nope")

    monkeypatch.setattr(client, "request_approval", bad)
    ctl = make_controller(client, store, uniform_hours())
    run_until(ctl, lambda: client.state()["sim_minute"] >= 1300, limit=400)
    n = len(calls)
    assert n >= 1
    assert len(set(calls)) == n
    failed = [r[0] for r in store.conn.execute("select created_minute from proposals where state='SUBMIT_FAILED'")]
    assert all(b - a >= settings.SUBMIT_FAILED_BACKOFF_MIN for a, b in zip(failed, failed[1:]))
    assert store.conn.execute("select count(*) from alerts where code='submit_backoff'").fetchone()[0] >= 1


def test_auto_403_falls_back_to_approval_route(client, store, world, monkeypatch):
    auto = DesiredChange("nw_renewal", "did_spread", Priority.ROUTINE,
                         {"allocations": {"default": {"caller_id": {"D1": 0.4, "D2": 0.4, "D3": 0.2}}}},
                         route_preference=ROUTE_AUTO, summary="spread")
    puts = []

    def forbidden(wid, body):
        puts.append(body["proposal_id"])
        raise c.Forbidden(403, "no", "denied")

    monkeypatch.setattr(client, "put_config", forbidden)
    ctl = make_controller(client, store, lambda cx, wid: [auto] if wid == "nw_renewal" else [])
    run_until(ctl, lambda: client.state()["sim_minute"] >= 1300, limit=400)
    assert 1 <= len(puts) <= 3  # 4 h, then 8 h, then 16 h windows: not one per tick
    assert len(puts) == len(set(puts))
    routes = {r[0] for r in store.conn.execute("select route from proposals where state!='SUBMIT_FAILED'")}
    assert routes == {pr.ROUTE_APPROVAL}


def _incident_change():
    return DesiredChange("nw_renewal", "did_spread", Priority.INCIDENT,
                         {"allocations": {"default": {"caller_id": {"D1": 0.4, "D2": 0.4, "D3": 0.2}}}},
                         summary="evacuate")


def test_incident_approval_422_retries_on_short_fixed_backoff(client, store, world, monkeypatch):
    """D1: an incident is not blocked for hours (or doubling to a day) by a failed send."""
    def bad(body):
        raise c.Unprocessable(422, "bad", "nope")

    monkeypatch.setattr(client, "request_approval", bad)
    inc = _incident_change()
    ctl = make_controller(client, store, lambda cx, wid: [inc] if wid == "nw_renewal" else [])
    run_until(ctl, lambda: client.state()["sim_minute"] >= 1300, limit=400)
    failed = [r[0] for r in store.conn.execute(
        "select created_minute from proposals where state='SUBMIT_FAILED' order by created_minute")]
    gaps = [b - a for a, b in zip(failed, failed[1:])]
    assert len(failed) >= 4
    assert all(g >= settings.SUBMIT_FAILED_BACKOFF_URGENT_MIN for g in gaps)
    assert max(gaps) < settings.SUBMIT_FAILED_BACKOFF_MIN  # no 240 min / doubling window


def test_incident_auto_403_falls_back_to_approval_immediately(client, store, world, monkeypatch):
    puts = []

    def forbidden(wid, body):
        puts.append(body["proposal_id"])
        raise c.Forbidden(403, "no", "denied")

    monkeypatch.setattr(client, "put_config", forbidden)
    inc = _incident_change()
    inc = DesiredChange(inc.workflow, inc.kind, Priority.INCIDENT, inc.patch,
                        route_preference=ROUTE_AUTO, summary="evacuate")
    ctl = make_controller(client, store, lambda cx, wid: [inc] if wid == "nw_renewal" else [])
    run_until(ctl, lambda: client.state()["sim_minute"] >= 1300, limit=400)
    assert len(puts) == 1  # the failing auto route is never retried
    routes = {r[0] for r in store.conn.execute("select route from proposals where state!='SUBMIT_FAILED'")}
    assert pr.ROUTE_APPROVAL in routes
