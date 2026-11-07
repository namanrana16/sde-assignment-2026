"""`why` stub, credit ledger and OutcomeUnknown reconciliation."""
import httpx
import pytest

from controller import audit
from controller import client as c
from controller import proposal_repo as pr
from controller.__main__ import main as cli_main
from controller.credits import call_with_credit_ledger
from tests.controller.helpers import make_controller, proposal_states, run_until, uniform_hours


def test_parse_at_and_format():
    assert audit.parse_at("Day 3 14:00") == 3 * 1440 + 840
    assert audit.parse_at("D0 09:05") == 545 and audit.parse_at("Day -1 23:45") == -15
    assert audit.fmt_minute(3 * 1440 + 840) == "Day 3 14:00"
    for bad in ("3 14:00", "Day 3 25:00", "Day x 10:00"):
        with pytest.raises(ValueError):
            audit.parse_at(bad)


def test_why_reports_version_proposal_lifecycle_and_decisions(client, store, world, capsys):
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: any(s in (pr.APPLIED, pr.MONITORING) for s in proposal_states(store).values()))
    ctl.tick()
    ctl.tick()
    rep = audit.why_at(store, "nw_renewal", 1 * 1440)
    assert rep["version"]["version"] == 2 and rep["proposal"]["proposal_id"] == "nw_renewal:1:daily_bundle"
    assert rep["proposal"]["rationale"] and rep["proposal"]["approval_id"]
    assert any(d["kind"] == "proposal_applied" for d in rep["decisions"])
    assert [e["event"] for e in rep["proposal"]["events"]][:3] == ["DRAFT", "INTENT", "PENDING"]
    before = audit.why_at(store, "nw_renewal", 60)  # before the change was observed: the initial version
    assert before["version"]["version"] == 1 and before["proposal"] is None
    store.close()
    assert cli_main(["why", "--state-dir", str(store.path.parent), "--workflow", "nw_renewal",
                     "--at", "Day 1 12:00"]) == 0
    out = capsys.readouterr().out
    assert "nw_renewal:1:daily_bundle" in out and "APPLIED" in out and "lifecycle" in out
    assert cli_main(["why", "--state-dir", str(store.path.parent), "--workflow", "x", "--at", "nope"]) == 2


def test_credit_ledger_records_a_successful_call(client, store, world):
    res = call_with_credit_ledger(client, store, "nw_renewal", 0, "validate P3", client.run_validation,
                                  "nw_renewal", "P3", 300)
    assert res["credits_remaining"] == 197
    credits, resp = store.conn.execute("select credits, response from llm_calls").fetchone()
    assert credits == 3 and "val_" in resp


def test_credit_ledger_reconciles_an_outcome_unknown_call(world, store):
    import sim.server
    from fastapi.testclient import TestClient
    with TestClient(sim.server.app) as tc:
        def handler(req):
            resp = tc.request(req.method, req.url.path, content=req.content,
                              headers={"content-type": "application/json"})
            if req.url.path == "/validation/runs":
                raise httpx.ReadTimeout("response lost after the sim charged us")
            return httpx.Response(resp.status_code, content=resp.content,
                                  headers={"content-type": "application/json"})

        cl = c.SimClient(http=httpx.Client(base_url="http://sim", transport=httpx.MockTransport(handler)),
                         sleep=lambda s: None)
        with pytest.raises(c.OutcomeUnknown):
            call_with_credit_ledger(cl, store, "nw_renewal", 5, "validate P3", cl.run_validation,
                                    "nw_renewal", "P3", 300)
        assert world.credits == 197  # it was charged exactly once: no retry
    credits, resp = store.conn.execute("select credits, response from llm_calls").fetchone()
    assert credits == 3 and resp == "outcome_unknown"
    assert store.conn.execute("select count(*) from decisions where kind='credit_outcome_unknown'").fetchone()[0] == 1


def test_outcome_unknown_submit_is_reconciled_by_proposal_id_lookup(client, store, world, monkeypatch):
    real = client.request_approval

    def lost_response(body):
        real(body)  # the sim processed it ...
        raise c.OutcomeUnknown("POST", "/approvals", "read timeout")  # ... but we never saw the reply

    monkeypatch.setattr(client, "request_approval", lost_response)
    ctl = make_controller(client, store, uniform_hours())
    assert run_until(ctl, lambda: len(world.approvals) == 1)
    assert proposal_states(store)["nw_renewal:1:daily_bundle"] == pr.INTENT
    monkeypatch.setattr(client, "request_approval", real)
    ctl.tick()  # next reconcile adopts it; nothing is re-sent
    assert proposal_states(store)["nw_renewal:1:daily_bundle"] in (pr.PENDING, pr.APPROVED, pr.APPLIED, pr.REJECTED)
    assert len(world.approvals) == 1
