import json

import httpx
import pytest

from controller.desired import Registry
from controller import cube, settings
from controller.client import AdvanceNotApplied, SimClient
from controller.ingest import Ingestor, mirror_configs, ts_to_minute
from controller.loop import Controller, Windows, choose_step
from controller.__main__ import main as cli_main
from controller.store import Store


def snapshot(store):
    c = store.conn
    return (c.execute("select count(*), sum(connected), sum(engaged) from calls").fetchone(),
            c.execute("select sum(dials), sum(connects), sum(engaged), sum(sum_adherence) from cube").fetchone(),
            c.execute("select count(*) from leads").fetchone(), store.get_cursor(),
            store.count("alerts"), store.count("decisions"))


@pytest.fixture()
def ingested(client, store):
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    ing = Ingestor(client, store)
    stats = ing.sync()
    return client, store, ing, stats


def test_ingest_history_basic(ingested, world):
    client, store, ing, stats = ingested
    assert stats.calls > 0 and stats.events == world.event_seq
    assert store.get_cursor().last_event_id == world.event_seq
    calls, cubesum = snapshot(store)[:2]
    assert cubesum[0] == calls[0] and cubesum[1] == calls[1] and cubesum[2] == calls[2]
    assert store.count("alerts") == 0  # cohort self-check passes on default-only calls


def test_reprocessing_chunks_is_a_noop(ingested):
    client, store, ing, _ = ingested
    before = snapshot(store)
    for meta in client.list_chunks("history"):
        stats = ing.process_chunk(meta)
        assert stats.events == 0
    # a brand-new ingestor (fresh caches, as after restart) behaves the same
    for meta in client.list_chunks("history"):
        Ingestor(client, store).process_chunk(meta)
    assert snapshot(store) == before


def test_resume_after_partial_ingest(client, store, world):
    chunks = client.list_chunks("history")
    ing = Ingestor(client, store)
    ing.process_chunk(chunks[0])
    first = store.get_cursor()
    for _ in range(40):
        client.advance(15)
    Ingestor(client, store).sync()
    assert store.get_cursor().last_event_id == world.event_seq > first.last_event_id


def test_failed_chunk_rolls_back(client, store, monkeypatch):
    ing = Ingestor(client, store)

    def boom(batch):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ing, "_flush", boom)
    with pytest.raises(RuntimeError):
        ing.process_chunk(client.list_chunks("history")[0])
    assert store.get_cursor().last_event_id == 0 and store.count("calls") == 0
    assert store.count("leads") == 0


def test_cohort_mismatch_raises_alert(client, store, world):
    # Mirror a config whose cohorts differ from what the sim actually used.
    wid = client.workflows()[0]["workflow_id"]
    cfg = client.get_config(wid)
    bad = dict(cfg["config"], cohorts=[{"id": "ghost", "where": {"lang": ["hi", "en", "ta", "te", "mr", "bn"]}}])
    store.store_config_body(wid, cfg["version"], "x", bad)
    Ingestor(client, store).sync()
    row = store.conn.execute("select code from alerts where code='cohort_mismatch'").fetchone()
    assert row is not None
    assert store.conn.execute("select count(*) from alerts where code='cohort_mismatch_total'").fetchone()[0] >= 1


