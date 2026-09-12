"""Tests for runbound.wrap() against async (awaitable) fake clients.

Same duck-typed fakes as the sync suite, with ``async def create``. The event
loop is driven by ``asyncio.run()`` inside each test body, so the suite keeps
its zero-plugin, zero-dependency setup (no pytest-asyncio).
"""

import asyncio

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


class FakeAsyncCompletions:
    """Shaped like openai.resources.chat.AsyncCompletions."""

    def __init__(self, model="gpt-4o", tokens=(1000, 500), usage=True):
        self.model = model
        self.tokens = tokens
        self.usage = usage
        self.calls = 0
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        usage = None
        if self.usage:
            usage = FakeUsage(prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1])
        return FakeResponse(model=self.model, usage=usage)


class FakeAsyncOpenAI:
    def __init__(self, **kwargs):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeAsyncCompletions(**kwargs)

    @property
    def completions(self) -> FakeAsyncCompletions:
        return self.chat.completions


class FakeAsyncMessages:
    """Shaped like anthropic.resources.AsyncMessages."""

    def __init__(self, model="claude-sonnet-4-5", tokens=(1000, 500), usage=True):
        self.model = model
        self.tokens = tokens
        self.usage = usage
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        usage = None
        if self.usage:
            usage = FakeUsage(input_tokens=self.tokens[0], output_tokens=self.tokens[1])
        return FakeResponse(model=self.model, usage=usage)


class FakeAsyncAnthropic:
    def __init__(self, **kwargs):
        self.messages = FakeAsyncMessages(**kwargs)


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


# --- happy paths ------------------------------------------------------------


def test_async_openai_call_is_recorded_with_tokens_and_cost():
    runbound.init()
    client = runbound.wrap(FakeAsyncOpenAI())

    response = asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert response.content == "hello"  # response passes through untouched
    assert client.completions.calls == 1
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)
    assert session.total_cost_usd == pytest.approx(1000 / 1e6 * 2.50 + 500 / 1e6 * 10.00)


def test_async_anthropic_call_is_recorded_with_tokens_and_cost():
    runbound.init()
    client = runbound.wrap(FakeAsyncAnthropic())

    asyncio.run(client.messages.create(model="claude-sonnet-4-5", messages=[]))

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)
    assert session.total_cost_usd == pytest.approx(1000 / 1e6 * 3.00 + 500 / 1e6 * 15.00)


def test_installed_async_wrapper_is_itself_a_coroutine_function():
    runbound.init()
    client = runbound.wrap(FakeAsyncOpenAI())

    assert asyncio.iscoroutinefunction(client.chat.completions.create)


def test_async_wrapper_forwards_arguments_to_the_original_method():
    runbound.init()
    client = runbound.wrap(FakeAsyncOpenAI())

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[], temperature=0.2))

    assert client.completions.last_kwargs["temperature"] == 0.2


def test_concurrent_async_calls_are_all_recorded():
    runbound.init()
    client = runbound.wrap(FakeAsyncOpenAI())

    async def main():
        await asyncio.gather(
            *(client.chat.completions.create(model="gpt-4o", messages=[]) for _ in range(5))
        )

    asyncio.run(main())

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (5, 7500)


# --- tripping ---------------------------------------------------------------


def test_async_budget_trip_raises_from_the_await_after_the_call_returned():
    # 0.0075 USD per call; the second call crosses 0.01.
    runbound.init(budget_usd=0.01, on_anomaly="raise")
    client = runbound.wrap(FakeAsyncOpenAI())

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    with pytest.raises(GuardrailTripped) as excinfo:
        asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert excinfo.value.anomaly.detector == "budget"
    assert client.completions.calls == 2  # the model call itself did happen


def test_async_anthropic_budget_trip_raises():
    runbound.init(budget_usd=0.01, on_anomaly="raise")
    client = runbound.wrap(FakeAsyncAnthropic())

    with pytest.raises(GuardrailTripped):
        asyncio.run(client.messages.create(model="claude-sonnet-4-5", messages=[]))

    assert client.messages.calls == 1


# --- fail-open --------------------------------------------------------------


def test_async_missing_usage_yields_a_zero_token_event():
    runbound.init()
    client = runbound.wrap(FakeAsyncOpenAI(usage=False))
    spy = _recorder()

    response = asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert response.content == "hello"
    event = spy.events[0]
    assert (event.kind, event.tokens_in, event.tokens_out) == ("llm_call", 0, 0)
    assert runbound.current_session().step_count == 1


def test_async_underlying_client_error_propagates_unchanged():
    runbound.init()
    client = FakeAsyncOpenAI()

    async def boom(**kwargs):
        raise RuntimeError("upstream 500")

    client.chat.completions.create = boom
    runbound.wrap(client)

    with pytest.raises(RuntimeError, match="upstream 500"):
        asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))


def test_async_wrap_is_inert_before_init():
    client = runbound.wrap(FakeAsyncOpenAI())

    response = asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert response.content == "hello"
    assert runbound.current_session() is None


# --- dispatch ---------------------------------------------------------------


def test_is_wrapped_is_true_after_wrapping_an_async_client():
    from runbound.wrappers import anthropic_wrapper, openai_wrapper

    runbound.init()
    openai_client = runbound.wrap(FakeAsyncOpenAI())
    anthropic_client = runbound.wrap(FakeAsyncAnthropic())

    assert openai_wrapper.is_wrapped(openai_client)
    assert anthropic_wrapper.is_wrapped(anthropic_client)


def test_double_wrap_of_an_async_client_is_a_no_op():
    runbound.init()
    client = runbound.wrap(FakeAsyncOpenAI())
    once = client.chat.completions.create

    assert runbound.wrap(client) is client
    assert client.chat.completions.create is once

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert client.completions.calls == 1
    assert runbound.current_session().step_count == 1  # exactly one event


def test_double_wrap_of_an_async_anthropic_client_is_a_no_op():
    runbound.init()
    client = runbound.wrap(FakeAsyncAnthropic())
    runbound.wrap(client)

    asyncio.run(client.messages.create(model="claude-sonnet-4-5", messages=[]))

    assert client.messages.calls == 1
    assert runbound.current_session().step_count == 1


def test_a_functools_wrapped_async_create_is_still_detected_as_async():
    import functools

    runbound.init()
    client = FakeAsyncOpenAI()
    original = client.chat.completions.create

    @functools.wraps(original)
    def decorated(**kwargs):  # a sync shim returning the coroutine, as SDKs do
        return original(**kwargs)

    client.chat.completions.create = decorated
    runbound.wrap(client)

    response = asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert response.content == "hello"
    assert runbound.current_session().total_tokens == 1500


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
