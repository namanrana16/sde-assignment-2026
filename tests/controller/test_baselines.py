"""C3 baselines on a real (short) history, and cold start."""
import pytest

from controller import baselines, settings
from controller.desired import PlanContext, Registry
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client


def test_baselines_after_history_ingest(tmp_path):
    world = build(tmp_path, ("ks_cancel",), days=2, history_days=5)
    client, http = make_client(world)
    store = Store(tmp_path / "s.db")
    reg = Registry()
    reg.register_detector(baselines.ensure)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    ctl.tick()
    b = baselines.get(store, "ks_cancel")
    assert b["status"] == "history" and b["history_days"] == 5
    assert set(b["did_daily"]) == {"D1", "D2"} and len(b["did_daily"]["D1"]) == 5
    assert b["prompt"]["P1"]["flag_rate"] is not None and b["prompt"]["P1"]["connects"] > 1000
    assert 0.2 < b["engagement"]["mean"] < 0.9 and b["engagement"]["variance"] >= 0
    assert sum(b["lead_cells"].values()) > 1000
    assert set(b["pickup_hour_feature"]) == {"lang", "city_tier", "age_band", "value_band"}
    assert store.conn.execute("select count(*) from decisions where kind='baselines'").fetchone()[0] == 1
    ctl.tick()
    assert store.conn.execute("select count(*) from decisions where kind='baselines'").fetchone()[0] == 1  # once
    store.close()
    http.__exit__(None, None, None)


def test_cold_start_is_marked_and_refreshed_from_live_data(tmp_path):
    world = build(tmp_path, ("ks_cancel",), days=3, history_days=0)
    client, http = make_client(world)
    store = Store(tmp_path / "c.db")
    reg = Registry()
    reg.register_detector(baselines.ensure)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    ctl.tick()
    assert baselines.get(store, "ks_cancel")["status"] == "missing"
    while client.state()["sim_minute"] < 1440 * 2 and not ctl.tick()[0]:
        pass
    b = baselines.get(store, "ks_cancel")
    assert b["status"] == "cold_start" and b["engagement"]["days"] >= 1
    store.close()
    http.__exit__(None, None, None)
