"""Governance + planner against the real sim in-process (shortened public_01)."""
import pytest

from controller import proposal_repo as pr
from controller.desired import ROUTE_AUTO, DesiredChange, Priority
from tests.controller.helpers import (HOURS, make_controller, proposal_states, run_until, sim_kinds,
                                      uniform_hours)

BAD_KINDS = {"apply_conflict", "approval_lapsed", "auto_apply_rejected"}


def states(store):
    return proposal_states(store)


def first_pid(store):
    return store.conn.execute("select proposal_id from proposals order by plan_seq limit 1").fetchone()[0]


def assert_clean_sim_log(world):
    bad = BAD_KINDS & set(sim_kinds(world))
    assert not bad, bad


def current(client, wid="nw_renewal"):
    return client.get_config(wid)


def test_no_producers_means_no_proposals_and_no_changes(client, store, world):
    ctl = make_controller(client, store)
    assert run_until(ctl, lambda: client.state()["sim_minute"] >= 1500)
    assert store.count("proposals") == 0 and not world.approvals
    assert current(client)["version"] == 1


def test_submit_approve_apply_end_to_end(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: any(s in (pr.APPLIED, pr.MONITORING) for s in states(store).values()))
    pid = first_pid(store)
    assert pid == "nw_renewal:1:daily_bundle"
    cfg = current(client)
    assert cfg["version"] == 2 and cfg["actor"] == "controller_approved"
    assert set(cfg["config"]["schedule"]["hour_weights"]["default"]) == {str(h) for h in range(9, 21)}
    # the sim holds exactly the body we persisted before sending, with every approver field filled
    (aid, a), = world.approvals.items()
    stored = store.conn.execute("select body, approval_id from proposals where proposal_id=?", (pid,)).fetchone()
    assert stored[1] == aid and a["status"] == "applied" and a["proposal_id"] == pid
    req = a["request"]
    assert req["change_summary"] and req["diff"] and req["dry_run"] and req["rationale"]
    assert isinstance(req["affected_leads"], int) and 0 <= req["impact_score"] <= 1
    assert a["_reasons"] == []  # no hygiene reasons: nothing soft-invalid was sent
    events = [r[0] for r in store.conn.execute("select event from proposal_events where proposal_id=? order by id", (pid,))]
    assert events[:3] == ["DRAFT", "INTENT", "PENDING"] and "APPROVED" in events and "APPLIED" in events
    # approval_decided events are linked to our proposal and ingested once
    ctl.tick()
    ctl.tick()
    assert store.conn.execute("select count(*) from proposal_events where approval_id=? and event_id is not null",
                              (aid,)).fetchone()[0] == 1
    # the config_changed event carries our proposal id and is observed in our store
    assert store.conn.execute("select proposal_id from config_versions where workflow_id='nw_renewal' and version=2"
                              ).fetchone()[0] == pid
    # filed only inside approver hours (10-19)
    created = a["created_minute"] % 1440
    assert 600 <= created < 1140
    assert_clean_sim_log(world)
    # the producer no longer wants anything: no further proposals
    assert run_until(ctl, lambda: client.state()["sim_minute"] >= 1500)
    assert store.count("proposals") == 1 and len(world.approvals) == 1


def test_stale_base_replans_without_calling_apply(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    # another actor changes the config while our approval is pending
    cfg = current(client)
    body = cfg["config"]
    body["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    client.put_config("nw_renewal", {"proposal_id": "other-actor", "base_version": cfg["version"],
                                     "proposed_config": body, "rationale": "interleaved auto-apply"})
    assert run_until(ctl, lambda: states(store).get("nw_renewal:1:daily_bundle") == pr.STALE)
    assert run_until(ctl, lambda: any(s in (pr.MONITORING, pr.APPLIED) for s in states(store).values()))
    st = states(store)
    assert st["nw_renewal:1:daily_bundle"] == pr.STALE
    second = [p for p in st if p != "nw_renewal:1:daily_bundle"]
    assert len(second) == 1 and second[0].startswith("nw_renewal:2:") and st[second[0]] in (pr.APPLIED, pr.MONITORING)
    # the stale approval was never applied; no graded conflict or lapse was logged
    assert world.approvals[store.conn.execute("select approval_id from proposals where proposal_id=?",
                                              ("nw_renewal:1:daily_bundle",)).fetchone()[0]]["status"] != "applied"
    assert_clean_sim_log(world)
    assert current(client)["version"] == 3  # other actor (v2) then our re-planned proposal (v3)
    base2 = store.conn.execute("select base_version from proposals where plan_seq=2").fetchone()[0]
    assert base2 == 2  # re-planned against the fresh base


