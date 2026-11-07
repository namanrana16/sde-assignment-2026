"""Fatigue ledger, timing rules (P4/P5), state machine and jobs, without a simulator."""
import json

import pytest

from controller import fatigue, settings
from controller import proposal_repo as pr
from controller.desired import DesiredChange, Priority, Registry

RENEWAL = {"approver_hours": [10, 19], "approver_latency_minutes": [30, 240], "proposal_ttl_hours": 24,
           "max_requests_per_24h": 3}
KYC = {"approver_hours": [10, 18], "approver_latency_minutes": [60, 480], "proposal_ttl_hours": 24,
       "max_requests_per_24h": 2}
KESTREL = {"approver_hours": [10, 19], "approver_latency_minutes": [20, 120], "proposal_ttl_hours": 12,
           "max_requests_per_24h": 4}


def at(day, hh, mm=0):
    return day * 1440 + hh * 60 + mm


def test_next_working_matches_the_sim_rule():
    assert fatigue.next_working(at(0, 9), [10, 19]) == at(0, 10)
    assert fatigue.next_working(at(0, 12), [10, 19]) == at(0, 12)
    assert fatigue.next_working(at(0, 19), [10, 19]) == at(1, 10)
    assert fatigue.next_working(at(0, 23), [10, 19]) == at(1, 10)


def test_never_files_at_night_or_outside_hours():
    for hh in (0, 3, 9, 19, 22):
        ok, why = fatigue.filing_window_ok(at(1, hh), RENEWAL, False, "nw_renewal")
        assert not ok and "approver hours" in why
    assert fatigue.filing_window_ok(at(1, 10), RENEWAL, False, "nw_renewal")[0]


def test_worst_case_decision_must_precede_expiry():
    # 18:00 + 240 min = 22:00 -> next opening 10:00 is 16 h away: fine with a 24 h TTL
    assert fatigue.filing_window_ok(at(0, 18), RENEWAL, False, "nw_renewal")[0]
    # ks_cancel has a 12 h TTL: the same filing could expire unseen
    ok, why = fatigue.filing_window_ok(at(0, 18), KESTREL, True, "other")
    assert not ok and "expires_at" in why
    assert fatigue.filing_window_ok(at(0, 11), KESTREL, False, "other")[0]
    # a shortened TTL (expires_in_hours) tightens the rule
    assert fatigue.filing_window_ok(at(0, 10), RENEWAL, False, "x", expires_in_hours=24)[0]
    assert not fatigue.filing_window_ok(at(0, 10), RENEWAL, True, "x", expires_in_hours=2)[0]  # best P < routine floor
    assert fatigue.ttl_minutes(RENEWAL, 100) == 24 * 60  # capped at the policy TTL


def test_soft_cap_not_before_11_is_routine_only_and_from_settings():
    assert settings.SOFT_CAPS["ks_cancel"] == {"max_per_day": 2, "not_before_hour": 11}
    ok, why = fatigue.filing_window_ok(at(0, 10, 30), KESTREL, True, "ks_cancel")
    assert not ok and "11:00" in why
    assert fatigue.filing_window_ok(at(0, 10, 30), KESTREL, False, "ks_cancel")[0]  # incident may use 10:00
    assert fatigue.filing_window_ok(at(0, 11), KESTREL, True, "ks_cancel")[0]
    assert fatigue.filing_window_ok(at(0, 10, 30), KESTREL, True, "unlisted_workflow")[0]  # default: none


def tier_ok(used, gov, prio, wid="x"):
    return fatigue.slot_available(used, gov, prio, wid)[0]


@pytest.mark.parametrize("total,routine,severe", [(2, 1, 1), (3, 1, 2), (4, 2, 3)])
def test_slot_tiers_per_budget_size(total, routine, severe):
    gov = {"max_requests_per_24h": total}
    assert fatigue.routine_budget(gov, "x") == min(routine, 2 if total == 4 else routine)
    assert fatigue.severe_budget(gov) == severe
    assert fatigue.slot_limit(gov, Priority.INCIDENT, "x") == total
    # the last slot is safety-only
    assert tier_ok(total - 1, gov, Priority.INCIDENT)
    assert not tier_ok(total - 1, gov, Priority.SEVERE)
    assert not tier_ok(total - 1, gov, Priority.ROUTINE)
    assert not tier_ok(total, gov, Priority.INCIDENT)
    # a rollback may use the second-to-last slot, routine bundles stop earlier when total > 2
    assert tier_ok(total - 2, gov, Priority.SEVERE)
    assert tier_ok(total - 2, gov, Priority.ROUTINE) == (total - 2 < fatigue.routine_budget(gov, "x"))


