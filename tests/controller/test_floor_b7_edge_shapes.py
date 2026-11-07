"""B7: the whole loop (all detectors, producers, vetoes) survives odd workflow shapes without swallowing errors."""
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sim.server
from controller.client import SimClient
from controller.detectors import did_data
from controller.loop import Controller
from controller.store import Store
from sim.scenario import load_scenario
from sim.world import World

ROOT = Path(__file__).resolve().parents[2]
DAY = 1440
FAIL_CODES = ("detector_failed", "producer_failed", "veto_failed", "person_analysis_failed", "advance_failed")


def _single_options(w):
    w["options"] = {d: v[:1] for d, v in w["options"].items()}
    w["default_config"] = {"allocations": {"default": {d: {w["options"][d][0]: 1} for d in
                                                       ("welcome_message", "voice", "caller_id")}},
                           "prompt_version": w["options"]["prompt_version"][0]}


def _single_did(w):
    w["options"]["caller_id"] = w["options"]["caller_id"][:1]
    w["default_config"]["allocations"]["default"]["caller_id"] = {w["options"]["caller_id"][0]: 1}


def _single_prompt(w):
    w["options"]["prompt_version"] = w["options"]["prompt_version"][:1]
    w["default_config"]["prompt_version"] = w["options"]["prompt_version"][0]


def _no_auto(w):
    w["governance"]["auto_apply"] = {"enabled": False, "allowed_dims": [], "max_shift": 0.0}


def _hours(lo_hi):
    def f(w):
        w["governance"]["approver_hours"] = lo_hi
    return f


SHAPES = {
    "single_option_per_dim": (_single_options, {}),
    "single_did": (_single_did, {}),
    "single_prompt": (_single_prompt, {}),
    "history_0": (lambda w: None, {"history_days": 0}),
    "auto_apply_disabled": (_no_auto, {}),
    "tick_30": (lambda w: None, {"tick_minutes": 30}),
    "tick_60": (lambda w: None, {"tick_minutes": 60}),
    "approver_always": (_hours([0, 24]), {}),
    "strict_min_pass": (lambda w: w["governance"].update(min_validation_pass_rate=0.99), {}),
    "zero_credits": (lambda w: None, {"credits": 0}),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_loop_survives_shape_without_isolated_failures(tmp_path, shape):
    mutate, top = SHAPES[shape]
    sc = load_scenario(str(ROOT / "scenarios" / "public_02_incidents.yaml"))
    sc.update(days=2, history_days=2, incidents=[], credits=300)
    sc.update(top)
    sc["tenants"] = [t for t in sc["tenants"] if t["workflows"][0]["id"] == "nw_renewal"]
    for t in sc["tenants"]:
        for w in t["workflows"]:
            w["dials_per_hour"] = 60
            w["daily_batch"]["size"] = 300
            mutate(w)
    world = World(copy.deepcopy(sc), tmp_path / "run")
    sim.server._world = world
    did_data.forget()
    with TestClient(sim.server.app) as http:
        store = Store(tmp_path / "state" / "t.db")
        try:
            ctl = Controller(SimClient(http=http, sleep=lambda s: None), store)
            ctl.startup()
            for _ in range(2000):
                done, _ = ctl.tick()
                if done:
                    break
            assert done, "the simulated run should finish"
            bad = store.conn.execute(
                f"select code, message from alerts where code in ({','.join('?' * len(FAIL_CODES))})", FAIL_CODES).fetchall()
            assert bad == [], bad
        finally:
            store.close()
