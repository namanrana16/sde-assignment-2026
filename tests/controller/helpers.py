"""Shared helpers for the governance integration tests (tests may use sim internals)."""
import json

from controller import faults
from controller.desired import ROUTE_APPROVAL, DesiredChange, Priority, Registry
from controller.loop import Controller

HOURS = {str(h): 1 for h in range(9, 21)}  # nw_renewal dials 9-21


class SimulatedCrash(BaseException):
    """Stands in for ``os._exit``: not an Exception, so no ``except Exception`` swallows it."""


def raise_crash(code):
    raise SimulatedCrash(code)


def arm_crash(monkeypatch, spec):
    monkeypatch.setenv("CRASH_AT", spec)
    faults.reset(raise_crash)


def disarm_crash(monkeypatch):
    monkeypatch.delenv("CRASH_AT", raising=False)
    faults.reset()


def uniform_hours(wid="nw_renewal", kind="daily_bundle", priority=Priority.ROUTINE,
                  route=ROUTE_APPROVAL, hours=HOURS):
    """A harmless producer: wants one uniform hour-weight table until it is in effect."""
    def producer(ctx, workflow_id):
        if workflow_id != wid:
            return []
        return [DesiredChange(wid, kind, priority, {"hour_weights": {"default": dict(hours)}},
                              route, {}, f"{kind}: uniform hour weights")]
    return producer


def registry_of(*producers):
    reg = Registry()
    for p in producers:
        reg.register_producer(p)
    return reg


def make_controller(client, store, *producers):
    ctl = Controller(client, store, registry=registry_of(*producers))
    ctl.startup()
    return ctl


def run_until(ctl, predicate, limit=600):
    for _ in range(limit):
        done, _ = ctl.tick()
        if predicate():
            return True
        if done:
            return False
    return False


def proposal_states(store):
    return {r[0]: r[1] for r in store.conn.execute(
        "select proposal_id, state from proposals order by plan_seq, proposal_id")}


def sim_kinds(world):
    with open(world.run_dir / "sim_log.jsonl") as fh:
        return [json.loads(line)["kind"] for line in fh]