def test_rollback_never_blocks_a_safety_fix():
    for total in (2, 3, 4):
        gov = {"max_requests_per_24h": total}
        used = 0
        while tier_ok(used, gov, Priority.SEVERE):  # rollbacks spend every slot they are allowed
            used += 1
        assert tier_ok(used, gov, Priority.INCIDENT), total


def test_budget_tiers_on_real_policies():
    assert fatigue.routine_budget(RENEWAL, "nw_renewal") == 1
    assert fatigue.routine_budget(KYC, "nw_kyc") == 1
    assert fatigue.routine_budget(KESTREL, "ks_cancel") == 2  # soft cap 2 = max(1, 4 - 2)
    assert fatigue.routine_budget({"max_requests_per_24h": 1}, "x") == 1  # A4: the single slot is shared
    assert fatigue.slot_available(1, KYC, Priority.ROUTINE, "nw_kyc")[0] is False
    assert fatigue.slot_available(1, KYC, Priority.INCIDENT, "nw_kyc")[0] is True
    assert fatigue.slot_available(2, KYC, Priority.INCIDENT, "nw_kyc")[0] is False


# ------------------------------------------------------------------ ledger over the proposals table
def intent(repo, wid, seq, created, route="approval", priority=2):
    row = pr.ProposalRow(f"{wid}:{seq}:k", wid, "k", pr.INTENT, None, 1, created, route, priority, seq,
                         None, None, None, None, None, None, {"request": {}, "dims": ["voice"]})
    repo.create_intent(row, created)
    return row


@pytest.fixture()
def repo(store):
    return pr.ProposalRepo(store)


def test_ledger_counts_by_created_at_including_rejected(repo):
    a = intent(repo, "w", 1, at(0, 10))
    a = repo.transition(a, pr.PENDING, at(0, 10), approval_id="apr_1", filed_minute=at(0, 10))
    repo.transition(a, pr.REJECTED, at(0, 12))
    b = intent(repo, "w", 2, at(0, 15))
    repo.transition(b, pr.PENDING, at(0, 15), approval_id="apr_2", filed_minute=at(0, 15))
    assert repo.count_recent_requests("w", at(0, 16)) == 2  # the rejected one still counts
    assert repo.count_recent_requests("w", at(1, 9, 59)) == 2  # sim rule: created > now - 24h
    assert repo.count_recent_requests("w", at(1, 10)) == 1  # exactly 24h old: aged out
    assert repo.count_recent_requests("w", at(1, 15, 1)) == 0
    assert repo.count_recent_requests("other", at(0, 16)) == 0


def test_unsent_intent_counts_conservatively_but_stale_never_sent_does_not(repo):
    a = intent(repo, "w", 1, at(0, 10))
    assert repo.count_recent_requests("w", at(0, 11)) == 1
    repo.transition(a, pr.STALE, at(0, 11))
    assert repo.count_recent_requests("w", at(0, 11)) == 0


def test_auto_proposals_do_not_use_fatigue_slots(repo):
    a = intent(repo, "w", 1, at(0, 10), route="auto")
    repo.transition(a, pr.AUTO_APPLIED, at(0, 10), applied_version=2, applied_minute=at(0, 10))
    assert repo.count_recent_requests("w", at(0, 11)) == 0
    assert repo.last_auto_minute("w") == at(0, 10)


# ------------------------------------------------------------------ state machine
def test_every_transition_writes_event_and_decision_rows(repo, store):
    a = intent(repo, "w", 1, at(0, 10))
    n_dec = store.count("decisions")
    a = repo.transition(a, pr.PENDING, at(0, 10), "created", approval_id="apr_1", filed_minute=at(0, 10))
    a = repo.transition(a, pr.APPROVED, at(0, 12), apply_by_minute=at(0, 14))
    a = repo.transition(a, pr.APPLIED, at(0, 12), applied_version=2, applied_minute=at(0, 12))
    a = repo.transition(a, pr.MONITORING, at(0, 13))
    a = repo.transition(a, pr.CLOSED, at(1, 13))
    events = [e[1] for e in repo.events(a.proposal_id)]
    assert events == ["DRAFT", "INTENT", "PENDING", "APPROVED", "APPLIED", "MONITORING", "CLOSED"]
    assert store.count("decisions") == n_dec + 5
    kinds = [r[0] for r in store.conn.execute("select kind from decisions order by id")]
    assert kinds[-1] == "proposal_closed" and "proposal_approved" in kinds
    assert json.loads(store.conn.execute("select inputs from decisions order by id desc limit 1")
                      .fetchone()[0])["proposal_id"] == a.proposal_id


