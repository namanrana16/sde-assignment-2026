"""B3: one workflow's deterministic exception must not crash the tick or hide other workflows' changes."""
import pytest

from controller.desired import ROUTE_APPROVAL, DesiredChange, PlanContext, Priority, Registry
from controller.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _ctx(store, now=0):
    return PlanContext(store=store, client=None, workflows={"a": {}, "b": {}}, now=now)


def _change(wid):
    return DesiredChange(wid, "daily_bundle", Priority.ROUTINE, {}, ROUTE_APPROVAL, {}, "x")


def _alerts(store, code):
    return store.conn.execute("select workflow_id from alerts where code=?", (code,)).fetchall()


def test_raising_producer_yields_nothing_for_that_workflow_only_and_alerts_once_per_day(store):
    reg = Registry()

    def bad(ctx, wid):
        if wid == "a":
            raise KeyError("one option")
        return [_change(wid)]

    reg.register_producer(bad)
    ctx = _ctx(store)
    assert reg.collect(ctx, "a") == []
    assert [c.workflow for c in reg.collect(ctx, "b")] == ["b"]
    reg.collect(ctx, "a")  # same sim day: deduped
    assert _alerts(store, "producer_failed") == [("a",)]
    assert store.conn.execute("select count(*) from decisions where kind='producer_failed'").fetchone()[0] == 1
    reg.collect(_ctx(store, 1440), "a")
    assert len(_alerts(store, "producer_failed")) == 2


def test_raising_detector_does_not_stop_later_detectors(store):
    reg, ran = Registry(), []

    def bad(ctx):
        raise ZeroDivisionError

    reg.register_detector(bad)
    reg.register_detector(lambda ctx: ran.append(1))
    reg.run_detectors(_ctx(store))
    assert ran == [1] and len(_alerts(store, "detector_failed")) == 1


def test_base_exceptions_are_not_swallowed(store):
    reg = Registry()

    def stop(ctx):
        raise KeyboardInterrupt

    reg.register_detector(stop)
    with pytest.raises(KeyboardInterrupt):
        reg.run_detectors(_ctx(store))


def test_failing_veto_blocks_routine_but_never_a_safety_fix(store):
    reg = Registry()

    def bad(ctx, wid, kind):
        raise IndexError

    reg.register_veto(bad)
    ctx = _ctx(store)
    assert "failed" in reg.veto(ctx, "a", "experiment_promote")
    assert reg.veto(ctx, "a", "did_evacuation") is None
    assert reg.veto(ctx, "a", "experiment_harm_stop") is None
    assert reg.veto(ctx, "a", "halluc_fix") is None
