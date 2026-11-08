"""Round-4: an OutcomeUnknown suggest-fixes is retried once only if the sim did not charge for it."""
from controller.client import OutcomeUnknown
from controller.detectors import halluc as hl
from controller.detectors import halluc_data as hd
from tests.controller.test_halluc_units import FakeClient, ctx_for, new_body


class FlakySuggest(FakeClient):
    """First suggest_fixes raises OutcomeUnknown (charged or not), later ones succeed."""

    def __init__(self, charged: bool, failures: int = 1):
        super().__init__()
        self.charged, self.failures = charged, failures

    def suggest_fixes(self, wid, problem):
        if self.failures > 0:
            self.failures -= 1
            self.suggest_calls += 1
            if self.charged:
                self.credits -= 3
            raise OutcomeUnknown("POST", "/llm/suggest-fixes", "boom")
        return super().suggest_fixes(wid, problem)


def _prepare(store, client, body):
    hl.prepare_fix(ctx_for(store, client), "w", body, hd.fresh_state(), "P2")


def test_unpaid_outcome_unknown_allows_exactly_one_retry_on_a_later_tick(store):
    client, body = FlakySuggest(charged=False), new_body()
    _prepare(store, client, body)
    assert client.suggest_calls == 1 and body["target"] is None
    assert body["suggested"] is False and body["suggest_retry_used"] is True
    assert body["credits_spent"] == 0  # nothing was charged, nothing counts against the incident
    _prepare(store, client, body)  # a later tick
    assert client.suggest_calls == 2 and body["target"] == "P3" and body["suggested"] is True
    assert body["credits_spent"] == 3 + 3 * 3


def test_second_unpaid_failure_is_not_retried_again(store):
    client, body = FlakySuggest(charged=False, failures=2), new_body()
    _prepare(store, client, body)
    _prepare(store, client, body)
    _prepare(store, client, body)
    assert client.suggest_calls == 2 and body["suggested"] is True and body["target"] is None


def test_paid_but_lost_suggestions_are_never_paid_twice_and_are_recorded(store):
    client, body = FlakySuggest(charged=True), new_body()
    _prepare(store, client, body)
    _prepare(store, client, body)
    assert client.suggest_calls == 1 and body["suggested"] is True and body["target"] is None
    assert body["credits_spent"] == 3 and not body.get("suggest_retry_used")
    kinds = [r[0] for r in store.conn.execute("select kind from decisions").fetchall()]
    assert "suggestions_paid_but_lost" in kinds


class BalanceDown(FakeClient):
    """The sim's state is unreachable when the balance is read before the paid call."""

    def __init__(self):
        super().__init__()
        self.down = True

    def credits_remaining(self):
        if self.down:
            from controller.client import SimUnavailable
            raise SimUnavailable("GET /sim/state")
        return super().credits_remaining()


def test_failed_balance_read_before_the_paid_call_marks_nothing_and_retries_next_tick(store, monkeypatch):
    monkeypatch.setattr(hl, "_spend_ok", lambda *a, **k: True)  # isolate the read inside _suggest
    client, body = BalanceDown(), new_body()
    _prepare(store, client, body)
    assert client.suggest_calls == 0 and body["suggested"] is False and body["credits_spent"] == 0
    client.down = False
    _prepare(store, client, body)
    assert client.suggest_calls == 1 and body["suggested"] is True