def test_approved_but_base_changed_before_apply_is_never_applied(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    aid = store.conn.execute("select approval_id from proposals").fetchone()[0]
    # make the approver decide at the next advance, then change the base in the same breath
    world.approvals[aid]["decide_minute"] = world.now
    cfg = current(client)
    cfg["config"]["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    client.put_config("nw_renewal", {"proposal_id": "x1", "base_version": cfg["version"],
                                     "proposed_config": cfg["config"], "rationale": "r"})
    ctl.tick()
    ctl.tick()
    assert states(store)["nw_renewal:1:daily_bundle"] == pr.STALE
    assert world.approvals[aid]["status"] == "approved"  # approved, but we never called /apply
    assert_clean_sim_log(world)


def test_expired_is_a_rejection_and_replans_with_a_new_id(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    aid = store.conn.execute("select approval_id from proposals").fetchone()[0]
    a = world.approvals[aid]
    a["decide_minute"] = a["expires_minute"] + 600  # a slower approver than the policy promised
    assert run_until(ctl, lambda: pr.EXPIRED in states(store).values(), limit=900)
    first_base = store.conn.execute("select base_version from proposals where plan_seq=1").fetchone()[0]
    assert run_until(ctl, lambda: len(states(store)) >= 2, limit=900)
    st = states(store)
    assert st["nw_renewal:1:daily_bundle"] == pr.EXPIRED
    second = [p for p in st if p != "nw_renewal:1:daily_bundle"][0]  # re-planned: new id, current base
    assert second.startswith("nw_renewal:2:") and store.conn.execute(
        "select base_version from proposals where plan_seq=2").fetchone()[0] == first_base
    assert_clean_sim_log(world)


def test_rejected_counts_against_fatigue_and_is_not_applied(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    aid = store.conn.execute("select approval_id from proposals").fetchone()[0]
    world.approvals[aid]["_decision"], world.approvals[aid]["_reasons"] = "rejected", ["approver declined (judgement call)"]
    assert run_until(ctl, lambda: pr.REJECTED in states(store).values())
    now = client.state()["sim_minute"]
    assert ctl.gov.requests_used("nw_renewal", now) == 1  # rejected still occupies a slot for 24 h
    assert current(client)["version"] == 1
    # one routine bundle per day: nothing more is filed today even though the change is still wanted
    assert run_until(ctl, lambda: client.state()["sim_minute"] >= 1380)
    assert len(states(store)) == 1
    assert_clean_sim_log(world)


def test_superseding_incident_carries_every_still_wanted_change(client, store, world):
    incident = DesiredChange("nw_renewal", "incident", Priority.INCIDENT,
                             {"allocations": {"default": {"welcome_message": {"W1": 0.9, "W2": 0.1}}}},
                             summary="incident: shift welcome")
    wanted = {"on": False}
    ctl = make_controller(client, store, uniform_hours(),
                          lambda ctx, wid: [incident] if wanted["on"] and wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    wanted["on"] = True
    assert run_until(ctl, lambda: len(states(store)) >= 2)
    a, b = list(states(store))[:2]
    row_b = ctl.gov.repo.get(b)
    assert row_b.supersedes == a and row_b.priority == Priority.INCIDENT and row_b.kind == "incident"
    req = row_b.body["request"]
    assert set(row_b.body["dims"]) == {"schedule", "welcome_message"}  # incident + the still-wanted bundle
    assert req["proposed_config"]["schedule"]["hour_weights"]["default"]
    assert len(world.approvals) == 2
    assert run_until(ctl, lambda: all(s in (pr.APPLIED, pr.SUPERSEDED, pr.MONITORING) for s in
                                      list(states(store).values())[:2]))
    st = states(store)
    winners = [p for p in (a, b) if st[p] in (pr.APPLIED, pr.MONITORING)]
    losers = [p for p in (a, b) if st[p] == pr.SUPERSEDED]
    assert len(winners) == 1 and len(losers) == 1
    loser_aid = ctl.gov.repo.get(losers[0]).approval_id
    assert world.approvals[loser_aid]["status"] != "applied"  # never applied
    assert_clean_sim_log(world)  # no lapse, no conflict


def test_same_priority_second_request_waits_p1(client, store, world):
    other = DesiredChange("nw_renewal", "extra", Priority.ROUTINE,
                          {"allocations": {"default": {"welcome_message": {"W1": 0.8, "W2": 0.2}}}})
    ctl = make_controller(client, store, uniform_hours(), lambda ctx, wid: [other] if wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    for _ in range(8):
        ctl.tick()
    inflight = [p for p, s in states(store).items() if s in (pr.PENDING, pr.APPROVED, pr.INTENT)]
    assert len(inflight) <= 1 and len(world.approvals) == 1  # one bundle, one in-flight approval


def test_priority_order_incident_over_severe_over_bundle(client, store, world):
    mk = lambda kind, prio, w: DesiredChange("nw_renewal", kind, prio, {"allocations": {"default": {"voice": w}}},
                                             summary=kind)
    ctl = make_controller(client, store, uniform_hours(),
                          lambda c, wid: [mk("harm_stop", Priority.SEVERE, {"V1": 0.8, "V2": 0.2}),
                                          mk("incident", Priority.INCIDENT, {"V1": 0.7, "V2": 0.3})]
                          if wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    row = ctl.gov.repo.get(first_pid(store))
    assert row.kind == "incident" and row.priority == Priority.INCIDENT
    # the most urgent change wins a conflicting key; every other wanted change rides along
    voice = row.body["request"]["proposed_config"]["allocations"]["default"]["voice"]
    assert voice == {"V1": pytest.approx(0.7), "V2": pytest.approx(0.3)}
    assert "schedule" in row.body["dims"]


def auto_spread():
    return DesiredChange("nw_renewal", "did_spread", Priority.ROUTINE,
                         {"allocations": {"default": {"caller_id": {"D1": 0.4, "D2": 0.4, "D3": 0.2}}}},
                         route_preference=ROUTE_AUTO, summary="spread DIDs")


def test_auto_apply_when_idle_and_policy_allows(client, store, world):
    ctl = make_controller(client, store, lambda c, wid: [auto_spread()] if wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.AUTO_APPLIED in states(store).values() or pr.MONITORING in states(store).values())
    assert current(client)["actor"] == "controller_auto" and not world.approvals
    assert_clean_sim_log(world)


def test_auto_apply_waits_while_an_approval_is_in_flight_p3(client, store, world):
    on = {"auto": False}
    ctl = make_controller(client, store, uniform_hours(),
                          lambda c, wid: [auto_spread()] if on["auto"] and wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    on["auto"] = True
    for _ in range(3):
        ctl.tick()
    assert current(client)["version"] == 1 and not any(r[0] == "auto" for r in
                                                       store.conn.execute("select route from proposals"))
    assert run_until(ctl, lambda: any(r[0] == "auto" for r in store.conn.execute("select route from proposals")))
    assert states(store)["nw_renewal:1:daily_bundle"] in (pr.APPLIED, pr.MONITORING, pr.REJECTED, pr.STALE)
    assert_clean_sim_log(world)


def test_flagged_evacuation_auto_applies_despite_in_flight_approval(client, store, world):
    on = {"auto": False}
    evac = DesiredChange("nw_renewal", "did_evacuation", Priority.INCIDENT,
                         {"allocations": {"default": {"caller_id": {"D1": 0.2, "D2": 0.8}}}},
                         route_preference=ROUTE_AUTO, evidence={"flag_evacuation": True}, summary="evacuate D1")
    ctl = make_controller(client, store, uniform_hours(), lambda c, wid: [evac] if on["auto"] and wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    on["auto"] = True
    assert run_until(ctl, lambda: current(client)["version"] == 2)
    assert current(client)["actor"] == "controller_auto"
    assert run_until(ctl, lambda: states(store)["nw_renewal:1:daily_bundle"] == pr.STALE)  # re-planned
    assert_clean_sim_log(world)


def test_policy_that_forbids_auto_falls_back_to_approval(client, store, world):
    kyc = DesiredChange("nw_kyc", "did_spread", Priority.ROUTINE,
                        {"allocations": {"default": {"caller_id": {"D1": 0.6, "D2": 0.4}}}},
                        route_preference=ROUTE_AUTO, summary="spread DIDs")
    ctl = make_controller(client, store, lambda c, wid: [kyc] if wid == "nw_kyc" else [])
    assert run_until(ctl, lambda: store.count("proposals") >= 1)
    route = store.conn.execute("select route from proposals").fetchone()[0]
    assert route == "approval" and len(world.approvals) == 1
    assert "auto_apply_rejected" not in sim_kinds(world)  # no graded 403: the pre-check redirected it


def test_locally_invalid_change_is_never_sent(client, store, world):
    bad = DesiredChange("nw_renewal", "prompt_swap", Priority.INCIDENT, {"prompt_version": "P3"},
                        summary="swap prompt without evidence")
    ctl = make_controller(client, store, lambda c, wid: [bad] if wid == "nw_renewal" else [])
    for _ in range(40):
        ctl.tick()
    assert not world.approvals and store.count("proposals") == 0
    assert store.conn.execute("select count(*) from alerts where code='proposal_invalid_locally'").fetchone()[0] == 1
    assert store.conn.execute("select count(*) from decisions where kind='proposal_invalid_locally'").fetchone()[0] == 1


def test_prompt_change_with_passing_validation_evidence_is_filed(client, store, world):
    run = client.run_validation("nw_renewal", "P3", 300)
    store.conn.execute("insert into validation_runs(run_id, workflow_id, prompt_version, n, pass_rate, credits,"
                       " sim_minute) values (?,?,?,?,?,?,?)",
                       (run["validation_run_id"], "nw_renewal", "P3", 300, run["pass_rate"], 3, 0))
    prompt = DesiredChange("nw_renewal", "incident", Priority.INCIDENT, {"prompt_version": "P3"},
                           evidence={"validation_run_id": run["validation_run_id"], "impact_override": 0.5},
                           summary="revert prompt")
    ctl = make_controller(client, store, lambda c, wid: [prompt] if wid == "nw_renewal" else [])
    if run["pass_rate"] >= 0.97:
        assert run_until(ctl, lambda: world.approvals)
        a = next(iter(world.approvals.values()))
        assert a["request"]["evidence"] == {"validation_run_id": run["validation_run_id"]}
        assert a["_reasons"] in ([], ["approver declined (judgement call)"])
    else:
        for _ in range(40):
            ctl.tick()
        assert not world.approvals  # below the bar: never sent


def test_late_approval_is_not_applied_and_logs_no_lapse(client, store, world):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    aid = store.conn.execute("select approval_id from proposals").fetchone()[0]
    a = world.approvals[aid]
    a["decide_minute"] = world.now + 15
    ctl.tick()  # decision arrives during this advance
    client.advance(15)
    ctl.gov.reconcile(world.now)  # record APPROVED
    row = ctl.gov.repo.get("nw_renewal:1:daily_bundle")
    if row.state == pr.PENDING:
        pytest.skip("approver rejected this id")
    assert row.state == pr.APPROVED
    for _ in range(30):  # jump past apply_by without the controller applying
        client.advance(15)
    ctl.gov.apply_approved(world.now)
    assert ctl.gov.repo.get("nw_renewal:1:daily_bundle").state == pr.LAPSED
    assert a["status"] == "approved"  # /apply was never called
    assert store.conn.execute("select count(*) from alerts where code='approval_lapsed_locally'").fetchone()[0] == 1
    assert_clean_sim_log(world)


def _change(kind, prio, weights):
    return DesiredChange("nw_renewal", kind, prio, {"allocations": {"default": {"voice": weights}}}, summary=kind)


def test_rollback_and_safety_fix_in_the_same_tick_share_one_slot(client, store, world):
    rollback = _change("rollback", Priority.SEVERE, {"V1": 0.8, "V2": 0.2})
    fix = DesiredChange("nw_renewal", "halluc_fix", Priority.INCIDENT,
                        {"allocations": {"default": {"welcome_message": {"W1": 0.9, "W2": 0.1}}}}, summary="fix")
    ctl = make_controller(client, store, lambda c, wid: [rollback, fix] if wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    assert len(states(store)) == 1  # one merged proposal, one slot (P2)
    row = ctl.gov.repo.get(first_pid(store))
    assert row.kind == "halluc_fix" and {"voice", "welcome_message"} <= set(row.body["dims"])


def test_safety_fix_supersedes_an_in_flight_rollback_using_the_reserved_slot(client, store, world):
    rollback = _change("rollback", Priority.SEVERE, {"V1": 0.8, "V2": 0.2})
    fix = DesiredChange("nw_renewal", "halluc_fix", Priority.INCIDENT,
                        {"allocations": {"default": {"welcome_message": {"W1": 0.9, "W2": 0.1}}}}, summary="fix")
    on = {"fix": False}
    ctl = make_controller(client, store, lambda c, wid: ([rollback] + ([fix] if on["fix"] else []))
                          if wid == "nw_renewal" else [])
    assert run_until(ctl, lambda: pr.PENDING in states(store).values())
    on["fix"] = True
    assert run_until(ctl, lambda: len(states(store)) >= 2)
    a, b = list(states(store))[:2]
    row_b = ctl.gov.repo.get(b)
    assert row_b.supersedes == a and row_b.priority == Priority.INCIDENT
    assert {"voice", "welcome_message"} <= set(row_b.body["dims"])  # the rollback rides along
    assert ctl.gov.requests_used("nw_renewal", client.state()["sim_minute"]) == 2


def test_rollbacks_cannot_spend_the_safety_slot_but_the_safety_fix_still_files(client, store, world):
    """Budget 3 (nw_renewal): rollbacks stop at the second-to-last slot; the incident still files."""
    from controller import fatigue
    ctl = make_controller(client, store, lambda c, wid: [])
    gov = ctl.gov.workflows["nw_renewal"]["governance"]
    assert gov["max_requests_per_24h"] == 3
    assert fatigue.slot_available(1, gov, Priority.SEVERE, "nw_renewal")[0] is True
    assert fatigue.slot_available(2, gov, Priority.SEVERE, "nw_renewal")[0] is False
    assert fatigue.slot_available(2, gov, Priority.INCIDENT, "nw_renewal")[0] is True
