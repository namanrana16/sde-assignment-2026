"""suggest-fixes against the REAL sim app: bad default prompt, nothing known-good, so the LLM path must run."""
import json

import pytest

from controller import settings
from controller.desired import Registry
from controller.detectors import halluc
from controller.loop import Controller
from controller.store import Store
from tests.controller.sim_worlds import build, make_client, when


def _sim_log(world, kind):
    return [r for r in map(json.loads, open(world.run_dir / "sim_log.jsonl")) if r["kind"] == kind]


def test_suggest_fixes_end_to_end_against_real_sim(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_HALLUC", True)
    world = build(tmp_path, ("ks_cancel",), days=3, history_days=4, default_prompt="P2")
    client, http = make_client(world)
    store = Store(tmp_path / "state" / "e2e.db")
    reg = Registry()
    halluc.register(reg)
    ctl = Controller(client, store, registry=reg)
    ctl.startup()
    try:
        credits0 = client.credits_remaining()
        applied = lambda: client.get_config("ks_cancel")["config"]["prompt_version"] != "P2"
        closed = lambda: store.conn.execute("select count(*) from decisions where kind='incident_closed'").fetchone()[0] == 1
        while client.state()["sim_minute"] < when(2, 20):
            done, _ = ctl.tick()
            if (applied() and closed()) or done:
                break
        runs = store.conn.execute("select prompt_version, pass_rate from validation_runs").fetchall()
        if {r[0]: r[1] for r in runs}.get("P2", 1.0) >= 0.97:
            pytest.skip("validation noise let the bad prompt pass")
        # exactly one paid suggest-fixes, seen by the sim and by our ledger
        sug = _sim_log(world, "llm_suggest_fixes")
        assert len(sug) == 1
        assert store.conn.execute("select count(*), sum(credits) from llm_calls where request='suggest-fixes'").fetchone() == (1, 3.0)
        suggested = [s["prompt_version"] for s in sug[0]["suggestions"]]
        assert suggested and "P2" not in suggested
        # validations were bought for every suggestion (plus the Day-0 one for P2)
        vals = _sim_log(world, "validation_run")
        validated = {v["prompt_version"] for v in vals}
        assert set(suggested) <= validated and "P2" in validated
        spent = credits0 - client.credits_remaining()
        assert spent >= 3 + len(suggested)
        # incident body and the filed proposal carry the validation evidence
        body = json.loads(store.conn.execute("select body from incidents").fetchone()[0])
        assert body["suggested"] and body["candidates"] and all(c["source"] == "llm" for c in body["candidates"])
        (apid, ap), = list(world.approvals.items())
        cand = next(c for c in body["candidates"] if c["prompt"] == ap["proposed_config"]["prompt_version"])
        assert ap["request"]["evidence"]["validation_run_id"] == cand["validation_run_id"]
        assert ap["proposed_config"]["prompt_version"] in suggested
        # the fix is applied through the approval, and the incident closes after verification
        assert ap["status"] == "applied" and applied()
        assert closed(), "incident was applied but not verified/closed in the time window"
    finally:
        store.close()
        http.__exit__(None, None, None)
