"""The dollar door is a hard stop, and its notice must not swallow the wall's alert.

Since 0.4.0 a call whose worst case would cross ``budget_usd`` is refused before it
goes out, whatever ``on_anomaly`` says (you set a money budget). The token door, added
in front of a limit that could already just warn, follows ``on_anomaly`` instead
(``tests/test_token_budget.py``). What the two share is the alert's dedupe key: it reads
``details["level"]``, which the wall's own budget anomaly carries, so a reservation
anomaly carrying it too would have taken the wall's alert for the same session.
"""

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped

from test_reservation_ledger import MODEL, PRICES, SHORT, Completions, assert_reserved_zero, openai_client


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Recorder:
    def __init__(self):
        self.seen = []

    def on_event(self, session, event):
        pass

    def on_anomaly(self, session, anomaly, reacted):
        self.seen.append((anomaly.details.get("rule"), reacted, anomaly))


def run(on_anomaly: str, **config):
    runbound.init(budget_usd=1.0, on_anomaly=on_anomaly, custom_prices=PRICES, **config)
    recorder = Recorder()
    api._ENGINE.observers.append(recorder)
    return recorder


@pytest.mark.parametrize("mode", ["raise", "warn"])
def test_a_dollar_reservation_refuses_at_the_door_whatever_on_anomaly_says(mode):
    recorder = run(mode)
    provider = Completions()
    client = openai_client(provider)

    with runbound.session("k"), pytest.raises(GuardrailTripped) as tripped:
        client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=2)  # worst case $2 > $1

    assert tripped.value.decision.boundary == "money"
    assert provider.calls == 0
    assert_reserved_zero()


@pytest.mark.parametrize("mode", ["raise", "warn"])
def test_a_dollar_refusal_is_said_once(mode):
    recorder = run(mode)
    client = openai_client(Completions())

    with runbound.session("k"), pytest.raises(GuardrailTripped):
        client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=2)

    assert [(rule, reacted) for rule, reacted, _ in recorder.seen] == [("reservation", "door")]


def test_the_reservation_anomaly_carries_its_level_apart_from_the_dedupe_key():
    recorder = run("warn")
    client = openai_client(Completions())

    with runbound.session("k"), pytest.raises(GuardrailTripped):
        client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=2)

    anomaly = recorder.seen[0][2]
    assert "level" not in anomaly.details
    assert anomaly.details["reservation_level"] == "key"


def test_the_walls_own_alert_still_fires_after_a_reservation_refusal_under_warn():
    recorder = run("warn")
    refused = openai_client(Completions())
    with runbound.session("k"), pytest.raises(GuardrailTripped):
        refused.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=2)  # the door's notice

    crossing = openai_client(Completions(tokens_out=3))  # no stated cap: not reserved; costs $3 after
    with runbound.session("k"):
        crossing.chat.completions.create(model=MODEL, messages=SHORT)

    rules = [rule for rule, _reacted, _a in recorder.seen]
    assert rules == ["reservation", None]  # the wall's anomaly (no rule) was not deduped away
    assert recorder.seen[1][2].details["limit_hit"] == "budget_usd"


def test_the_uncapped_admission_refusal_carries_its_level_apart_too():
    recorder = run("warn", budget_admission=True, admission_output_tokens=2)
    client = openai_client(Completions())

    with runbound.session("k"), pytest.raises(GuardrailTripped):
        client.chat.completions.create(model=MODEL, messages=SHORT)  # assumed cap of 2 tokens: $2 > $1

    anomaly = recorder.seen[0][2]
    assert anomaly.details["rule"] == "admission" and "level" not in anomaly.details
    assert anomaly.details["reservation_level"] == "key"
