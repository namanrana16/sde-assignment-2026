"""Review round 2 regressions: absolute-incident engagement, pending suggestions, revert rescans,
catalog filtering, per-workflow isolation, re-opening for a known-bad prompt that returns."""
import json

from controller import incidents, settings
from controller.desired import PlanContext
from controller.detectors import halluc as hl
from controller.detectors import halluc_data as hd
from tests.controller.test_halluc_units import WF, FakeClient, ctx_for, new_body, state_with_known_good


def _inc(store, body, opened=4000):
    inc_id = incidents.open_incident(store, "w", "halluc", opened, body)
    return {"id": inc_id, "opened_minute": opened, "body": body}


def _day0_body(**over):
    body = hl._new_body("P2", None, {"rate": 0.2, "base_rate": 0.0, "n": 400, "p": 0.0}, {}, "bad_prompt_day0")
    body.update({"candidates": [{"prompt": "P3", "validation_run_id": "v", "pass_rate": 1.0, "n": 300, "source": "llm"}],
                 "target": "P3", "reverts_checked": True, "suggested": True} | over)
    return body


def _day0_state(p3_counts):
    st = hd.fresh_state()
    hd._apply_group(st, "P2", -10, [400, 30, 360.0, 200], -600, -500)  # bad prompt: 50% engagement
    hd._apply_group(st, "P3", 100, p3_counts, 5940, 6000)
    return st


# ------------------------------------------------------------------ 1. absolute incidents check engagement
def test_absolute_incident_rejects_a_safe_but_engagement_killing_fix(store):
    body = _day0_body()
    inc = _inc(store, body)
    st = _day0_state([500, 3, 460.0, 200])  # 40% engaged vs the bad prompt's 50%
    hl._verify(ctx_for(store, FakeClient(), now=6000), "w", inc, st, "P3")
    assert body["tried"] == ["P3"]
    assert store.conn.execute("select count(*) from decisions where kind='incident_verify_failed'").fetchone()[0] == 1
    assert store.conn.execute("select state from incidents").fetchone()[0] == "open"


def test_absolute_incident_closes_when_engagement_holds(store):
    body = _day0_body()
    inc = _inc(store, body)
    hl._verify(ctx_for(store, FakeClient(), now=6000), "w", inc, _day0_state([500, 3, 460.0, 240]), "P3")
    assert store.conn.execute("select state from incidents").fetchone()[0] == "closed"


def test_absolute_incident_without_comparable_engagement_records_it(store):
    body = _day0_body()
    inc = _inc(store, body)
    st = hd.fresh_state()
    hd._apply_group(st, "P3", 100, [500, 3, 460.0, 200], 5940, 6000)  # bad prompt has no connects at all
    hl._verify(ctx_for(store, FakeClient(), now=6000), "w", inc, st, "P3")
    row = store.conn.execute("select inputs from decisions where kind='incident_verified'").fetchone()
    assert row is not None and json.loads(row[0])["engagement_compared"] is False


# ------------------------------------------------------------------ 2. pending suggestions are retried
def test_pending_suggestions_are_validated_later_without_repaying(store):
    client = FakeClient()
    body = new_body()
    body.update({"kind": "bad_prompt_day0", "pending_suggestions": ["P3"], "suggested": True, "reverts_checked": True})
    inc = _inc(store, body)
    hl._manage(ctx_for(store, client), "w", inc, hd.fresh_state(), "P2")
    assert client.validations == [("P3", 300)] and client.suggest_calls == 0
    assert body["target"] == "P3" and body["pending_suggestions"] == []


def test_pending_suggestions_are_retried_while_a_failed_fix_is_live(store):
    client = FakeClient()
    body = new_body()
    body.update({"kind": "bad_prompt_day0", "pending_suggestions": ["P4"], "suggested": True, "reverts_checked": True,
                 "tried": ["P3"], "candidates": [{"prompt": "P3", "validation_run_id": "v", "pass_rate": 1.0,
                                                  "n": 300, "source": "llm"}]})
    hl._manage(ctx_for(store, client), "w", _inc(store, body), hd.fresh_state(), "P3")
    assert body["target"] == "P4" and client.suggest_calls == 0


