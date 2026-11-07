"""B5/B6: credit reserve scales with workflows, per-tick alerts are bounded, suggest-fixes is not wasted."""
import json

import pytest

from controller import incidents, settings
from controller.desired import PlanContext
from controller.detectors import halluc as hl
from controller.detectors import halluc_data as hd
from controller.store import Store
from tests.controller.test_halluc_units import FakeClient

NOW = 5000


def wf(options=("P1", "P2", "P3")):
    return {"workflow_id": "w", "governance": {"min_validation_pass_rate": 0.97}, "objective": {},
            "options": {"prompt_version": list(options)}}


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_reserve_scales_down_with_many_workflows():
    assert incidents.reserve_per_workflow(300, 12) == pytest.approx(12.5)
    assert incidents.reserve_per_workflow(300, 3) == pytest.approx(settings.CREDIT_RESERVE_PER_WORKFLOW)
    assert incidents.reserve_per_workflow(None, 12) == settings.CREDIT_RESERVE_PER_WORKFLOW
    assert not incidents.can_spend(300, 3, 12)  # legacy: 25 x 12 = 300 reserve, routine impossible
    assert incidents.can_spend(300, 3, 12, credits_initial=300)  # 12.5 x 12 = 150 reserve
    assert not incidents.can_spend(152, 3, 12, credits_initial=300)


def _ctx(store, client, w=None):
    return PlanContext(store, client, {"w": w or wf()}, NOW)


def _body():
    return hl._new_body("P2", "P1", {"rate": 0.05, "base_rate": 0.015, "n": 300, "p": 1e-6}, {}, "prompt_flag_spike")


def _alerts(store):
    return [r[0] for r in store.conn.execute("select code from alerts")]


@pytest.mark.parametrize("credits", [0.0, 5.0])
def test_low_credits_bounded_alerts_and_no_spend(store, credits):
    client = FakeClient(credits=credits)
    st = hd.fresh_state()
    for tick in range(100):
        ctx = PlanContext(store, client, {"w": wf()}, NOW + tick * 15)  # all within one sim day
        hl.prepare_fix(ctx, "w", _body(), st, "P2")
        hl.day0_validation(ctx)
    codes = _alerts(store)
    assert len(codes) <= 4 and client.suggest_calls <= 1 and len(client.validations) <= 1
    assert client.credits >= 0


def test_alerts_repeat_only_once_per_sim_day(store):
    client = FakeClient(credits=0.0)
    st = hd.fresh_state()
    for day in range(3):
        for tick in range(10):
            hl.prepare_fix(PlanContext(store, client, {"w": wf()}, day * 1440 + tick * 15), "w", _body(), st, "P2")
    assert _alerts(store).count("no_prompt_fix") == 3


def test_suggest_response_is_persisted_before_validation(store):
    client = FakeClient(credits=200.0)
    ctx = _ctx(store, client)
    saved = []
    body = _body()
    orig = client.run_validation

    def spy(wid, prompt, n):
        saved.append((body["suggested"], list(body.get("pending_suggestions", []))))
        raise RuntimeError("crash mid validation")

    client.run_validation = spy
    persisted = []
    with pytest.raises(RuntimeError):
        hl.prepare_fix(ctx, "w", body, hd.fresh_state(), "P2", persist=lambda: persisted.append(json.dumps(body)))
    snap = json.loads(persisted[-1])
    assert snap["suggested"] and snap["credits_spent"] >= settings.SUGGEST_FIXES_COST
    assert snap["pending_suggestions"], "suggestions must be persisted before the validations start"
    client.run_validation = orig


def test_single_prompt_workflow_never_buys_suggestions(store):
    client = FakeClient(credits=200.0)
    ctx = _ctx(store, client, wf(options=("P2",)))
    body = _body()
    hl.prepare_fix(ctx, "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 0 and body["target"] is None


def test_suggest_skipped_when_every_other_prompt_was_tried(store):
    client = FakeClient(credits=200.0)
    body = _body()
    body["tried"] = ["P1", "P3"]
    hl.prepare_fix(_ctx(store, client), "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 0


def test_suggest_still_called_when_an_untried_option_exists(store):
    client = FakeClient(credits=200.0)
    body = _body()
    body["tried"] = ["P1"]
    hl.prepare_fix(_ctx(store, client), "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 1


def test_credits_low_alert_uses_the_scaled_reserve(store):
    from controller import alerts
    store.set_meta("credits_initial", "300.0")
    for i in range(12):
        store.set_meta(f"workflow_policy:w{i}", "{}")
    assert alerts.credits_low(store, 200.0) == []  # reserve 12 x 12.5 = 150
    assert alerts.credits_low(store, 100.0)
