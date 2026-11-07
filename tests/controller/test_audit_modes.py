"""The three `why` modes on a store built by an in-process sim run (tests may import sim)."""
import json

import pytest

from controller import audit, audit_render
from controller import proposal_repo as pr
from controller.__main__ import main as cli_main
from tests.controller.helpers import make_controller, proposal_states, run_until, uniform_hours


@pytest.fixture()
def ran(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: any(s in (pr.APPLIED, pr.MONITORING) for s in proposal_states(store).values()))
    for _ in range(8):
        ctl.tick()
    return store


def test_parse_bound_day_covers_whole_day():
    assert audit.parse_bound("Day 2") == 2 * 1440
    assert audit.parse_bound("Day 4", end=True) == 5 * 1440
    assert audit.parse_bound("Day 4 10:00", end=True) == 4 * 1440 + 600


def test_at_mode_reports_version_proposal_approval_and_next(ran):
    rep = audit.why_at(ran, "nw_renewal", 1 * 1440 + 60)
    p = rep["proposal"]
    assert rep["version"]["actor"] == "controller_approved" and p["approval"]["approval_id"] == p["approval_id"]
    assert p["approval"]["decided_at"] and p["approval"]["approver"].startswith("policy approver")
    assert rep["decisions"] and any(e["event"] == "APPLIED" for e in p["events"])
    assert rep["next_change"] is None
    text = audit_render.render_at(rep)
    for needle in ("active version", "approval apr_", "proposal lifecycle", "post-apply monitoring"):
        assert needle in text
    earlier = audit.why_at(ran, "nw_renewal", 0)
    assert earlier["version"]["version"] == 1 and earlier["next_change"]["version"]["version"] == 2


def test_range_mode_lists_config_and_proposal_events(ran):
    rep = audit.why_range(ran, "nw_renewal", 0, 3 * 1440)
    types = [e["type"] for e in rep["events"]]
    assert "config_observed" in types and "proposal_approved" in types and "proposal_applied" in types
    assert "proposal_draft" not in types and "advance_intent" not in types
    assert "no events" in audit_render.render_range(audit.why_range(ran, "nope", 0, 10))


def test_lead_mode_joins_calls_to_versions(ran):
    row = ran.conn.execute("select l.lead_id, c.day from calls c join leads l on l.lid=c.lid"
                           " order by c.event_id desc limit 1").fetchone()
    rep = audit.why_lead(ran, row[0], row[1])
    assert rep["calls"] and all(c["day"] == row[1] for c in rep["calls"])
    c = rep["calls"][0]
    assert c["config_version"] in rep["versions"] and c["prompt_active_version"] in rep["versions"]
    assert "allocation from config" in audit_render.render_lead(rep)
    assert audit.why_lead(ran, "missing")["lead"] is None


def test_cli_modes_and_json(ran, capsys):
    ran.close()
    base = ["why", "--db", str(ran.path)]
    assert cli_main(base + ["--workflow", "nw_renewal", "--from", "Day 0", "--to", "Day 2"]) == 0
    assert "timeline nw_renewal" in capsys.readouterr().out
    assert cli_main(base + ["--workflow", "nw_renewal", "--at", "Day 1 12:00", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "at"
    from controller.store import Store
    with Store(ran.path) as s:
        lead = s.conn.execute("select lead_id from leads limit 1").fetchone()[0]
    assert cli_main(base + ["--lead", lead, "--day", "0"]) == 0
    assert "lead " in capsys.readouterr().out
    assert cli_main(base + ["--workflow", "nw_renewal"]) == 2
    assert cli_main(["why", "--db", str(ran.path) + ".nope", "--lead", "x"]) == 1


def test_approval_decision_is_shown_once_with_sim_and_observation_times(ran):
    p = audit.why_at(ran, "nw_renewal", 1 * 1440 + 60)["proposal"]
    decisions = [e for e in p["events"] if e["event"].upper() in ("APPROVED", "REJECTED")]
    assert len(decisions) == 1  # the sim event and our state change are one decision
    d = decisions[0]
    assert d["decided_at"] <= d["observed_at"]  # sim decision time precedes the tick that observed it
    ap = p["approval"]
    assert ap["decided_at"] and ap["observed_at"] and ap["decided_minute"] == d["decided_at"]
    text = audit_render.render_at(audit.why_at(ran, "nw_renewal", 1 * 1440 + 60))
    assert "decided_at" in text and "observed_at" in text


def test_workflow_policy_is_stored_at_startup_and_printed(ran):
    pol = audit.workflow_policy(ran, "nw_renewal")
    assert pol["approver_hours"] == [10, 19] and pol["approver_latency_minutes"] == [30, 240]
    assert pol["objective"]["primary"] == "engagement_rate"
    text = audit_render.render_at(audit.why_at(ran, "nw_renewal", 1 * 1440 + 60))
    assert "approver hours [10, 19]" in text and "latency [30, 240]" in text
