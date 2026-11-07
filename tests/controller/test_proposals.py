"""Local validation of every approver soft check and the auto-apply policy; C10 helpers."""
import dataclasses

import pytest

from controller import client as c
from controller import dryrun
from controller.cohorts import VOCAB_META_KEY, FeatureVocab
from controller.configlib import apply_patch
from controller.desired import DesiredChange, Priority
from controller.ingest import Ingestor, mirror_configs
from controller.proposals import (ProposalDraft, build_draft, validate_approval_fields,
                                  validate_auto_apply)

WF_RENEWAL = {"workflow_id": "nw_renewal", "governance": {
    "min_validation_pass_rate": 0.97,
    "auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"], "max_shift": 0.5}}}
WF_KYC = {"workflow_id": "nw_kyc", "governance": {
    "min_validation_pass_rate": 0.97, "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0}}}
CFG = {"prompt_version": "P3"}


def draft(**kw):
    base = dict(workflow_id="nw_renewal", kind="daily_bundle", base_version=1, proposed_config=CFG,
                dims=("schedule",), max_shift=0.0, tv={}, change_summary="s", diff={"a": 1},
                affected_leads=10, dry_run={"m": 1}, impact_score=0.5, rationale="r",
                evidence={}, expires_in_hours=None)
    base.update(kw)
    return ProposalDraft(**base)


def no_runs(_):
    return None


def test_valid_draft_passes():
    assert validate_approval_fields(draft(), WF_RENEWAL, no_runs) == []


@pytest.mark.parametrize("field,value,fragment", [
    ("change_summary", "   ", "change_summary"), ("change_summary", "", "change_summary"),
    ("diff", {}, "diff is empty"), ("dry_run", {}, "dry_run is empty"),
    ("rationale", "", "rationale"), ("rationale", " \n", "rationale"),
    ("affected_leads", -1, "affected_leads"), ("affected_leads", 1.5, "affected_leads"),
    ("affected_leads", True, "affected_leads"), ("affected_leads", None, "affected_leads"),
    ("impact_score", 1.01, "impact_score"), ("impact_score", -0.01, "impact_score"),
    ("impact_score", "0.5", "impact_score"), ("impact_score", None, "impact_score"),
    ("dims", (), "no change"),
])
def test_each_soft_field_is_rejected_locally(field, value, fragment):
    problems = validate_approval_fields(draft(**{field: value}), WF_RENEWAL, no_runs)
    assert any(fragment in p for p in problems), problems


def test_boundary_values_accepted():
    for score in (0, 1, 0.0, 1.0):
        assert validate_approval_fields(draft(impact_score=score), WF_RENEWAL, no_runs) == []
    assert validate_approval_fields(draft(affected_leads=0), WF_RENEWAL, no_runs) == []


def run(**kw):
    return {"workflow_id": "nw_renewal", "prompt_version": "P3", "pass_rate": 0.98, **kw}


@pytest.mark.parametrize("lookup,evidence,ok", [
    (lambda r: run(), {"validation_run_id": "v1"}, True),
    (lambda r: run(pass_rate=0.97), {"validation_run_id": "v1"}, True),
    (lambda r: run(pass_rate=0.969), {"validation_run_id": "v1"}, False),
    (lambda r: run(workflow_id="nw_kyc"), {"validation_run_id": "v1"}, False),
    (lambda r: run(prompt_version="P1"), {"validation_run_id": "v1"}, False),
    (lambda r: None, {"validation_run_id": "v1"}, False),
    (lambda r: run(), {}, False),
])
def test_prompt_change_needs_matching_passing_validation(lookup, evidence, ok):
    d = draft(dims=("prompt_version",), evidence=evidence)
    assert (validate_approval_fields(d, WF_RENEWAL, lookup) == []) is ok


def test_auto_apply_policy():
    assert validate_auto_apply(draft(dims=("caller_id",), max_shift=0.5), WF_RENEWAL) == []
    assert validate_auto_apply(draft(dims=("schedule", "caller_id"), max_shift=0.2), WF_RENEWAL) == []
    assert any("not permitted" in p for p in validate_auto_apply(draft(dims=("voice",)), WF_RENEWAL))
    assert any("not permitted" in p for p in validate_auto_apply(draft(dims=("cohorts",)), WF_RENEWAL))
    assert any("not permitted" in p for p in validate_auto_apply(draft(dims=("prompt_version",)), WF_RENEWAL))
    assert any("max_shift" in p for p in validate_auto_apply(draft(dims=("caller_id",), max_shift=0.51), WF_RENEWAL))
    assert any("rationale" in p for p in validate_auto_apply(draft(rationale=" "), WF_RENEWAL))
    assert any("no change" in p for p in validate_auto_apply(draft(dims=()), WF_RENEWAL))
    assert any("not enabled" in p for p in validate_auto_apply(draft(dims=("caller_id",)), WF_KYC))


@pytest.mark.parametrize("patch,rationale", [
    ({"allocations": {"default": {"caller_id": {"D1": 0.4, "D2": 0.4, "D3": 0.2}}}}, "spread"),
    ({"allocations": {"default": {"caller_id": {"D3": 1}}}}, "too big"),
    ({"allocations": {"default": {"voice": {"V1": 0.9, "V2": 0.1}}}}, "voice"),
    ({"prompt_version": "P3"}, "prompt"),
    ({"hour_weights": {"default": {str(h): 1 for h in range(9, 21)}}}, "sched"),
    ({"hour_weights": {"default": {str(h): 1 for h in range(9, 21)}}}, ""),
    ({}, "nothing"),
])
def test_local_auto_apply_check_agrees_with_the_sim(client, world, patch, rationale):
    """Pre-checks accept exactly what PUT /config accepts, so no graded 403 is ever logged."""
    wf = {w["workflow_id"]: w for w in client.workflows()}["nw_renewal"]
    version, cur = client.get_config("nw_renewal")["version"], client.get_config("nw_renewal")["config"]
    new = apply_patch(wf, cur, patch)
    from controller.configlib import changed_dims, max_shift
    d = draft(workflow_id="nw_renewal", dims=tuple(changed_dims(cur, new)), max_shift=max_shift(cur, new),
              rationale=rationale, proposed_config=new, base_version=version)
    local_ok = validate_auto_apply(d, wf) == []
    try:
        client.put_config("nw_renewal", {"proposal_id": "x", "base_version": version,
                                         "proposed_config": new, "rationale": rationale})
        sim_ok = True
    except c.Forbidden:
        sim_ok = False
    except c.Unprocessable:  # sim also refuses a no-op before the policy check
        sim_ok = False
    assert local_ok == sim_ok


# ------------------------------------------------------------------ C10
@pytest.fixture()
def ingested(client, store, world):
    mirror_configs(client, store, [w["workflow_id"] for w in client.workflows()])
    Ingestor(client, store).sync()
    return client, store, {w["workflow_id"]: w for w in client.workflows()}


def change(patch, **kw):
    return DesiredChange("nw_renewal", kw.pop("kind", "daily_bundle"), kw.pop("priority", Priority.ROUTINE),
                         patch, evidence=kw.pop("evidence", {}), summary=kw.pop("summary", "s"))


def test_dry_run_and_fields_for_an_allocation_change(ingested):
    client, store, wfs = ingested
    wf = wfs["nw_renewal"]
    cfg = client.get_config("nw_renewal")
    vocab = FeatureVocab.load(store.get_meta(VOCAB_META_KEY))
    d = build_draft(store, vocab, wf, cfg["version"], cfg["config"],
                    [change({"allocations": {"default": {"voice": {"V1": 0.5, "V2": 0.5}}}})], 0)
    assert d.dims == ("voice",) and d.max_shift == pytest.approx(0.5)
    arms = d.dry_run["allocations"][0]["arms"]
    assert arms["V1"]["status"] == "observed" and arms["V2"]["status"] == "no_evidence"  # never observed
    assert d.dry_run["allocations"][0]["evidence"] == "partial"
    assert isinstance(d.affected_leads, int) and d.affected_leads >= 0
    assert d.impact_score == 0  # an unobserved arm earns no lift
    assert d.diff["allocations"]["default"]["voice"]["new"] == {"V1": 0.5, "V2": 0.5}
    assert validate_approval_fields(d, wf, lambda r: None) == []


def test_observed_arms_give_a_ci_and_bounded_impact(ingested):
    client, store, wfs = ingested
    wf = wfs["nw_renewal"]
    cfg = client.get_config("nw_renewal")
    vocab = FeatureVocab.load(store.get_meta(VOCAB_META_KEY))
    # caller ids D1/D2 are both observed in history: shifting between them is fully evidenced
    d = build_draft(store, vocab, wf, cfg["version"], cfg["config"],
                    [change({"allocations": {"default": {"caller_id": {"D1": 0.9, "D2": 0.1}}}})], 0)
    e = d.dry_run["allocations"][0]
    assert e["evidence"] == "observed" and len(e["ci95_new"]) == 2 and e["ci95_new"][0] <= e["expected_new"]
    assert 0 <= d.impact_score <= 1
    assert d.tv["default/caller_id"] == pytest.approx(0.4) and d.max_shift == pytest.approx(0.4)


def test_prompt_change_is_validation_only_and_schedule_has_replay(ingested):
    client, store, wfs = ingested
    wf = wfs["nw_renewal"]
    cfg = client.get_config("nw_renewal")
    vocab = FeatureVocab.load(store.get_meta(VOCAB_META_KEY))
    other = next(p for p in wf["options"]["prompt_version"] if p != cfg["config"]["prompt_version"])
    d = build_draft(store, vocab, wf, cfg["version"], cfg["config"],
                    [change({"prompt_version": other, "hour_weights": {"default": {"10": 2, "11": 1}}},
                            evidence={"validation_run_id": "val_00001", "impact_override": 0.4})], 0)
    assert "validation-only" in d.dry_run["prompt_version"]["method"]
    assert d.evidence == {"validation_run_id": "val_00001"} and d.impact_score == 0.4
    assert d.dry_run["schedule"] and "hour re-weighting" in d.dry_run["schedule"][0]["method"]
    assert d.affected_leads >= 0 and d.dims == ("prompt_version", "schedule")


def test_affected_leads_respects_next_campaign_only(ingested, world):
    client, store, wfs = ingested
    client.advance(9 * 60)  # a campaign exists with active leads at 09:00 of day 0
    Ingestor(client, store).sync()
    vocab = FeatureVocab.load(store.get_meta(VOCAB_META_KEY))
    wf = dict(wfs["nw_renewal"])
    cfg = client.get_config("nw_renewal")["config"]
    sched = apply_patch(wf, cfg, {"hour_weights": {"default": {"10": 3}}})
    voice = apply_patch(wf, cfg, {"allocations": {"default": {"voice": {"V1": 0.5, "V2": 0.5}}}})
    now = client.state()["sim_minute"]
    active = sum(dryrun.active_cell_counts(store, "nw_renewal", now, len(wf["cadence"])).values())
    assert active > 0
    assert dryrun.affected_leads(store, vocab, wf, cfg, sched, now) == active  # schedule hits every active lead
    assert dryrun.affected_leads(store, vocab, wf, cfg, voice, now) == active  # mid_flight: allowed
    locked = dict(wf, mid_flight="next_campaign_only")
    nxt = dryrun.affected_leads(store, vocab, locked, cfg, voice, now)
    assert 0 < nxt <= wf["daily_batch"]["size"]  # only the expected next-batch share
    assert dryrun.affected_leads(store, vocab, wf, cfg, cfg, now) == 0


def test_impact_formula():
    dr = {"traffic_share": {"a": 0.5, "b": 0.5},
          "allocations": [{"cohort": "a", "rel_lift_lb": 0.10}, {"cohort": "b", "rel_lift_lb": -0.2}]}
    assert dryrun.impact_score(dr, "engagement_rate") == 0.5  # 0.5 * 0.10 / 0.10
    assert dryrun.impact_score({"traffic_share": {"a": 1}, "allocations": [{"cohort": "a", "rel_lift_lb": 0.3}]},
                               "engagement_rate") == 1.0  # clipped
    assert dryrun.impact_score({}, "engagement_rate") == 0
    assert dryrun.impact_score(dr, "x", override=2) == 1.0 and dryrun.impact_score(dr, "x", override=-1) == 0
    sched = {"traffic_share": {"a": 1}, "schedule": [{"cohort": "a", "evidence": "observed", "rel_lift": 0.1}]}
    assert dryrun.impact_score(sched, "engagement_rate") == 0  # schedule moves pickup only
    assert dryrun.impact_score(sched, "pickup_rate") == pytest.approx(0.5)  # haircut, no CI on a replay


def test_draft_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        draft().rationale = "x"


def test_cohorts_are_never_auto_applied_even_if_policy_lists_them():
    wf = {"workflow_id": "x", "governance": {"auto_apply": {
        "enabled": True, "allowed_dims": ["schedule", "cohorts"], "max_shift": 0.5}}}
    assert validate_auto_apply(draft(dims=("schedule",)), wf) == []
    assert any("cohort" in p for p in validate_auto_apply(draft(dims=("schedule", "cohorts")), wf))
