"""Part-1 audit fixes: safety-DID impact, p-value text, readable why header."""
import pytest

from controller import audit_render, dryrun, stats
from controller import proposal_repo as pr
from controller.detectors import did, did_plan
from tests.controller.helpers import make_controller, proposal_states, run_until, uniform_hours


@pytest.fixture()
def ran_store(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: any(s in (pr.APPLIED, pr.MONITORING) for s in proposal_states(store).values()))
    for _ in range(8):
        ctl.tick()
    return store


def test_evacuation_impact_scales_with_share_moved_and_deficit():
    cur = {"A": 0.5, "B": 0.5}
    step = {"A": 0.4, "B": 0.6}
    # moved 0.10 of traffic off A, A picks up 40% worse -> 0.04 / 0.10 lift scale = 0.4
    assert did_plan.evacuation_impact([(cur, step)], {"A": 0.4}) == pytest.approx(0.04 / dryrun.IMPACT_FULL_LIFT)
    assert did_plan.evacuation_impact([(cur, step)], {"A": 0.9}) == pytest.approx(0.9 * 0.1 / dryrun.IMPACT_FULL_LIFT)
    assert did_plan.evacuation_impact([({"A": 1.0}, {"B": 1.0})], {"A": 0.9}) == 1.0  # clipped
    assert did_plan.evacuation_impact([(cur, step)], {}) == 0.0
    assert did_plan.evacuation_impact([(cur, cur)], {"A": 0.4}) == 0.0


def test_flagged_evacuation_change_carries_an_impact_override():
    st = {"flagged": {"A": {"q_est": 0.5}}, "weak": {}}
    cfg = {"allocations": {"default": {"caller_id": {"A": 0.5, "B": 0.5}}}, "cohorts": []}
    moves = [({"A": 0.5, "B": 0.5}, {"A": 0.3, "B": 0.7})]
    ev = type("E", (), {})()
    ch = did._change("wf", {"allocations": {}}, {"A": "flagged", "B": "tested"}, st, ev, ["x"], 0.2, cfg, moves)
    assert ch.evidence["impact_override"] == pytest.approx(0.2 * 0.5 / dryrun.IMPACT_FULL_LIFT)


@pytest.mark.parametrize("p,text", [(0.0, "p<1e-300"), (1e-320, "p<1e-300"), (3.2e-7, "p=3.2e-07"),
                                    (0.0123, "p=1.2e-02")])
def test_p_text_never_prints_zero(p, text):
    assert stats.p_text(p) == text


def test_p_underflow_is_formatting_only():
    assert stats.norm_sf(40.0) == 0.0 and stats.p_text(stats.norm_sf(40.0)) == "p<1e-300"


def test_header_is_at_most_six_lines():
    rep = {"workflow": "w", "at": "Day 1 12:00", "version": {"version": 2, "actor": "controller_auto", "ts_minute": 1500,
           "changed_dims": "caller_id", "proposal_id": "w:1", "approval_id": None},
           "proposal": {"proposal_id": "w:1", "route": "auto", "state": "CLOSED", "dims": ["caller_id"], "tv": {},
                        "summary": "s", "approval": None, "events": [], "applied_minute": 1500, "superseded_by": []},
           "decisions": [], "later": [], "config": None, "evidence": [], "monitoring": [], "next_change": None}
    assert len(audit_render.header_lines(rep)) <= 6


def test_rollback_classification_and_lifecycle_text():
    from controller import audit_story
    assert audit_story.is_rollback("schedule_rollback", "x", None)
    assert audit_story.is_rollback("experiment_update", "Revert arm", None)
    assert audit_story.is_rollback("experiment_update", "x", {"kind": "experiment_revert"})
    assert not audit_story.is_rollback("did_probe", "Probe caller IDs", None)
    p = {"proposal_id": "w:1", "route": "auto", "state": "CLOSED", "base_version": 1, "applied_version": 2,
         "applied_minute": 1500, "summary": "s", "rationale": "r", "impact_score": 0.1, "affected_leads": 3,
         "tv": {}, "approval": None, "dry_run": None, "superseded_by": [],
         "events": [{"ts_minute": 2900, "event": "CLOSED", "payload": {"note": "monitoring window over"}}]}
    rb = {"version": 4, "at": "Day 1 12:15", "kind": "schedule_rollback", "reason": "p<1e-300"}
    text = "\n".join(audit_render._proposal_lines(p, rb, False))
    assert "rolled back by v4 at Day 1 12:15: schedule_rollback, p<1e-300" in text


def test_default_hides_inputs_and_verbose_shows_them(ran_store):
    from controller import audit
    rep = audit.why_at(ran_store, "nw_renewal", 1 * 1440 + 60)
    assert "inputs:" not in audit_render.render_at(rep)
    assert "inputs:" in audit_render.render_at(rep, verbose=True)
    assert audit_render.render_at(rep).startswith("SUMMARY")
    assert "WHO:" in audit_render.render_at(rep)
