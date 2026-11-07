"""Task: the daily per-person analysis is wired into the tick, analysis only, and can never stop the loop."""
from controller import loop, person_analysis, settings
from controller.desired import Registry
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client

DAY = settings.MINUTES_PER_DAY


def _controller(tmp_path):
    world = build(tmp_path, ("ks_cancel",), days=2, history_days=3)
    client, http = make_client(world)
    store = Store(tmp_path / "s.db")
    ctl = Controller(client, store, registry=Registry())
    ctl.startup()
    return ctl, store, http


def test_tick_calls_daily_analysis_every_tick_when_flag_on(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(person_analysis, "maybe_run_daily", lambda s, now: calls.append(now))
    ctl, store, http = _controller(tmp_path)
    for _ in range(3):
        ctl.tick()
    assert len(calls) == 3  # the once-per-day gate lives inside maybe_run_daily
    store.close()
    http.__exit__(None, None, None)


def test_real_job_runs_once_per_day_through_the_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PERSON_ANALYSIS", True)
    ctl, store, http = _controller(tmp_path)
    for _ in range(6):
        ctl.tick()
    n = store.conn.execute("select count(*) from decisions where kind='person_analysis'").fetchone()[0]
    assert n == 1
    store.close()
    http.__exit__(None, None, None)


def test_exception_in_analysis_is_logged_and_tick_continues(tmp_path, monkeypatch):
    def boom(store, now):
        raise RuntimeError("analysis exploded")
    monkeypatch.setattr(person_analysis, "maybe_run_daily", boom)
    ctl, store, http = _controller(tmp_path)
    before = ctl.client.state()["sim_minute"]
    done, _ = ctl.tick()
    assert ctl.client.state()["sim_minute"] > before  # the tick still advanced
    row = store.conn.execute("select message from alerts where code='person_analysis_failed'").fetchone()
    assert row and "analysis exploded" in row[0]
    assert store.count("proposals") == 0
    store.close()
    http.__exit__(None, None, None)
