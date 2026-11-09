"""Review 4: append-only audit triggers (D2) and schedule haircut disclosure (D1)."""
import sqlite3

import pytest

from controller import schedule, settings
from controller.store import Store
from tests.controller.test_schedule_producer import PLAN, cfg, setup


def _store(tmp_path):
    return Store(tmp_path / "a.db")


@pytest.mark.parametrize("table", ["decisions", "alerts"])
def test_audit_tables_reject_update_and_delete(tmp_path, table):
    store = _store(tmp_path)
    cols = [r[1] for r in store.conn.execute(f"PRAGMA table_info({table})") if r[1] != "id"]
    vals = [1 if "minute" in c else "x" for c in cols]
    store.conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute(f"UPDATE {table} SET {cols[-1]}='y'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute(f"DELETE FROM {table}")
    assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_proposal_events_backfill_allowed_but_nothing_else(tmp_path):
    store = _store(tmp_path)
    store.conn.execute("INSERT INTO proposal_events (event_id, approval_id, event, payload)"
                       " VALUES (1,'ap','approval_decided','p')")
    store.conn.execute("UPDATE proposal_events SET proposal_id='P1' WHERE approval_id='ap' AND proposal_id IS NULL")
    assert store.conn.execute("SELECT proposal_id FROM proposal_events").fetchone()[0] == "P1"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("UPDATE proposal_events SET proposal_id='P2'")  # already linked
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("UPDATE proposal_events SET payload='tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("DELETE FROM proposal_events")


def test_triggers_are_added_to_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    Store(path).close()
    raw = sqlite3.connect(path)
    for name in [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]:
        raw.execute(f"DROP TRIGGER {name}")
    raw.commit()
    raw.close()
    store = Store(path)
    store.conn.execute("INSERT INTO decisions (sim_minute, workflow_id, kind, summary) VALUES (1,'w','k','s')")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("DELETE FROM decisions")


def test_schedule_proposal_discloses_the_haircut_and_keeps_impact(tmp_path):
    ctx = setup(tmp_path, cfg(), {"plan": PLAN}, auto=False)
    [ch] = schedule.schedule_producer(ctx, "w")
    pct = f"{int(settings.SCHEDULE_LIFT_HAIRCUT * 100)}%"
    method = ch.evidence["dry_run_extra"]["schedule_guardrail"]["method"]
    assert pct in method and "no CI" in method
    expected = settings.SCHEDULE_LIFT_HAIRCUT * PLAN["estimate"]["rel_lift"] / 0.10
    assert ch.evidence["impact_override"] == pytest.approx(expected)