def test_events_recorded_in_audit_tables(client, store, world):
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    ing = Ingestor(client, store)
    ing.sync()
    wid = "nw_renewal"
    cfg = client.get_config(wid)["config"]
    cfg["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    client.put_config(wid, {"proposal_id": "t1", "base_version": 1, "proposed_config": cfg,
                            "rationale": "spread"})
    for _ in range(4):
        client.advance(15)
    stats = ing.sync()
    assert stats.config_changes >= 1
    v2 = store.config_body(wid, 2)
    assert v2 and v2["allocations"]["default"]["caller_id"] == {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    assert store.conn.execute("select actor from config_versions where workflow_id=? and version=2",
                              (wid,)).fetchone()[0] == "controller_auto"
    assert store.count("campaigns") > 0
    assert store.conn.execute("select count(*) from decisions where kind='config_observed'").fetchone()[0] >= 1
    assert store.count("alerts") == 0


def test_ts_to_minute():
    assert ts_to_minute("2027-01-04T00:00:00+05:30") == 0
    assert ts_to_minute("2027-01-05T01:15:00+05:30") == 1440 + 75
    assert ts_to_minute("2027-01-03T23:45:00+05:30") == -15


def test_cube_helpers(ingested, world):
    client, store, ing, _ = ingested
    wid = "nw_renewal"
    by_day = cube.rollup(store, wid, by=("day",))
    assert [r.key[0] for r in by_day] == sorted(r.key[0] for r in by_day) and min(r.key[0] for r in by_day) < 0
    tot = cube.totals(store, wid)
    assert tot.dials == sum(r.dials for r in by_day) and 0 < tot.pickup < 1 and tot.engagement is not None
    assert tot.flag_rate is not None
    v = cube.totals(store, wid, variant={"voice": "no-such"})
    assert v.dials == 0 and v.pickup is None
    cells = cube.cells_matching(ing.store, ing.vocab, wid, {"lang": ["hi"]})
    sub = cube.totals(store, wid, cells=cells)
    assert 0 < sub.dials < tot.dials
    assert cube.rollup(store, wid, cells=[]) == []
    assert cube.rollup(store, wid, by=("hour",), day_min=-1, day_max=-1)
    with pytest.raises(ValueError):
        cube.rollup(store, wid, by=("bogus",))
    with pytest.raises(ValueError):
        cube.rollup(store, wid, variant={"cell": "1"})


# ---------------------------------------------------------------- loop
def test_windows_and_adaptive_step():
    wfs = [{"dialling_window": [9, 21], "governance": {"approver_hours": [10, 19]}},
           {"dialling_window": [10, 20], "governance": {"approver_hours": [10, 18]}}]
    w = Windows.from_workflows(wfs)
    day = settings.MINUTES_PER_DAY
    assert w.active_at(9 * 60) and not w.active_at(3 * 60) and not w.active_at(21 * 60)
    assert choose_step(3 * 60, 10 * day, w, False) == 60
    assert choose_step(3 * 60, 10 * day, w, True) == 15  # something in flight
    assert choose_step(12 * 60, 10 * day, w, False) == 15  # inside windows
    assert choose_step(8 * 60 + 15, 10 * day, w, False) == 45  # stop at the 09:00 boundary
    assert choose_step(day - 15, day, w, False) == 15
    assert Windows.from_workflows([{}]).active_at(0)  # unreadable windows: always active


def test_run_to_finish_integration(client, store, world):
    ctl = Controller(client, store, registry=Registry())
    result = ctl.run()
    assert result.finished and result.ticks > 0
    assert client.state()["finished"]
    assert store.count("calls") > 0 and store.count("alerts") == 0
    assert store.get_cursor().last_event_id == world.event_seq  # fully drained
    assert store.count("metrics") == result.ticks
    kinds = {r[0] for r in store.conn.execute("select distinct kind from decisions")}
    assert {"startup", "advance_intent", "finished"} <= kinds
    steps = {r[0] for r in store.conn.execute("select distinct step_minutes from metrics")}
    assert 60 in steps and 15 in steps  # adaptive stepping engaged
    # no config changes: every call used version 1 and the default cohort
    assert store.conn.execute("select count(*) from calls where config_version<>1").fetchone()[0] == 0


def test_stop_at_minute_and_restart_resume(client, store, world):
    Controller(client, store, stop_at_minute=600, registry=Registry()).run()
    assert 600 <= client.state()["sim_minute"] < 700 and not client.state()["finished"]
    store.close()
    store2 = Store(store.path)
    result = Controller(client, store2, registry=Registry()).run()  # a "restart": resumes from SQLite
    assert result.finished
    assert store2.get_cursor().last_event_id == world.event_seq
    assert store2.count("calls") == store2.conn.execute("select sum(dials) from cube").fetchone()[0]
    store2.close()


def test_advance_transport_error_does_not_double_advance(world, store, tmp_path):
    import sim.server
    from fastapi.testclient import TestClient
    state = {"lost_once": False}
    with TestClient(sim.server.app) as tc:
        def handler(req):
            resp = tc.request(req.method, req.url.path, content=req.content,
                              headers={"content-type": "application/json"})
            if req.url.path == "/sim/advance" and not state["lost_once"]:
                state["lost_once"] = True
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(resp.status_code, content=resp.content,
                                  headers={"content-type": resp.headers["content-type"]})

        http = httpx.Client(base_url="http://sim", transport=httpx.MockTransport(handler))
        cl = SimClient(http=http, sleep=lambda s: None)
        before = cl.state()["sim_minute"]
        res = cl.advance(15)
    assert res["recovered"] and res["sim_minute"] == before + 15 == world.now


def test_controller_gives_up_after_repeated_advance_failures(client, store, monkeypatch):
    def fail(minutes):
        raise AdvanceNotApplied("nope")

    monkeypatch.setattr(client, "advance", fail)
    monkeypatch.setattr("controller.loop.time.sleep", lambda s: None)
    with pytest.raises(AdvanceNotApplied):
        Controller(client, store, registry=Registry()).run()
    assert store.conn.execute("select count(*) from alerts where code='advance_failed'").fetchone()[0] == 5


def test_status_cli(client, store, world, tmp_path, capsys):
    Controller(client, store, stop_at_minute=0, registry=Registry()).run()
    store.close()
    assert cli_main(["status", "--json", "--state-dir", str(store.path.parent)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["calls"] > 0 and out["cursor"]["event_id"] == world.event_seq
    assert cli_main(["status", "--state-dir", str(tmp_path / "empty")]) == 1


def test_sim_restart_mid_run_raises_world_regressed_and_alerts(client, store, world, monkeypatch):
    from controller.loop import Controller, WorldRegressed
    ctl = Controller(client, store)
    ctl.startup()
    for _ in range(3):
        ctl.tick()
    real = client.state
    monkeypatch.setattr(client, "state", lambda: {**real(), "sim_minute": 0, "last_event_id": 0})
    with pytest.raises(WorldRegressed):
        ctl.tick()
    assert store.conn.execute("select count(*) from alerts where code='world_regressed'").fetchone()[0] == 1


def test_run_cli_exits_nonzero_on_world_regression(monkeypatch):
    from controller import __main__ as m
    from controller.loop import WorldRegressed
    monkeypatch.setattr(m, "_run", lambda args: (_ for _ in ()).throw(WorldRegressed("x")))
    assert m.cmd_run(None) == m.EXIT_WORLD_REGRESSED
