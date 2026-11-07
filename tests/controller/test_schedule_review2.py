"""Review round 2: schedule apply detection, dry-run campaign age, partition relearn, config guards."""
import json
import math

import pytest

from controller import dryrun, schedule, settings
from controller.configlib import ProposalError, complete_hours
from controller.desired import PlanContext
from controller.detectors.common import latest_config
from controller.proposal_repo import INTENT, ROUTE_APPROVAL, ProposalRow, ProposalRepo
from controller.store import Store

HOURS = [9, 21]
WF = {"workflow_id": "w", "dialling_window": HOURS, "objective": {}, "cadence": [1, 2, 3],
      "governance": {"auto_apply": {"enabled": False, "allowed_dims": []}}}
COHORTS = [{"id": "a", "where": {"age_band": ["60+"]}}]


def table(v):
    return {str(h): v for h in range(9, 21)}


def cfg(tables=None):
    return {"cohorts": COHORTS, "allocations": {"default": {}}, "prompt_version": "P1",
            "schedule": {"hour_weights": tables or {}, "lead_preferred_hour": {}}}


def _applied_world(tmp_path, day_proposed=2, day_applied=3):
    store = Store(tmp_path / "s.db")
    proposed = {"a": table(1.234567), "default": table(1.0)}
    store.store_config_body("w", 1, "x", cfg())
    body = {"request": {"proposed_config": cfg(proposed), "dry_run": {"schedule_guardrail": {"rel_lift": 0.02}}},
            "dims": ["schedule"]}
    row = ProposalRow("w:1:schedule_refresh", "w", "schedule_refresh", INTENT, None, 1, 0, ROUTE_APPROVAL, 3, 1,
                      None, None, None, None, None, None, body)
    ProposalRepo(store).create_intent(row, day_proposed * 1440)
    store.store_config_body("w", 2, "x", cfg(proposed))
    store.upsert_config_event("w", 2, "supervisor", day_applied * 1440 + 600, ["schedule"], "w:1:schedule_refresh", None)
    return store, proposed


def test_apply_on_a_later_day_than_the_proposal_is_still_detected(tmp_path):
    store, proposed = _applied_world(tmp_path)
    stale_plan = {"day": 3, "tables": {"a": table(1.3), "default": table(1.0)}, "estimate": {"rel_lift": 0.03}}
    st = {"plan": stale_plan}
    ctx = PlanContext(store, None, {"w": WF}, 3 * 1440 + 700)
    schedule._detect_apply(ctx, "w", st, latest_config(store, "w"))
    assert st["applied"]["status"] == schedule.MONITORING and st["applied"]["tables"] == proposed
    assert st["applied"]["minute"] == 3 * 1440 + 600
    assert schedule._owned(st, latest_config(store, "w")[1])


def test_supervisor_edit_not_linked_to_our_proposal_is_not_claimed(tmp_path):
    store, _ = _applied_world(tmp_path)
    store.upsert_config_event("w", 2, "supervisor", 3 * 1440, ["schedule"], None, None)
    st = {"plan": {"day": 3, "tables": {"a": table(1.3)}, "estimate": {"rel_lift": 0.03}}}
    schedule._detect_apply(PlanContext(store, None, {"w": WF}, 3 * 1440 + 700), "w", st, latest_config(store, "w"))
    assert "applied" not in st


def test_bad_post_apply_drop_after_late_apply_flags_rollback(tmp_path, monkeypatch):
    store, _ = _applied_world(tmp_path)
    st = {"plan": {"day": 3, "tables": {}, "estimate": {"rel_lift": 0.03}}}
    ctx = PlanContext(store, None, {"w": WF}, 4 * 1440)
    c = latest_config(store, "w")
    schedule._detect_apply(ctx, "w", st, c)
    monkeypatch.setattr(schedule, "_period", lambda *a, **k: {(10, "a"): [10_000, 1, 1]})
    drop = {"drop": 0.5, "p_drop": 0.0, "before": 0.5, "after_std": 0.1}
    monkeypatch.setattr(schedule.sm, "verify_change",
                        lambda b, a: {"usable": True, "pickup": drop, "engagement": dict(drop, drop=0.0),
                                      "n_after": 10_000})
    schedule._verify(ctx, "w", st, c)
    assert st["applied"]["status"] == schedule.ROLLBACK
    [ch] = schedule.schedule_producer(_producer_ctx(store, st, ctx), "w")
    assert ch.kind == "schedule_rollback"


