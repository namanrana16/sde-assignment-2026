"""A2: sibling prompt safety check (unit + fake-client behaviour)."""
from types import SimpleNamespace

from controller import settings
from controller.desired import PlanContext
from controller.detectors import halluc_data as hd
from controller.detectors import prompt_safety as ps
from controller.incidents import store_validation, validations_of
from controller.store import Store


def run(rid, prompt, n, fails):
    return {"run_id": rid, "workflow_id": "w", "prompt_version": prompt, "n": n, "pass_rate": round(1 - fails / n, 4)}


def test_clearly_safer_needs_disjoint_bounds():
    live = [run("a", "P4", 300, 8)]
    assert ps.compare(live, [run("b", "P3", 200, 0)])["clear"]
    assert not ps.compare(live, [run("b", "P3", 200, 1)])["clear"]  # upper bound still overlaps
    assert not ps.compare(live, [run("b", "P3", 200, 8)])["clear"]


def test_promising_but_unseparated():
    c = ps.compare([run("a", "P4", 300, 8)], [run("b", "P3", 200, 1)])
    assert c["promising"] and not c["clear"]


def test_no_clear_sibling_for_equal_prompts():
    c = ps.compare([run("a", "P1", 300, 3)], [run("b", "P3", 200, 2)])
    assert not c["clear"] and not c["promising"]


class FakeClient:
    def __init__(self, fails, credits=200.0):
        self.fails, self.credits, self.calls = dict(fails), credits, []

    def credits_remaining(self):
        return self.credits

    def run_validation(self, wid, prompt, n):
        self.credits -= -(-n // 100)
        self.calls.append((prompt, n))
        f = self.fails[prompt]
        return {"validation_run_id": f"val_{len(self.calls)}", "workflow_id": wid, "prompt_version": prompt, "n": n,
                "failures": f, "pass_rate": round(1 - f / n, 4), "credits_remaining": self.credits}

    state = None


def ctx_for(tmp_path, client, prompts=("P1", "P2", "P3", "P4", "P5"), min_pass=0.97, n_wf=1, live_fails=8):
    store = Store(tmp_path / "s.db")
    wf = {"workflow_id": "w", "options": {"prompt_version": list(prompts)},
          "governance": {"min_validation_pass_rate": min_pass}}
    store_validation(store, {"validation_run_id": "val_live", "workflow_id": "w", "prompt_version": "P4", "n": 300,
                             "pass_rate": round(1 - live_fails / 300, 4)}, 3.0, 0)
    workflows = {f"w{i}": dict(wf, workflow_id=f"w{i}") for i in range(n_wf)}
    workflows["w"] = wf
    return PlanContext(store, client, workflows, 0)


def test_check_workflow_picks_lowest_bound_in_catalogue_order_and_caps_siblings(tmp_path):
    client = FakeClient({"P1": 0, "P2": 0, "P3": 0, "P4": 8, "P5": 0})
    ctx = ctx_for(tmp_path, client)
    out = ps._check_workflow(ctx, "w", "P4")
    assert [p for p, _ in client.calls] == ["P1", "P2", "P3"]  # catalogue order, at most SIBLING_MAX
    assert all(n == settings.SIBLING_N for _, n in client.calls)
    assert out["phase"] == ps.RECOMMENDED and out["candidate"] == "P1"  # tie on bound: catalogue order
    assert out["evidence"]["validation_run_id"] == "val_1"


def test_check_workflow_respects_credit_reserve(tmp_path):
    client = FakeClient({"P1": 0, "P2": 0, "P3": 0, "P4": 8}, credits=75.0 + 1.0)  # reserve 25 x 3 workflows
    ctx = ctx_for(tmp_path, client, n_wf=2)
    out = ps._check_workflow(ctx, "w", "P4")
    assert client.credits >= settings.CREDIT_RESERVE_PER_WORKFLOW * 3 - 1e-9
    assert any("skipped" in v for v in out["checked"].values())


def test_topup_runs_once_for_a_promising_sibling(tmp_path):
    client = FakeClient({"P1": 1, "P2": 9, "P3": 9, "P4": 8})
    ctx = ctx_for(tmp_path, client)
    ps._check_workflow(ctx, "w", "P4")
    assert [c for c in client.calls if c[0] == "P1"] == [("P1", 200), ("P1", settings.SIBLING_TOPUP_N)]
    assert len([c for c in client.calls if c[0] == "P2"]) == 1


def test_no_recommendation_when_live_is_fine(tmp_path):
    client = FakeClient({"P1": 3, "P2": 3, "P3": 3, "P4": 3})
    ctx = ctx_for(tmp_path, client)
    assert ps._check_workflow(ctx, "w", "P4")["phase"] is None


def test_lifecycle_applied_then_dropped_when_supervisor_changes_prompt(tmp_path):
    ctx = ctx_for(tmp_path, FakeClient({}))
    st = {"phase": ps.RECOMMENDED, "live": "P4", "candidate": "P3"}
    assert ps._advance(ctx, "w", st, "P3") and st["phase"] == ps.APPLIED and st["old"] == "P4"
    assert ps._advance(ctx, "w", st, "P2") and st["phase"] == ps.DROPPED


def test_immaterial_live_failure_rate_is_not_switched_even_if_bounds_separate(tmp_path):
    c = ps.compare([run("a", "P1", 300, 6)], [run("b", "P5", 500, 0)], ceiling=0.03)
    assert c["clear"] and not c["material"]  # 2% observed vs 0%: noise between two prompts inside the tolerance


def test_live_prompt_gets_one_second_look_before_an_immaterial_switch(tmp_path):
    client = FakeClient({"P1": 0, "P2": 9, "P3": 9, "P4": 6})  # live P4: 5/300 then 6/300 more
    ctx = ctx_for(tmp_path, client, live_fails=5)
    out = ps._check_workflow(ctx, "w", "P4")
    assert [c for c in client.calls if c[0] == "P4"] == [("P4", settings.SIBLING_TOPUP_N)]
    assert out["phase"] is None  # pooled live 11/600 = 1.8%: lower bound 1.1% < half the 3% tolerance
