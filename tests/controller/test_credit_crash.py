"""A crash between a credit charge and its ledger row is reconciled against /sim/state on restart."""
import pytest

from controller import credits, faults
from tests.controller.helpers import SimulatedCrash, arm_crash, disarm_crash


def ledger_total(store):
    return store.conn.execute("SELECT COALESCE(SUM(credits),0) FROM llm_calls").fetchone()[0]


def test_crash_after_charge_before_record_is_reconciled(client, store, world, monkeypatch):
    assert "after-credit-call-before-record" in faults.POINTS
    credits.reconcile_ledger(client, store, 0)  # first start: remembers the initial balance
    initial = world.credits
    arm_crash(monkeypatch, "after-credit-call-before-record:1")
    with pytest.raises(SimulatedCrash):
        credits.call_with_credit_ledger(client, store, "nw_renewal", 5, "validate P3", client.run_validation,
                                        "nw_renewal", "P3", 300)
    disarm_crash(monkeypatch)
    assert world.credits < initial and ledger_total(store) == 0  # charged but unrecorded
    credits.reconcile_ledger(client, store, 10)  # restart
    assert ledger_total(store) == pytest.approx(initial - world.credits)
    assert store.conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='credit_unrecorded'").fetchone()[0] == 1
    credits.reconcile_ledger(client, store, 20)  # idempotent
    assert ledger_total(store) == pytest.approx(initial - world.credits)
    assert store.conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='credit_unrecorded'").fetchone()[0] == 1
