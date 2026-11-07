"""Unit tests: segment test, fix selection, credit reserve, tracker segments."""
import json

import pytest

from controller import incidents, settings
from controller.desired import PlanContext
from controller.detectors import halluc as hl
from controller.detectors import halluc_data as hd

WF = {"workflow_id": "w", "governance": {"min_validation_pass_rate": 0.97},
      "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}}


# ------------------------------------------------------------------ segment test
def test_segment_test_triggers_on_a_real_spike_with_enough_connects():
    base = [14000, 206, 14000 * 0.93, 7000]
    seg = [250, 14, 250 * 0.85, 120]  # 5.6% vs 1.5%
    t = hl.segment_test(seg, base)
    assert t["triggered"] and t["p"] < settings.HALLUC_P


def test_segment_test_needs_min_connects_and_ignores_noise():
    base = [14000, 206, 13000.0, 7000]
    assert not hl.segment_test([120, 12, 110.0, 60], base)["triggered"]  # high rate, too few connects
    assert not hl.segment_test([400, 7, 372.0, 200], base)["triggered"]  # 1.75%: noise
    assert not hl.segment_test([400, 2, 372.0, 200], base)["triggered"]  # lower is never an alert


def test_borderline_rate_is_not_rescued_by_an_adherence_drop():
    """Review fix 5: the old 10x-alpha 'corroborated' branch is gone; p must clear alpha on its own."""
    base = [14000, 206, 14000 * 0.93, 7000]
    seg = [500, 18, 500 * 0.80, 250]  # 3.6% with a big adherence drop: p between 1e-4 and 1e-3
    t = hl.segment_test(seg, base)
    assert 1e-4 <= t["p"] < 1e-3 and not t["triggered"]


def test_small_counts_use_the_exact_test():
    from controller.stats import fisher_greater
    base = [600, 3, 560.0, 300]  # small baseline: exact Fisher, not the pooled z
    t = hl.segment_test([250, 9, 230.0, 120], base)
    assert t["p"] == pytest.approx(fisher_greater(9, 250, 3, 600))


def test_ceiling_test_is_baseline_free():
    assert hl.ceiling_test([600, 40, 500.0, 300], 0.03)["triggered"]  # 6.7%, lower bound well above 3%
    assert not hl.ceiling_test([600, 15, 500.0, 300], 0.03)["triggered"]  # 2.5%
    assert not hl.ceiling_test([100, 20, 90.0, 50], 0.03)["triggered"]  # too few connects


def test_day0_verdict_is_conclusive_only_when_the_upper_bound_misses_the_bar():
    assert hl._day0_verdict([{"run_id": "a", "n": 300, "pass_rate": 0.90}], 0.97)["definite"]
    borderline = [{"run_id": "a", "n": 300, "pass_rate": 0.96}]
    v = hl._day0_verdict(borderline, 0.97)
    assert not v["definite"] and not v["confirmed"]
    assert hl._day0_verdict(borderline + [{"run_id": "b", "n": 300, "pass_rate": 0.955}], 0.97)["confirmed"]
    assert hl._day0_verdict(borderline + [{"run_id": "b", "n": 300, "pass_rate": 0.99}], 0.97) is None


def test_drift_uses_a_stricter_level():
    base = [13000, 170, 12000.0, 7000]
    seg = [1000, 27, 930.0, 500]  # 2.7% vs 1.3%: p ~ 3e-4, an unlucky day, not an incident
    assert not hl.segment_test(seg, base, settings.HALLUC_P_DRIFT)["triggered"]


def test_engagement_guardrail():
    base = [1000, 15, 930.0, 500]
    assert hl.engagement_ok([400, 5, 372.0, 190], base, 0.02)[0]  # 47.5% vs 50%: within noise
    assert not hl.engagement_ok([2000, 20, 1860.0, 800], base, 0.02)[0]  # 40%: strict-script trap


# ------------------------------------------------------------------ fix ranking
def test_ranking_uses_failure_upper_bound_not_just_the_bar():
    runs = [{"prompt": "P5", "pass_rate": 0.9933, "n": 300}, {"prompt": "P3", "pass_rate": 1.0, "n": 300},
            {"prompt": "P4", "pass_rate": 0.9733, "n": 300}, {"prompt": "P9", "pass_rate": 0.96, "n": 300}]
    ranked = hl.rank_by_failure_bound(runs, 0.97)
    assert [r["prompt"] for r in ranked] == ["P3", "P5", "P4"]  # P9 below the bar is dropped
    assert ranked[0]["fail_ub"] < ranked[1]["fail_ub"] < ranked[2]["fail_ub"]


# ------------------------------------------------------------------ credits
def test_credit_reserve_rules():
    assert incidents.can_spend(200, 3, 3)  # routine, far above 3 x 25
    assert not incidents.can_spend(77, 3, 3)  # would dip below the 75 reserve
    assert incidents.can_spend(40, 3, 3, incident_spent=0)  # incident may use the reserve
    assert not incidents.can_spend(2, 3, 3, incident_spent=0)  # never more than we have (no 402)
    assert not incidents.can_spend(200, 3, 3, incident_spent=24)  # one incident is capped at the reserve
    assert incidents.validation_cost(300) == 3 and incidents.validation_cost(50) == 1


class FakeClient:
    def __init__(self, credits=200.0, suggestions=("P3", "P4", "P5")):
        self.credits = credits
        self.suggest_calls = 0
        self.validations: list[tuple[str, int]] = []
        self.suggestions = suggestions
        self.rates = {"P1": 0.9933, "P3": 1.0, "P4": 0.9733, "P5": 0.9967, "P2": 0.9}

    def credits_remaining(self):
        return self.credits

    def suggest_fixes(self, wid, problem):
        self.suggest_calls += 1
        self.credits -= 3
        return {"suggestions": [{"prompt_version": p, "summary": ""} for p in self.suggestions],
                "credits_remaining": self.credits}

    def run_validation(self, wid, prompt, n):
        self.validations.append((prompt, n))
        self.credits -= -(-n // 100)
        return {"validation_run_id": f"val_{len(self.validations):05d}", "workflow_id": wid,
                "prompt_version": prompt, "n": n, "failures": 0, "pass_rate": self.rates[prompt],
                "credits_remaining": self.credits}


def ctx_for(store, client, now=5000, n_wf=3):
    return PlanContext(store, client, {f"w{i}": WF for i in range(n_wf)} | {"w": WF}, now)


def new_body(stats=None):
    return hl._new_body("P2", "P1", stats or {"rate": 0.055, "base_rate": 0.015, "n": 300, "p": 1e-6},
                        {"actor": "supervisor", "minute": 4000}, "prompt_flag_spike")


def state_with_known_good():
    st = hd.fresh_state()
    st["prompts"] = {"P1": {"total": [14000, 210, 13000.0, 7000], "hours": {}, "first_ts": -5000, "last_ts": 3000}}
    return st


def test_revert_to_known_good_uses_stored_validation_and_no_credits(store):
    client = FakeClient()
    incidents.store_validation(store, {"validation_run_id": "val_x", "workflow_id": "w", "prompt_version": "P1",
                                       "n": 300, "pass_rate": 0.99}, 3, 0)
    body = new_body()
    hl.prepare_fix(ctx_for(store, client), "w", body, state_with_known_good(), "P2")
    assert body["target"] == "P1" and body["candidates"][0]["validation_run_id"] == "val_x"
    assert client.suggest_calls == 0 and client.validations == [] and body["credits_spent"] == 0


def test_without_known_good_asks_llm_validates_all_and_ranks(store):
    client = FakeClient()
    body = new_body()
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 1 and [p for p, _ in client.validations] == ["P3", "P4", "P5"]
    assert all(n == 300 for _, n in client.validations)
    assert [c["prompt"] for c in body["candidates"]] == ["P3", "P5", "P4"]  # lowest failure bound first
    assert body["target"] == "P3" and body["credits_spent"] == 12
    assert all(c["source"] == "llm" for c in body["candidates"])
    rows = store.conn.execute("select count(*) from llm_calls").fetchone()[0]
    assert rows == 4  # suggest + three validations all in the ledger


def test_verify_failure_moves_to_next_candidate_without_new_credit_use(store):
    client = FakeClient()
    body = new_body()
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P2")
    spent = client.credits
    body["tried"].append("P3")
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P3")
    assert body["target"] == "P5" and client.credits == spent


def test_incident_credit_cap_stops_spend_and_alerts(store):
    client = FakeClient()
    body = new_body()
    body["credits_spent"] = 23
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 0 and body["target"] is None  # 23 + 3 > 25: no speculative call
    assert store.conn.execute("select count(*) from alerts where code='suggest_fixes_unaffordable'").fetchone()[0] == 1


def test_low_credits_never_call_the_sim(store):
    client = FakeClient(credits=2.0)
    body = new_body()
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 0 and client.validations == []


def test_failing_known_good_validation_falls_back_to_llm(store):
    client = FakeClient()
    client.rates["P1"] = 0.90
    body = new_body()
    hl.prepare_fix(ctx_for(store, client), "w", body, state_with_known_good(), "P2")
    assert ("P1", 300) in client.validations and client.suggest_calls == 1 and body["target"] == "P3"


def test_revert_candidate_must_be_clearly_better_than_the_live_prompt(store):
    client = FakeClient()
    incidents.store_validation(store, {"validation_run_id": "val_x", "workflow_id": "w", "prompt_version": "P1",
                                       "n": 300, "pass_rate": 0.99}, 3, 0)
    body = new_body({"rate": 0.055, "base_rate": 0.015, "n": 300, "p": 1e-6, "rate_lb": 0.012})
    hl.prepare_fix(ctx_for(store, client), "w", body, state_with_known_good(), "P2")  # P1: 1.5%, ub ~1.7% > 1.2%
    assert body["rejected_reverts"] == ["P1"] and body["candidates"][0]["source"] == "llm"


def test_daily_credit_cap_blocks_incident_spend(store):
    client = FakeClient()
    store.conn.execute("INSERT INTO llm_calls(workflow_id, sim_minute, credits, request, response) VALUES"
                       " ('w', 5000, 29.0, 'x', 'y')")
    body = new_body()
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P2")
    assert client.suggest_calls == 0 and body["target"] is None  # 29 + 3 > 30 today


def test_day0_validation_scales_down_instead_of_skipping(store):
    assert hl._day0_n(ctx_for(store, FakeClient(credits=2.0)), "w", 1) == 200  # cost 2
    assert hl._day0_n(ctx_for(store, FakeClient(credits=1.0)), "w", 1) == 100
    assert hl._day0_n(ctx_for(store, FakeClient(credits=0.5)), "w", 1) is None


def test_verify_requires_non_inferior_flags_and_engagement(store):
    client = FakeClient()
    st = hd.fresh_state()
    st["first_prompt"], st["base"], st["base_frozen"] = "P1", [14000, 84, 13000.0, 7000], True
    body = new_body()
    inc_id = incidents.open_incident(store, "w", "halluc", 4000, body)
    hd._apply_group(st, "P1", 100, [500, 3, 460.0, 240], 5940, 6000)
    hl._verify(ctx_for(store, client, now=6000), "w", {"id": inc_id, "opened_minute": 4000, "body": body}, st, "P1")
    assert store.conn.execute("select state from incidents").fetchone()[0] == "closed"


def test_verify_failure_marks_prompt_bad_and_tries_next(store):
    client = FakeClient()
    st = hd.fresh_state()
    st["first_prompt"], st["base"], st["base_frozen"] = "P1", [14000, 84, 13000.0, 7000], True
    body = new_body()
    body["candidates"] = [{"prompt": "P3", "validation_run_id": "v", "pass_rate": 1.0, "n": 300, "source": "llm"},
                          {"prompt": "P5", "validation_run_id": "w", "pass_rate": 0.99, "n": 300, "source": "llm"}]
    body["target"] = "P3"
    body["reverts_checked"] = body["suggested"] = True
    inc_id = incidents.open_incident(store, "w", "halluc", 4000, body)
    hd._apply_group(st, "P3", 100, [500, 40, 450.0, 240], 5940, 6000)  # 8%: not non-inferior
    hl._verify(ctx_for(store, client, now=6000), "w", {"id": inc_id, "opened_minute": 4000, "body": body}, st, "P3")
    assert body["tried"] == ["P3"] and body["target"] == "P5" and hd.is_bad(st, "P3", 6000)


def test_false_alarm_closes_and_lifts_the_blacklist(store):
    client = FakeClient()
    st = hd.fresh_state()
    st["first_prompt"], st["base"], st["base_frozen"] = "P2", [14000, 210, 13000.0, 7000], True
    body = new_body()
    body["baseline_prompt"] = "P2"
    body["recheck_fail"] = settings.HALLUC_RECHECK_FAILS - 1
    inc_id = incidents.open_incident(store, "w", "halluc", 4000, body)
    hd.mark_bad(st, "P2", 4000, "prompt_flag_spike")
    hd._apply_group(st, "P2", 100, [400, 6, 370.0, 200], 5940, 6000)  # 1.5% = baseline: no spike
    hl._manage(ctx_for(store, client, now=6000), "w", {"id": inc_id, "opened_minute": 4000, "body": body}, st, "P2")
    assert store.conn.execute("select state from incidents").fetchone()[0] == "closed"
    assert not hd.is_bad(st, "P2", 6000)


def test_bad_prompt_blacklist_expires():
    st = hd.fresh_state()
    hd.mark_bad(st, "P2", 1000, "x")
    assert hd.is_bad(st, "P2", 1000 + 100)
    assert not hd.is_bad(st, "P2", 1000 + settings.HALLUC_BAD_EXPIRY_MINUTES)


# ------------------------------------------------------------------ tracker: per-prompt buckets
def add_calls(store, rows):
    for i, (ts, prompt, flag, adh, eng) in enumerate(rows):
        store.conn.execute("INSERT INTO calls(event_id, workflow_id, ts_minute, day, hour, lid, attempt_no,"
                           " config_version, cohort_id, welcome, voice, caller_id, prompt, connected, engaged,"
                           " halluc_flag, adherence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (store.count("calls") + 1, "w", ts, ts // 1440, ts % 1440 // 60, 1, 1, 1, "default",
                            "W1", "V1", "D1", prompt, 1, eng, flag, adh))


def test_tracker_buckets_per_prompt_and_baseline_freezes(store):
    add_calls(store, [(-600 + i, "P1", i % 50 == 0, 0.93, i % 2) for i in range(500)])
    st = hd.update(store, "w")
    assert not st["base_frozen"] and hd.total_counts(st, "P1")[0] == 500
    add_calls(store, [(10 + i, "P1", 0, 0.93, 1) for i in range(100)]
              + [(200 + i, "P2", i % 10 == 0, 0.8, 0) for i in range(60)])
    st = hd.update(store, "w")
    assert st["base_frozen"] and st["base"][0] == 500  # history only, live data never leaks into it
    assert hd.total_counts(st, "P2")[0] == 60 and hd.total_counts(st, "P1")[0] == 600
    assert hd.known_good_candidates(st, "P2", 300)[0][0] == "P1"
    assert hd.known_good_candidates(st, "P1", 300) == []
    assert hd.window_counts(st, "P2", 300)[0] == 60


def test_interleaved_prompts_do_not_thrash_or_pollute(store):
    """next_campaign_only: old P1 and new P2 calls interleave within the same hours."""
    rows = [(2000 + i // 2, "P1" if i % 2 else "P2", 0 if i % 2 else int(i % 10 == 0), 0.9, 1) for i in range(400)]
    add_calls(store, rows)
    st = hd.update(store, "w")
    p2, p1 = hd.window_counts(st, "P2", 2300), hd.window_counts(st, "P1", 2300)
    assert p2[0] == 200 and p2[1] == 40 and p1[0] == 200 and p1[1] == 0  # clean P1, flagged P2, no mixing


def test_tracker_is_incremental_and_restart_safe(store):
    add_calls(store, [(i, "P1", 0, 0.9, 1) for i in range(100)])
    hd.update(store, "w")
    add_calls(store, [(100 + i, "P1", 1, 0.9, 1) for i in range(50)])
    st = hd.update(store, "w")
    again = hd.update(store, "w")
    assert hd.total_counts(st, "P1")[0] == 150 == hd.total_counts(again, "P1")[0]
    assert hd.total_counts(again, "P1")[1] == 50


def test_noisy_single_day0_run_is_inconclusive_not_an_incident(store):
    incidents.store_validation(store, {"validation_run_id": "v1", "workflow_id": "w", "prompt_version": "P1",
                                       "n": 300, "pass_rate": 0.96}, 3, 0)
    st = hd.fresh_state()
    assert hl._detect_day0(ctx_for(store, FakeClient(), now=100), "w", st, "P1") is False
    assert store.count("incidents") == 0 and st["day0"]["P1"] == "inconclusive"
    # a confirmatory failure opens it
    incidents.store_validation(store, {"validation_run_id": "v2", "workflow_id": "w", "prompt_version": "P1",
                                       "n": 300, "pass_rate": 0.95}, 3, 1)
    assert hl._detect_day0(ctx_for(store, FakeClient(), now=101), "w", st, "P1") is True
    assert store.count("incidents") == 1


def _verify_state(prompt_counts):
    st = hd.fresh_state()
    st["first_prompt"], st["base"], st["base_frozen"] = "P1", [17000, 226, 16000.0, 9200], True
    hd._apply_group(st, "P1", 100, prompt_counts, 5940, 6000)
    return st


def test_verify_waits_when_non_inferiority_is_not_yet_resolvable(store):
    """A good fix with a ~1.7% observed rate cannot be shown non-inferior at n=413; that is 'wait', not 'fail'."""
    body = new_body()
    inc_id = incidents.open_incident(store, "w", "halluc", 4000, body)
    st = _verify_state([413, 7, 380.0, 230])
    inc = {"id": inc_id, "opened_minute": 4000, "body": body}
    hl._verify(ctx_for(store, FakeClient(), now=6000), "w", inc, st, "P1")
    assert body["tried"] == [] and store.conn.execute("select state from incidents").fetchone()[0] == "open"
    st = _verify_state([1600, 24, 1480.0, 900])  # enough data: 1.5% vs 1.3% base is non-inferior at the 1pp margin
    hl._verify(ctx_for(store, FakeClient(), now=6000), "w", inc, st, "P1")
    assert store.conn.execute("select state from incidents").fetchone()[0] == "closed"


def test_unvalidated_paid_suggestions_stay_pending_until_affordable(store):
    """D3: a transient spend refusal must not discard the paid suggestions."""
    client = FakeClient(credits=0.0)
    body = new_body()
    body["pending_suggestions"] = ["P3", "P4"]
    hl._validate_suggestions(ctx_for(store, client), "w", body, "P2", 0.97)
    assert body["pending_suggestions"] == ["P3", "P4"] and body["candidates"] == []
    client.credits = 200.0
    hl._validate_suggestions(ctx_for(store, client), "w", body, "P2", 0.97)
    assert body["pending_suggestions"] == [] and len(body["candidates"]) == 2
