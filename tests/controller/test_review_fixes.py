"""Phase 2 review fixes: apply ordering/halting, reconcile resilience, config builder hygiene."""
import copy

import pytest

from controller import client as c
from controller import configlib as cl
from controller import proposal_repo as pr
from controller.cohorts import FeatureVocab
from controller.governance import Governance
from controller.planner import Planner
from controller.desired import ROUTE_APPROVAL, DesiredChange, Priority, Registry

WF = {
    "workflow_id": "w", "dialling_window": [9, 13], "cadence": [2], "mid_flight": "allowed",
    "daily_batch": {"size": 100},
    "options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2"],
                "caller_id": ["D1", "D2", "D3"], "prompt_version": ["P1", "P2"]},
    "governance": {"approver_hours": [10, 19], "approver_latency_minutes": [30, 240],
                   "proposal_ttl_hours": 24, "max_requests_per_24h": 3,
                   "auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.5}},
    "objective": {"primary": "engagement_rate", "guardrail": {}},
}
CFG = cl.sim_normalise(WF, {
    "cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                               "caller_id": {"D1": 0.1, "D2": 0.2, "D3": 0.7}}},
    "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}})


class FakeClient:
    def __init__(self, workflows=("w",)):
        self.applied: list[str] = []
        self.apply_error: Exception | None = None
        self.approval_status = "approved"
        self.get_approval_error: Exception | None = None
        self.list_error: Exception | None = None
        self.workflows = workflows

    def get_config(self, wid):
        return {"version": 1, "actor": "x", "config": copy.deepcopy(CFG)}

    def apply_approval(self, wid, aid):
        if self.apply_error:
            raise self.apply_error
        self.applied.append(aid)
        return {"version": 2, "changed_dims": []}

    def get_approval(self, aid):
        if self.get_approval_error:
            raise self.get_approval_error
        return {"status": self.approval_status}

    def list_approvals(self, workflow_id=None, status=None):
        if self.list_error:
            raise self.list_error
        return []

    def config_history(self, wid):
        return []


def approved_row(gov, wid, seq, priority, apply_by, kind="k"):
    row = pr.ProposalRow(f"{wid}:{seq}:{kind}", wid, kind, pr.INTENT, None, 1, 0, pr.ROUTE_APPROVAL,
                         priority, seq, None, None, None, None, None, None, {"request": {}})
    gov.repo.create_intent(row, 0)
    row = gov.repo.transition(row, pr.PENDING, 0, "p", approval_id=f"apr_{wid}_{seq}")
    return gov.repo.transition(row, pr.APPROVED, 0, "a", apply_by_minute=apply_by)


@pytest.fixture()
def gov(store):
    wf2 = {**WF, "workflow_id": "v"}
    return Governance(FakeClient(), store, {"w": WF, "v": wf2})


def test_apply_order_is_priority_then_apply_by_then_seq(gov):
    approved_row(gov, "w", 1, Priority.ROUTINE, 100)
    approved_row(gov, "v", 2, Priority.ROUTINE, 90)
    approved_row(gov, "w", 3, Priority.INCIDENT, 500)
    order = []
    gov._apply = lambda row, now: order.append(row.proposal_id)  # type: ignore[method-assign]
    gov.apply_approved(50)
    assert order == ["w:3:k", "v:2:k", "w:1:k"]


@pytest.mark.parametrize("err", [c.OutcomeUnknown("POST", "/apply", "boom"), c.SimUnavailable("down")])
def test_unknown_apply_outcome_skips_rest_of_workflow_and_drops_cache(gov, err):
    approved_row(gov, "w", 1, Priority.INCIDENT, 100)
    approved_row(gov, "w", 2, Priority.ROUTINE, 100)
    approved_row(gov, "v", 3, Priority.ROUTINE, 100)
    gov.client.apply_error = err
    gov.live("w")
    assert gov._live_cache
    gov.apply_approved(50)
    states = {r.proposal_id: r.state for r in gov.repo.by_state(pr.APPROVED)}
    assert states["w:2:k"] == pr.APPROVED and states["w:1:k"] == pr.APPROVED
    assert gov.client.applied == []  # the second w row was never attempted; v raised too
    assert "w" in gov._skip_apply


def test_unknown_outcome_does_not_block_other_workflows(gov):
    approved_row(gov, "w", 1, Priority.INCIDENT, 100)
    approved_row(gov, "v", 2, Priority.ROUTINE, 100)
    real = gov.client.apply_approval

    def flaky(wid, aid):
        if wid == "w":
            raise c.OutcomeUnknown("POST", "/apply", "x")
        return real(wid, aid)
    gov.client.apply_approval = flaky
    gov.apply_approved(50)
    assert gov.repo.get("v:2:k").state == pr.APPLIED


def test_reconcile_helpers_survive_sim_errors(gov):
    row = approved_row(gov, "w", 1, Priority.ROUTINE, 100)
    for err in (c.SimUnavailable("x"), c.OutcomeUnknown("GET", "/a", "x"), c.NotFound(404, "nf", {})):
        gov.client.get_approval_error = err
        gov._reconcile_apply_outcome(row, 10)  # must not raise
        assert gov.repo.get(row.proposal_id).state == pr.APPROVED
    for err in (c.SimUnavailable("x"), c.OutcomeUnknown("GET", "/a", "x"), c.NotFound(404, "nf", {})):
        gov.client.list_error = err
        assert gov._find_approval(row) is not None  # LOOKUP_FAILED sentinel, never an exception


def test_not_approved_with_unexpected_status_is_terminal_and_alerts(gov, store):
    row = approved_row(gov, "w", 1, Priority.ROUTINE, 100)
    gov.client.apply_error = c.NotApproved(409, "not_approved", {})
    gov.client.approval_status = "rejected"
    gov.apply_approved(50)
    assert gov.repo.get(row.proposal_id).state == pr.STALE
    assert store.conn.execute("select count(*) from alerts where code='apply_unexpected_status'").fetchone()[0] == 1
    gov.apply_approved(51)  # not retried forever
    assert gov.client.applied == []


def test_note_once_survives_restart(store):
    g1 = Governance(FakeClient(), store, {"w": WF})
    assert g1.note_once("k", 100) is True and g1.note_once("k", 200) is False
    g2 = Governance(FakeClient(), store, {"w": WF})
    assert g2.note_once("k", 300) is False  # same day: still suppressed after a restart
    assert g2.note_once("k", 1500) is True


def test_daily_bundle_not_marked_done_when_submit_failed(store):
    gov = Governance(FakeClient(), store, {"w": WF})
    planner = Planner(gov.client, store, gov, Registry())
    ch = DesiredChange("w", "daily_bundle", Priority.ROUTINE, {"prompt_version": "P1"}, ROUTE_APPROVAL, {}, "x")
    gov.can_file = lambda *a, **k: (True, "")  # type: ignore[method-assign]
    gov.submit = lambda *a, **k: pr.ProposalRow("w:1:k", "w", "k", pr.SUBMIT_FAILED, None, 1, 0, "approval",
                                                2, 1, None, None, None, None, None, None, {})  # type: ignore
    planner._draft = lambda *a, **k: object()  # type: ignore[method-assign]
    planner._plan_approval(WF, 1, CFG, [ch], 700)
    assert planner.job_due("daily_bundle", "w", 700)
    gov.submit = lambda *a, **k: None  # type: ignore[method-assign]
    planner._plan_approval(WF, 1, CFG, [ch], 700)
    assert planner.job_due("daily_bundle", "w", 700)
    gov.submit = lambda *a, **k: pr.ProposalRow("w:2:k", "w", "k", pr.PENDING, None, 1, 0, "approval",
                                                2, 2, None, None, None, None, None, None, {})  # type: ignore
    planner._plan_approval(WF, 1, CFG, [ch], 700)
    assert not planner.job_due("daily_bundle", "w", 700)


# ------------------------------------------------------------------ configlib
VOCAB = FeatureVocab({"lang": ["hi", "en"], "city_tier": ["1"], "age_band": ["18-30"], "value_band": ["mid"]})


def test_cohort_values_checked_against_vocab():
    ok = {"cohorts": [{"id": "a", "where": {"lang": ["hi"]}}]}
    assert cl.apply_patch(WF, CFG, ok, VOCAB)["cohorts"][0]["id"] == "a"
    with pytest.raises(cl.ProposalError, match="unknown lang values"):
        cl.apply_patch(WF, CFG, {"cohorts": [{"id": "a", "where": {"lang": ["xx"]}}]}, VOCAB)
    # an empty vocabulary (cold start before any lead) cannot reject anything
    cl.apply_patch(WF, CFG, {"cohorts": [{"id": "a", "where": {"lang": ["xx"]}}]}, FeatureVocab())


def test_unpatched_dims_are_copied_bit_for_bit_and_ulp_is_no_change():
    skew = copy.deepcopy(CFG)
    skew["allocations"]["default"]["caller_id"] = {"D1": 0.1 + 1e-16 * 3, "D2": 0.2, "D3": 0.7 - 1e-16}
    new = cl.apply_patch(WF, skew, {"allocations": {"default": {"voice": {"V1": 1, "V2": 1}}}})
    assert new["allocations"]["default"]["caller_id"] == skew["allocations"]["default"]["caller_id"]
    assert cl.changed_dims(skew, new) == ["voice"]
    # a pure ulp difference is not a change and carries no TV
    assert cl.changed_dims(CFG, skew) == []
    assert cl.max_shift(CFG, skew) < 1e-9
    assert cl.apply_patch(WF, skew, {"allocations": {"default": {"caller_id": {"D1": 0.1, "D2": 0.2, "D3": 0.7}}}}) \
        is not None
    assert cl.changed_dims(skew, cl.apply_patch(WF, skew, {"allocations": {"default": {
        "caller_id": {"D1": 0.1, "D2": 0.2, "D3": 0.7}}}})) == []
