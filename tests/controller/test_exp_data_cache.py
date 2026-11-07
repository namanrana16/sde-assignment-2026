"""Completed days of the experiment arm counts are cached and must equal a raw scan exactly."""
import random

import pytest

from controller import exp_data
from controller.store import Store

DAY = 1440
COLS = ("event_id", "workflow_id", "ts_minute", "day", "hour", "lid", "attempt_no", "config_version", "cohort_id",
        "welcome", "voice", "caller_id", "prompt", "connected", "engaged")


@pytest.fixture()
def db(tmp_path):
    s = Store(tmp_path / "s" / "c.db")
    rng = random.Random(5)
    eid = 0
    for day in range(5):
        for k in range(400):
            eid += 1
            ts = day * DAY + rng.randrange(540, 1260)
            conn = int(rng.random() < 0.4)
            s.conn.execute(f"INSERT INTO calls({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                           (eid, "w", ts, day, ts % DAY // 60, rng.randrange(150), 1, 1 + day // 2, "default",
                            rng.choice(["W1", "W2"]), "V1", "D1", "P1", conn, int(conn and rng.random() < 0.5)))
    yield s
    s.close()


def raw(store, start, now, cells=None):
    return exp_data._load_arms_raw(store, "w", "welcome_message", start, now, cells)


def same(a, b):
    assert set(a) == set(b)
    for arm in a:
        x, y = a[arm], b[arm]
        assert (x.engaged, x.recent, x.pickup, x.engaged_s, x.recent_s) == (y.engaged, y.recent, y.pickup, y.engaged_s, y.recent_s)


@pytest.mark.parametrize("start,now", [(0, 4 * DAY + 700), (1 * DAY, 4 * DAY + 700), (DAY + 300, 4 * DAY + 700),
                                       (3 * DAY, 4 * DAY + 10)])
def test_cached_equals_raw_scan_and_is_reused(db, start, now):
    first = exp_data.load_arms(db, "w", "welcome_message", start, now)
    same(first, raw(db, start, now))
    n_cached = db.conn.execute("SELECT COUNT(*) FROM exp_day_cache").fetchone()[0]
    again = exp_data.load_arms(db, "w", "welcome_message", start, now + 15)
    same(again, raw(db, start, now + 15))
    assert db.conn.execute("SELECT COUNT(*) FROM exp_day_cache").fetchone()[0] == n_cached  # nothing recomputed


def test_today_and_partial_start_day_are_never_cached(db):
    exp_data.load_arms(db, "w", "welcome_message", DAY + 300, 3 * DAY + 100)
    days = {r[0] for r in db.conn.execute("SELECT day FROM exp_day_cache")}
    assert days == {2}  # day 1 starts mid-day, day 3 is today


def test_cell_filter_is_part_of_the_cache_key(db):
    for lid in range(150):
        db.conn.execute("INSERT OR IGNORE INTO leads(lid, lead_id, person_id, workflow_id, cell, campaign_id, ts_minute)"
                        " VALUES (?, ?, ?, 'w', ?, 'c', 0)", (lid, f"l{lid}", f"p{lid}", lid % 3))
    a = exp_data.load_arms(db, "w", "welcome_message", 0, 4 * DAY, cells=[0])
    b = exp_data.load_arms(db, "w", "welcome_message", 0, 4 * DAY, cells=[1, 2])
    same(a, raw(db, 0, 4 * DAY, [0]))
    same(b, raw(db, 0, 4 * DAY, [1, 2]))
