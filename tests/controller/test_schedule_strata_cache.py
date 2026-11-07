"""Per-day cache of the schedule strata must equal the single big SQL exactly."""
import random

import pytest

from controller import schedule
from controller.store import Store

DAY = 1440


@pytest.fixture()
def db(tmp_path):
    s = Store(tmp_path / "s" / "c.db")
    rng = random.Random(3)
    for k in range(5):
        s.conn.execute("INSERT INTO campaigns(campaign_id, workflow_id, ts_minute, leads, config_version_at_creation)"
                       " VALUES (?, 'w', ?, 100, 1)", (f"c{k}", k * DAY + 480))
    for lid in range(200):
        s.conn.execute("INSERT INTO leads(lid, lead_id, person_id, workflow_id, cell, campaign_id, ts_minute)"
                       " VALUES (?,?,?,?,?,?,0)", (lid, f"l{lid}", f"p{lid}", "w", lid % 4, f"c{lid % 5}"))
    eid = 0
    for day in range(6):
        for _ in range(500):
            eid += 1
            conn = int(rng.random() < 0.3)
            s.conn.execute("INSERT INTO calls(event_id, workflow_id, ts_minute, day, hour, lid, attempt_no,"
                           " config_version, cohort_id, welcome, voice, caller_id, prompt, connected, engaged)"
                           " VALUES (?,?,?,?,?,?,?,1,'default','W1','V1','D1','P1',?,?)",
                           (eid, "w", day * DAY + 600, day, rng.randrange(9, 21), rng.randrange(200),
                            rng.randrange(1, 4), conn, int(conn and rng.random() < 0.5)))
    yield s
    s.close()


def raw(store, lo, hi):
    return [(c, h, a, cd, n, x, e or 0) for c, h, a, cd, n, x, e in store.conn.execute(
        schedule.STRATA_SQL, ("w", lo, hi))]


@pytest.mark.parametrize("lo,today", [(0, 5), (2, 5), (-9, 4)])
def test_cached_strata_equal_the_raw_query(db, lo, today):
    got = schedule.strata_rows(db, "w", lo, today)
    assert got == raw(db, lo, today)
    assert schedule.strata_rows(db, "w", lo, today) == got  # second call served from the cache
    cached = {r[0] for r in db.conn.execute("SELECT day FROM exp_day_cache WHERE col='strata'")}
    assert today not in cached and cached  # today is never cached
