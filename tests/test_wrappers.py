"""Tests for runbound.wrap() against duck-typed fake clients.

No openai/anthropic package is imported anywhere — the wrappers work on shape
alone, which is exactly what makes OpenAI-compatible endpoints work too.
"""

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, model=None, usage=None):
        self.model = model
        self.usage = usage
        self.content = "hello"


class FakeCompletions:
    """Shaped like openai.resources.chat.Completions."""

    def __init__(self, model="gpt-4o", tokens=(1000, 500), usage=True):
        self.model = model
        self.tokens = tokens
        self.usage = usage
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        usage = None
        if self.usage:
            usage = FakeUsage(prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1])
        return FakeResponse(model=self.model, usage=usage)


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(**kwargs)

    @property
    def completions(self) -> FakeCompletions:
        return self.chat.completions


class FakeMessages:
    """Shaped like anthropic.resources.Messages."""

    def __init__(self, model="claude-sonnet-4-5", tokens=(1000, 500), usage=True):
        self.model = model
        self.tokens = tokens
        self.usage = usage
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        usage = None
        if self.usage:
            usage = FakeUsage(input_tokens=self.tokens[0], output_tokens=self.tokens[1])
        return FakeResponse(model=self.model, usage=usage)


class FakeAnthropic:
    def __init__(self, **kwargs):
        self.messages = FakeMessages(**kwargs)


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


# --- happy paths ------------------------------------------------------------


def test_openai_call_is_recorded_with_tokens_and_cost():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    response = client.chat.completions.create(model="gpt-4o", messages=[])

    assert response.content == "hello"  # response passes through untouched
    assert client.completions.calls == 1
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)
    # gpt-4o: $2.50/1M in, $10.00/1M out
    assert session.total_cost_usd == pytest.approx(1000 / 1e6 * 2.50 + 500 / 1e6 * 10.00)


def test_anthropic_call_is_recorded_with_tokens_and_cost():
    runbound.init()
    client = runbound.wrap(FakeAnthropic())

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)
    # claude-sonnet-4-5: $3.00/1M in, $15.00/1M out
    assert session.total_cost_usd == pytest.approx(1000 / 1e6 * 3.00 + 500 / 1e6 * 15.00)


def test_wrap_returns_the_same_client_object():
    runbound.init()
    client = FakeOpenAI()

    assert runbound.wrap(client) is client


def test_wrap_forwards_arguments_to_the_original_method():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user"}], temperature=0.2)

    assert client.completions.last_kwargs["temperature"] == 0.2


def test_model_falls_back_to_the_request_kwargs():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(model=None))
    spy = _recorder()

    client.chat.completions.create(model="gpt-4o-mini", messages=[])

    assert spy.events[0].model == "gpt-4o-mini"


def test_wrap_is_inert_before_init():
    client = runbound.wrap(FakeOpenAI())

    assert client.chat.completions.create(model="gpt-4o", messages=[]).content == "hello"
    assert runbound.current_session() is None


# --- tripping ---------------------------------------------------------------


def test_budget_trip_raises_after_the_underlying_call_returned():
    # 0.0075 USD per call; the second call crosses 0.01.
    runbound.init(budget_usd=0.01, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())

    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "budget"
    assert client.completions.calls == 2  # the model call itself did happen


def test_unpriced_model_costs_nothing_but_still_counts_tokens():
    runbound.init(max_total_tokens=1000, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="llama-3.1-70b-local", tokens=(600, 0)))

    client.chat.completions.create(model="llama-3.1-70b-local", messages=[])
    assert runbound.current_session().total_cost_usd == 0.0

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="llama-3.1-70b-local", messages=[])

    assert excinfo.value.anomaly.details["limit_hit"] == "max_total_tokens"


def test_custom_prices_apply_to_an_openai_compatible_endpoint():
    runbound.init(custom_prices={"llama-3.1-70b-local": (1.0, 2.0)})
    client = runbound.wrap(FakeOpenAI(model="llama-3.1-70b-local", tokens=(1_000_000, 1_000_000)))

    client.chat.completions.create(model="llama-3.1-70b-local", messages=[])

    assert runbound.current_session().total_cost_usd == pytest.approx(3.0)


# --- fail-open --------------------------------------------------------------


def test_missing_usage_yields_a_zero_token_event_and_the_call_succeeds():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(usage=False))
    spy = _recorder()

    response = client.chat.completions.create(model="gpt-4o", messages=[])

    assert response.content == "hello"
    event = spy.events[0]
    assert (event.kind, event.tokens_in, event.tokens_out, event.cost_usd) == ("llm_call", 0, 0, 0.0)
    assert runbound.current_session().step_count == 1


def test_garbage_usage_yields_a_zero_token_event():
    runbound.init()
    client = FakeOpenAI()
    client.chat.completions.tokens = ("lots", None)
    runbound.wrap(client)
    spy = _recorder()

    client.chat.completions.create(model="gpt-4o", messages=[])

    event = spy.events[0]
    assert (event.tokens_in, event.tokens_out) == (0, 0)
    assert runbound.current_session().step_count == 1


def test_underlying_client_error_propagates_unchanged():
    runbound.init()
    client = FakeOpenAI()

    def boom(**kwargs):
        raise RuntimeError("upstream 500")

    client.chat.completions.create = boom
    runbound.wrap(client)

    with pytest.raises(RuntimeError, match="upstream 500"):
        client.chat.completions.create(model="gpt-4o", messages=[])


# --- dispatch ---------------------------------------------------------------


def test_unknown_client_raises_value_error():
    runbound.init()

    with pytest.raises(ValueError, match="runbound.wrap"):
        runbound.wrap(object())


def test_double_wrap_is_a_no_op():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())
    once = client.chat.completions.create

    assert runbound.wrap(client) is client
    assert client.chat.completions.create is once

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert client.completions.calls == 1
    assert runbound.current_session().step_count == 1  # exactly one event


def test_double_wrap_of_anthropic_is_a_no_op():
    runbound.init()
    client = runbound.wrap(FakeAnthropic())
    runbound.wrap(client)

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert client.messages.calls == 1
    assert runbound.current_session().step_count == 1


class _EventRecorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self) -> None:
        self.events: list = []

    def check(self, state, event, config):
        self.events.append(event)
        return None


def _recorder() -> _EventRecorder:
    """Attach an event recorder to the engine built by the last init()."""
    spy = _EventRecorder()
    api._ENGINE.detectors.insert(0, spy)
    return spy