ILLEGAL = [(pr.INTENT, pr.APPROVED), (pr.INTENT, pr.APPLIED), (pr.PENDING, pr.APPLIED),
           (pr.APPROVED, pr.REJECTED), (pr.REJECTED, pr.APPROVED), (pr.APPLIED, pr.APPROVED),
           (pr.CLOSED, pr.MONITORING), (pr.SUPERSEDED, pr.APPLIED), (pr.STALE, pr.PENDING)]


@pytest.mark.parametrize("start,target", ILLEGAL)
def test_illegal_transitions_are_refused(repo, start, target):
    a = intent(repo, "w", 1, at(0, 10))
    walk = {pr.INTENT: [], pr.PENDING: [pr.PENDING], pr.APPROVED: [pr.PENDING, pr.APPROVED],
            pr.APPLIED: [pr.PENDING, pr.APPROVED, pr.APPLIED], pr.REJECTED: [pr.PENDING, pr.REJECTED],
            pr.CLOSED: [pr.PENDING, pr.APPROVED, pr.APPLIED, pr.MONITORING, pr.CLOSED],
            pr.SUPERSEDED: [pr.SUPERSEDED], pr.STALE: [pr.STALE]}[start]
    for step in walk:
        extra = {"approval_id": "apr"} if step == pr.PENDING else {}
        a = repo.transition(a, step, at(0, 10), **extra)
    with pytest.raises(pr.IllegalTransition):
        repo.transition(a, target, at(0, 11))
    assert repo.get(a.proposal_id).state == start  # nothing was written


@pytest.mark.parametrize("path", [
    [pr.PENDING, pr.REJECTED], [pr.PENDING, pr.EXPIRED], [pr.PENDING, pr.STALE],
    [pr.PENDING, pr.SUPERSEDED], [pr.PENDING, pr.APPROVED, pr.LAPSED],
    [pr.PENDING, pr.APPROVED, pr.STALE], [pr.PENDING, pr.APPROVED, pr.SUPERSEDED],
    [pr.SUPERSEDED], [pr.SUBMIT_FAILED], [pr.STALE], [pr.AUTO_APPLIED, pr.MONITORING, pr.CLOSED]])
def test_legal_paths(repo, path):
    a = intent(repo, "w", 1, at(0, 10))
    for step in path:
        extra = {"approval_id": "apr"} if step == pr.PENDING else {}
        a = repo.transition(a, step, at(0, 10), **extra)
    assert a.state == path[-1]


def test_transition_is_atomic_when_the_update_fails(repo, store):
    a = intent(repo, "w", 1, at(0, 10))
    with pytest.raises(ValueError):
        repo.transition(a, pr.PENDING, at(0, 10), bogus=1)
    assert repo.get(a.proposal_id).state == pr.INTENT


def test_body_is_persisted_in_intent_before_anything_is_sent(repo):
    a = intent(repo, "w", 7, at(0, 10))
    got = repo.get(a.proposal_id)
    assert got.state == pr.INTENT and got.body["dims"] == ["voice"] and got.plan_seq == 7
    assert repo.next_plan_seq("w") == 8 and repo.next_plan_seq("fresh") == 1


# ------------------------------------------------------------------ registry and jobs
def test_registry_collect_orders_by_priority_and_filters_workflow():
    reg = Registry()
    reg.register_producer(lambda ctx, wid: [DesiredChange(wid, "b", Priority.ROUTINE, {}, summary="r"),
                                            DesiredChange("other", "x", 0, {})])
    reg.register_producer(lambda ctx, wid: [DesiredChange(wid, "a", Priority.INCIDENT, {}, summary="i")])
    out = reg.collect(None, "w")
    assert [c.kind for c in out] == ["a", "b"]
    with pytest.raises(ValueError):
        DesiredChange("w", "k", 0, {}, route_preference="telepathy")


def test_job_last_run_is_persisted_in_store(store):
    from controller.planner import Planner
    p = Planner(None, store, None)
    assert p.job_due(settings.JOB_DAILY_BUNDLE, "w", at(3, 10))
    p.job_mark(settings.JOB_DAILY_BUNDLE, "w", at(3, 10))
    assert not p.job_due(settings.JOB_DAILY_BUNDLE, "w", at(3, 18))
    assert p.job_due(settings.JOB_DAILY_BUNDLE, "w", at(4, 0))  # next sim day: catches up
    assert p.job_due(settings.JOB_DAILY_BUNDLE, "other", at(3, 18))
    assert store.get_job(settings.JOB_DAILY_BUNDLE, "w") == at(3, 10)  # survives a "restart"