# ------------------------------------------------------------------ 3. revert rescan after a failed revert
def test_second_known_good_prompt_is_tried_before_paying_for_suggestions(store):
    client = FakeClient()
    for run_id, prompt in (("va", "P1"), ("vb", "P4")):
        incidents.store_validation(store, {"validation_run_id": run_id, "workflow_id": "w", "prompt_version": prompt,
                                           "n": 300, "pass_rate": 0.99}, 3, 0)
    st = state_with_known_good()
    st["prompts"]["P4"] = {"total": [9000, 130, 8000.0, 4500], "hours": {}, "first_ts": -4000, "last_ts": 2000}
    body = new_body()
    body.update({"reverts_checked": True, "tried": ["P1"], "candidates": [
        {"prompt": "P1", "validation_run_id": "va", "pass_rate": 0.99, "n": 300, "source": "known_good"}]})
    hl.prepare_fix(ctx_for(store, client), "w", body, st, "P1")
    assert body["target"] == "P4" and client.suggest_calls == 0 and client.validations == []


# ------------------------------------------------------------------ 4. catalog filter and isolation
def test_prompt_outside_the_workflow_catalog_is_never_offered(store):
    client = FakeClient()
    for run_id, prompt in (("va", "P1"), ("vb", "P4")):
        incidents.store_validation(store, {"validation_run_id": run_id, "workflow_id": "w", "prompt_version": prompt,
                                           "n": 300, "pass_rate": 0.99}, 3, 0)
    st = state_with_known_good()
    st["prompts"]["P4"] = {"total": [9000, 130, 8000.0, 4500], "hours": {}, "first_ts": -4000, "last_ts": 3500}  # newest
    wf = {**WF, "options": {"prompt_version": ["P1", "P2"]}}
    body = new_body()
    hl.prepare_fix(PlanContext(store, client, {"w": wf}, 5000), "w", body, st, "P2")
    assert body["target"] == "P1" and [c["prompt"] for c in body["candidates"]] == ["P1"]
    assert all(p != "P4" for p, _ in client.validations)


def test_one_workflow_failing_does_not_block_the_next(store, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_HALLUC", True)
    monkeypatch.setattr(hl, "day0_validation", lambda ctx: None)
    seen = []

    def fake_detect(ctx, wid, st, live):
        seen.append(wid)
        if wid == "wA":
            raise RuntimeError("boom")

    monkeypatch.setattr(hl, "_detect", fake_detect)
    for wid in ("wA", "wB"):
        store.store_config_body(wid, 1, None, {"prompt_version": "P1"})
    hl.halluc_detector(PlanContext(store, FakeClient(), {"wA": WF, "wB": WF}, 5000))
    assert seen == ["wA", "wB"]
    assert store.conn.execute("select count(*) from alerts where code='halluc_workflow_error' and workflow_id='wA'"
                              ).fetchone()[0] == 1


# ------------------------------------------------------------------ 5. a known-bad prompt that returns reopens
def _bad_returns_state(reason):
    st = state_with_known_good()
    hd._apply_group(st, "P2", 80, [400, 40, 360.0, 200], 4900, 5000)
    hd.mark_bad(st, "P2", 4000, reason)
    return st


def test_known_bad_prompt_that_returns_live_opens_a_new_incident(store):
    st = _bad_returns_state("prompt_flag_spike")
    hl._detect(ctx_for(store, FakeClient(), now=5000), "w", st, "P2")
    (inc,) = incidents.open_incidents(store, "w", "halluc")
    assert inc["body"]["bad_prompt"] == "P2" and inc["body"]["baseline_prompt"] == "P1"


def test_returning_day0_bad_prompt_reopens_as_absolute_incident(store):
    st = _bad_returns_state("bad_prompt_day0")
    hl._detect(ctx_for(store, FakeClient(), now=5000), "w", st, "P2")
    (inc,) = incidents.open_incidents(store, "w", "halluc")
    assert inc["body"]["kind"] == "bad_prompt_day0" and inc["body"]["baseline_prompt"] is None


def test_false_alarm_cooldown_still_prevents_reopening(store):
    st = _bad_returns_state("prompt_flag_spike")
    st["cleared"] = {"P2": 4900}
    hl._detect(ctx_for(store, FakeClient(), now=5000), "w", st, "P2")
    assert store.count("incidents") == 0
