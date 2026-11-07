"""B4: a strict min_validation_pass_rate must not turn a healthy ~1.5% flag rate into a perpetual incident."""
import pytest

from controller import incidents
from controller.desired import PlanContext
from controller.detectors import halluc as hl
from controller.detectors import halluc_data as hd
from controller.store import Store
from tests.controller.test_halluc_units import FakeClient

WF = {"workflow_id": "w", "governance": {"min_validation_pass_rate": 0.99}, "objective": {}}
NOW = 5000


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _state(live_n, live_flags, base=(20000, 300), cold=False):
    st = hd.fresh_state()
    st["first_prompt"], st["base_frozen"] = "P1", True
    st["base"] = [0, 0, 0.0, 0] if cold else [base[0], base[1], base[0] * 0.93, base[0] // 2]
    st["prompts"]["P1"] = {"total": [live_n, live_flags, live_n * 0.93, live_n // 2], "first_ts": -5000, "last_ts": NOW,
                           "hours": {str(NOW // 60): [live_n, live_flags, live_n * 0.93, live_n // 2]}}
    return st


def _spike(store, st):
    return hl._production_spike(PlanContext(store, None, {"w": WF}, NOW), "w", st, "P1")


def test_healthy_prompt_at_its_baseline_rate_is_not_a_ceiling_incident(store):
    assert _spike(store, _state(20000, 300)) is None  # 1.5% live vs 1.5% baseline, ceiling 1%


def test_true_spike_still_detected_against_the_baseline(store):
    got = _spike(store, _state(2000, 120))  # 6% vs 1.5%
    assert got is not None and got[2] == "prompt_flag_spike"


def test_ceiling_still_applies_on_a_cold_start_with_no_baseline(store):
    got = _spike(store, _state(2000, 120, cold=True))
    assert got is not None and got[2] == "prompt_flag_ceiling"
    assert _spike(store, _state(2000, 4, cold=True)) is None


def test_ceiling_with_a_baseline_is_raised_to_the_baseline_not_below_governance():
    assert hl._effective_ceiling(WF, [20000, 300, 0, 0]) > 0.015
    assert hl._effective_ceiling(WF, [20000, 0, 0, 0]) == pytest.approx(0.01)
    assert hl._effective_ceiling(WF, None) == pytest.approx(0.01)
    assert hl._effective_ceiling(WF, [0, 0, 0, 0]) == pytest.approx(0.01)


def test_borderline_known_good_validation_is_revalidated_once_at_n300(store):
    client = FakeClient()
    client.rates["P1"] = 0.997
    incidents.store_validation(store, {"validation_run_id": "val_old", "workflow_id": "w", "prompt_version": "P1",
                                       "n": 100, "pass_rate": 0.99}, 1, 0)
    ctx = PlanContext(store, client, {"w": WF}, NOW)
    body = hl._new_body("P2", "P1", {"rate": 0.05, "base_rate": 0.015, "n": 300, "p": 1e-6}, {}, "prompt_flag_spike")
    run = hl._validated(ctx, "w", "P1", body, 0.995)
    assert run is not None and run["pass_rate"] == 0.997 and client.validations == [("P1", 300)]
    client.rates["P1"] = 0.5
    incidents.store_validation(store, {"validation_run_id": "val_o2", "workflow_id": "w", "prompt_version": "P3",
                                       "n": 100, "pass_rate": 0.99}, 1, 0)
    client.rates["P3"] = 0.9
    assert hl._validated(ctx, "w", "P3", body, 0.995) is None  # revalidated, still fails
    assert hl._validated(ctx, "w", "P3", body, 0.995) is None
    assert client.validations.count(("P3", 300)) == 1  # only once


def test_far_below_min_pass_is_not_revalidated(store):
    client = FakeClient()
    incidents.store_validation(store, {"validation_run_id": "val_o", "workflow_id": "w", "prompt_version": "P2",
                                       "n": 300, "pass_rate": 0.90}, 3, 0)
    ctx = PlanContext(store, client, {"w": WF}, NOW)
    body = hl._new_body("P1", None, {"rate": 0.05, "base_rate": 0.015, "n": 300, "p": 1e-6}, {}, "prompt_flag_spike")
    assert hl._validated(ctx, "w", "P2", body, 0.99) is None and client.validations == []


def test_verify_uses_the_same_effective_ceiling_as_detection(store):
    """D2: a healthy replacement at the known-good baseline rate must close an absolute incident."""
    st = hd.fresh_state()
    st["prompts"]["K"] = {"total": [20000, 300, 18600.0, 10000], "hours": {}, "first_ts": -9000, "last_ts": 3000}
    hd._apply_group(st, "P9", 100, [10000, 150, 9300.0, 5000], 5940, 6000)  # 1.5% live, bar 1%
    body = hl._new_body("P2", None, {"rate": 0.05, "base_rate": 0.0, "n": 300, "p": 0.0}, {}, "prompt_flag_ceiling")
    inc_id = incidents.open_incident(store, "w", "halluc", 4000, body)
    ctx = PlanContext(store, None, {"w": WF}, 6000)
    hl._verify(ctx, "w", {"id": inc_id, "opened_minute": 4000, "body": body}, st, "P9")
    assert store.conn.execute("select state from incidents").fetchone()[0] == "closed"
