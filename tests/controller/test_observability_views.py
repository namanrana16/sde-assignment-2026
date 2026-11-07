"""C11/C14 views against a store produced by a real in-process sim run: view text, CLI, web pages, status."""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from controller import ui, view, view_data
from controller.__main__ import main as cli_main
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client, when


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("obs")
    world = build(tmp, ("nw_renewal",), days=4, history_days=6)
    client, http = make_client(world)
    store = Store(tmp / "state" / "t.db")
    Controller(client, store, stop_at_minute=when(3, 0)).run()
    path = store.path
    store.close()
    http.__exit__(None, None, None)
    return path


@pytest.fixture()
def ro(db):
    s = view_data.open_readonly(db)
    yield s
    s.close()


def test_view_has_exactly_the_six_sections_in_order(ro):
    text = view.render(view_data.build(ro, "nw_renewal"))
    heads = ["WHAT'S BEING TESTED", "WHAT'S WINNING", "WHAT WE NEED FROM YOU", "WHAT CHANGED TODAY",
             "OPEN INCIDENTS & ALERTS", "KPIs"]
    pos = [text.index(h) for h in heads]
    assert pos == sorted(pos)
    assert "pickup " in text and "engagement " in text  # reported separately, never blended


def test_view_shows_arms_ci_and_lift_for_a_running_experiment(ro):
    d = view_data.build(ro, "nw_renewal")
    running = [e for e in d["experiments"] if e["phase"] == "running"]
    assert running, "the sim run should leave an experiment running"
    text = view.render(d)
    assert "share" in text and "lift vs control" in text and "look " in text
    arms = running[0]["arms"]
    assert any(a["ci"] and a["ci"][0] < a["engagement"] < a["ci"][1] for a in arms.values())


def test_view_at_an_earlier_time_bounds_changes_and_kpis(ro):
    now = view_data.now_minute(ro)
    d = view_data.build(ro, "nw_renewal", now - 2 * 1440)
    assert d["kpis"]["day"] == (now - 2 * 1440) // 1440
    assert all(c["ts_minute"] // 1440 == d["kpis"]["day"] for c in d["changes"])


def test_changes_today_carry_actor_reason_and_why_hint(ro):
    for day in range(0, 3):
        for c in view_data.changes_today(ro, "nw_renewal", day * 1440 + 1439):
            assert c["actor"] and c["reason"] and c["why"].startswith("python -m controller why --workflow nw_renewal --at")


def test_weekly_lists_only_operational_auto_applies(ro):
    items = view_data.weekly_auto_applies(ro, "nw_renewal", view_data.now_minute(ro))
    assert items and all(set(i["dims"]) & {"caller_id", "schedule"} for i in items)
    assert all(i["reason"] for i in items)
    text = view.render_weekly(items, view_data.now_minute(ro), "nw_renewal")
    assert "reason:" in text


def test_read_only_connection_cannot_write(ro):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("INSERT INTO alerts(severity, code, message) VALUES ('page','x','y')")


def test_cli_view_weekly_status(db, capsys):
    d = str(db.parent)
    assert cli_main(["view", "--state-dir", d, "--workflow", "nw_renewal"]) == 0
    assert "WHAT WE NEED FROM YOU" in capsys.readouterr().out
    assert cli_main(["view", "--state-dir", d, "--weekly"]) == 0
    assert "Operational auto-applies" in capsys.readouterr().out
    assert cli_main(["view", "--state-dir", d, "--workflow", "nope"]) == 2
    capsys.readouterr()
    assert cli_main(["view", "--state-dir", d, "--workflow", "nw_renewal", "--at", "garbage"]) == 2
    capsys.readouterr()
    assert cli_main(["status", "--state-dir", d]) == 0
    out = capsys.readouterr().out
    assert "credits remaining" in out and "approvals in flight" in out and "local error counts" in out
    assert "last 24 sim hours" in out and "decisions/hour" in out


def test_metrics_ext_recorded_each_tick(ro):
    n = ro.conn.execute("SELECT COUNT(*) FROM metrics_ext").fetchone()[0]
    ticks = ro.conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    assert n > 0 and abs(n - ticks) <= 2
    credits = ro.conn.execute("SELECT credits FROM metrics_ext ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert credits is not None and credits > 0


def test_ui_pages_return_200(db):
    client = TestClient(ui.create_app(db))
    for url in ("/", "/wf/nw_renewal", "/weekly", "/status", "/why?workflow=nw_renewal&at=Day%202%2014:00"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "<meta name=\"viewport\"" in r.text
    assert "What we need from you" in client.get("/wf/nw_renewal").text
    assert "nw_renewal" in client.get("/").text
    assert client.get("/wf/nope").status_code == 404
    assert client.get("/why?workflow=nw_renewal&at=bad").status_code == 400


def test_ui_works_while_a_writer_holds_the_database(db):
    w = Store(db)  # a live controller: open write transaction in WAL mode
    try:
        w.conn.execute("BEGIN IMMEDIATE")
        w.conn.execute("INSERT INTO alerts(severity, code, message) VALUES ('digest','probe','x')")
        assert TestClient(ui.create_app(db)).get("/wf/nw_renewal").status_code == 200
        w.conn.execute("ROLLBACK")
    finally:
        w.close()


def test_ui_port_in_use_exits_with_a_clear_message(db, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert ui.serve(db, port) == 1
    assert "cannot listen" in capsys.readouterr().err


def test_ui_cli_missing_database(tmp_path, capsys):
    assert cli_main(["ui", "--state-dir", str(tmp_path / "none")]) == 1
    assert json.dumps(capsys.readouterr().out)  # prints a message, no traceback
