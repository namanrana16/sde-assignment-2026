import httpx
import pytest

from controller import client as c


def make(handler):
    http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return c.SimClient(http=http, sleep=lambda s: None)


def err(status, code):
    return httpx.Response(status, json={"error": code, "detail": "d"})


@pytest.mark.parametrize("status,code,exc", [
    (409, "duplicate_proposal_id", c.DuplicateProposal),
    (409, "stale_base_version", c.StaleBase),
    (409, "not_approved", c.NotApproved),
    (409, "finished", c.Finished),
    (409, "something_else", c.Conflict),
    (410, "approval_lapsed", c.Lapsed),
    (402, "out_of_credits", c.OutOfCredits),
    (422, "no_change", c.Unprocessable),
    (403, "auto_apply_not_permitted", c.Forbidden),
    (404, "not_found", c.NotFound),
    (400, "bad_minutes", c.BadRequest),
])
def test_error_mapping_carries_body(status, code, exc):
    calls = []

    def handler(req):
        calls.append(req)
        return err(status, code)

    with pytest.raises(exc) as e:
        make(handler).get_approval("a")
    assert e.value.body["error"] == code and e.value.status == status
    assert len(calls) == 1  # 4xx never retried


def test_5xx_retried_with_backoff_then_raises():
    calls, sleeps = [], []
    http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(
        lambda r: calls.append(r) or httpx.Response(503, text="no")))
    cl = c.SimClient(http=http, sleep=sleeps.append)
    with pytest.raises(c.SimUnavailable):
        cl.state()
    assert len(calls) == 5
    assert sleeps == sorted(sleeps) and len(sleeps) == 4


def test_transient_failures_recover():
    seq = iter([httpx.Response(500), httpx.ConnectError("boom"), httpx.Response(200, json={"ok": 1})])

    def handler(req):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    assert make(handler).state() == {"ok": 1}


def test_non_json_error_body():
    with pytest.raises(c.SimError) as e:
        make(lambda r: httpx.Response(418, text="teapot")).state()
    assert e.value.code == "non_json"


def test_bad_chunk_kind_and_ctor():
    with pytest.raises(ValueError):
        make(lambda r: httpx.Response(200, json=[])).list_chunks("bogus")
    with pytest.raises(ValueError):
        c.SimClient()


def advance_handler(minute_after, fail="transport"):
    state = {"minute": 0, "posts": 0}

    def handler(req):
        if req.url.path == "/sim/state":
            return httpx.Response(200, json={"sim_minute": state["minute"], "ts": "t",
                                             "finished": False})
        state["posts"] += 1
        if minute_after:
            state["minute"] = minute_after  # server applied it, response lost
        if fail == "transport":
            raise httpx.ReadTimeout("lost")
        return httpx.Response(502)

    return handler, state


@pytest.mark.parametrize("fail", ["transport", "5xx"])
def test_advance_recovers_when_minute_moved_and_never_retries(fail):
    handler, state = advance_handler(15, fail)
    res = make(handler).advance(15)
    assert res["recovered"] and res["sim_minute"] == 15 and state["posts"] == 1


def test_advance_not_applied_raises_without_retry():
    handler, state = advance_handler(0)
    with pytest.raises(c.AdvanceNotApplied):
        make(handler).advance(15)
    assert state["posts"] == 1


def test_advance_finished_maps():
    def handler(req):
        if req.url.path == "/sim/state":
            return httpx.Response(200, json={"sim_minute": 5})
        return err(409, "finished")

    with pytest.raises(c.Finished):
        make(handler).advance(15)


def test_advance_success_passthrough():
    def handler(req):
        if req.url.path == "/sim/state":
            return httpx.Response(200, json={"sim_minute": 5})
        return httpx.Response(200, json={"sim_minute": 20, "new_chunks": []})

    assert make(handler).advance(15)["sim_minute"] == 20


def test_wrappers_hit_expected_paths():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    cl = make(handler)
    cl.health()
    cl.workflows()
    cl.workflow("w")
    cl.get_config("w")
    cl.config_history("w")
    cl.list_approvals("w", "pending")
    cl.request_approval({})
    cl.apply_approval("w", "a")
    cl.put_config("w", {})
    cl.suggest_fixes("w", "p")
    cl.run_validation("w", "P1", 50)
    assert ("PUT", "/workflows/w/config") in seen and ("POST", "/validation/runs") in seen
    assert len(seen) == 11


def test_first_line_and_chunk_lines():
    def handler(req):
        if req.url.path == "/events/chunks/9":
            return httpx.Response(200, text='{"a":1}\n{"a":2}\n')
        return err(404, "nf")

    cl = make(handler)
    assert cl.first_line(9) == '{"a":1}'
    assert list(cl.iter_chunk_lines(9)) == ['{"a":1}', '{"a":2}']
    with pytest.raises(c.NotFound):
        cl.first_line(1)
