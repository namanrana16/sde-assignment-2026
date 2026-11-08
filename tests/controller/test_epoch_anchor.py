"""Runtime epoch anchoring: ts -> minute is verified against GET /sim/state at startup."""
from __future__ import annotations

import pytest

from controller import ingest
from controller.ingest import anchor_epoch, ts_to_minute
from controller.world_id import open_world_store
from sim.world import fmt_ts

DAY = 1440


@pytest.fixture(autouse=True)
def _reset_anchor():
    ingest.reset_epoch_anchor()
    yield
    ingest.reset_epoch_anchor()


class FakeClient:
    """Minimal client for open_world_store: a world with no chunks yet."""

    def __init__(self, state: dict) -> None:
        self._state = state

    def state(self) -> dict:
        return dict(self._state)

    def list_chunks(self, kind: str) -> list:
        return []


def make_state(minute: int, ts: str) -> dict:
    return {"scenario": "t", "sim_minute": minute, "ts": ts, "chunks": 0, "last_event_id": 0}


def test_contract_pair_has_zero_delta():
    assert anchor_epoch(fmt_ts(1500), 1500) == 0
    assert ts_to_minute(fmt_ts(-60)) == -60


@pytest.mark.parametrize("shift", [DAY, 330])
def test_shifted_ts_reanchors(shift):
    minute = 600
    shifted_ts = fmt_ts(minute + shift)
    assert anchor_epoch(shifted_ts, minute) == shift
    assert ts_to_minute(shifted_ts) == minute
    assert ts_to_minute(fmt_ts(minute + shift + 45)) == minute + 45
    assert ts_to_minute(fmt_ts(minute + shift - 2 * DAY)) == minute - 2 * DAY


def test_reanchor_is_idempotent_across_restart():
    ts = fmt_ts(600 + DAY)
    assert anchor_epoch(ts, 600) == DAY
    first = ts_to_minute(fmt_ts(700 + DAY))
    assert anchor_epoch(ts, 600) == DAY  # same state again: same delta, same minutes
    assert ts_to_minute(fmt_ts(700 + DAY)) == first == 700


def _alerts(store):
    return store.conn.execute("SELECT severity, code, message FROM alerts").fetchall()


def test_startup_writes_one_alert_when_shifted(tmp_path):
    store, _, _ = open_world_store(FakeClient(make_state(600, fmt_ts(600 + 330))), tmp_path)
    rows = _alerts(store)
    assert len(rows) == 1
    severity, code, message = rows[0]
    assert (severity, code) == ("warn", "epoch_reanchored")
    assert "330" in message and fmt_ts(930) in message
    assert ts_to_minute(fmt_ts(930)) == 600


def test_startup_no_alert_when_contract_holds(tmp_path):
    store, _, _ = open_world_store(FakeClient(make_state(600, fmt_ts(600))), tmp_path)
    assert _alerts(store) == []
    assert ts_to_minute(fmt_ts(600)) == 600


def test_state_without_ts_keeps_contract_epoch_and_writes_no_alert(tmp_path):
    """An older simulator's /sim/state without ts must not crash startup."""
    from controller import world_id
    from controller.store import Store

    store = Store(tmp_path / "x.db")
    world_id._anchor_epoch_from_state(store, {"sim_minute": 0})
    rows = store.conn.execute("SELECT COUNT(*) FROM alerts WHERE code='epoch_reanchored'").fetchone()[0]
    assert rows == 0