def _producer_ctx(store, st, ctx):
    from controller.detectors.common import save_state
    save_state(store, schedule.DETECTOR, "w", st)
    return ctx


def test_dryrun_ages_history_campaigns_with_floor_division(tmp_path):
    store = Store(tmp_path / "d.db")
    store.conn.execute("INSERT INTO campaigns VALUES ('c1','w',?,1,1)",
                       (-3 * 1440 + 480,))
    store.conn.execute("INSERT INTO leads VALUES (1,'L1','P1','w',0,'c1',?)",
                       (-3 * 1440 + 480,))
    assert dryrun.active_cell_counts(store, "w", 0, 3) == {}
    assert dryrun.active_cell_counts(store, "w", 0, 4) == {0: 1}


def test_complete_hours_rejects_non_integer_keys_and_non_finite_weights():
    for bad in ({"ten": 1.0}, {"10": float("nan")}, {"10": math.inf}):
        with pytest.raises(ProposalError):
            complete_hours(bad, WF, "t", False)


def test_partitions_history_is_bounded(tmp_path, monkeypatch):
    store = Store(tmp_path / "p.db")
    parts = iter(_P(str(i)) for i in range(20))
    monkeypatch.setattr(schedule, "_learn_partition", lambda ctx, wid: next(parts))
    st = {}
    for d in range(20):
        schedule._build_partition(PlanContext(store, None, {"w": WF}, d * 1440), "w", st)
    assert len(st["partitions"]) == schedule.PARTITION_HISTORY


def _relearn_world(tmp_path, monkeypatch, parts):
    store = Store(tmp_path / "r.db")
    store.store_config_body("w", 1, "x", dict(cfg(), cohorts=[]))
    it = iter(parts)
    monkeypatch.setattr(schedule, "_ready", lambda *a: True)
    monkeypatch.setattr(schedule, "_learn_partition", lambda ctx, wid: next(it))
    monkeypatch.setattr(schedule, "_plan", lambda *a: None)
    monkeypatch.setattr(schedule, "_detect_apply", lambda *a: None)
    monkeypatch.setattr(schedule, "_verify", lambda *a: None)
    return store


class _P:
    def __init__(self, tag):
        self.features, self.cohorts, self.n_min, self.score = ("f",), [{"id": tag}], 1, 5.0

    def as_dict(self):
        return {"cohorts": self.cohorts}


def test_partition_is_relearned_once_per_day_while_no_cohorts_are_live(tmp_path, monkeypatch):
    store = _relearn_world(tmp_path, monkeypatch, [_P("x"), _P("y"), _P("z")])
    for minute in (0, 600, 1440, 1440 + 600, 2 * 1440):
        schedule.schedule_detector(PlanContext(store, None, {"w": WF}, minute))
    from controller.detectors.common import load_state
    st = load_state(store, schedule.DETECTOR, "w")
    assert st["partition"]["cohorts"] == [{"id": "z"}]  # one fit per day: x, y, z
    assert st["partition_day"] == 2


def test_partition_is_not_relearned_once_cohorts_are_live(tmp_path, monkeypatch):
    store = _relearn_world(tmp_path, monkeypatch, [_P("x"), _P("y")])
    schedule.schedule_detector(PlanContext(store, None, {"w": WF}, 0))
    store.store_config_body("w", 2, "x", cfg())
    schedule.schedule_detector(PlanContext(store, None, {"w": WF}, 1440))
    from controller.detectors.common import load_state
    assert load_state(store, schedule.DETECTOR, "w")["partition"]["cohorts"] == [{"id": "x"}]
