"""Tests for the three ways the guard used to report green while guarding nothing.

1. ``client.responses.create`` — today's primary OpenAI API — flowed past
   ``wrap()`` uncounted. It is guarded now, alongside chat completions, and
   ``wrap()`` says out loud which surfaces it patched.
2. A stream whose provider never sends usage records a zero-token step; that
   is honest bookkeeping but invisible guarding, so it warns once per process.
3. An unpriced model costs $0.00, and prefix matching used to turn "unknown"
   into "confidently wrong" (``o3-pro`` priced as ``o3``). Unknown models now
   stay unknown and say so when a dollar budget is in play.

Nothing from openai/anthropic is imported; the fakes are duck-typed like the
rest of the wrapper suite.
"""

import asyncio
import logging

import pytest

import runbound
from runbound import api, pricing
from runbound import wrappers
from runbound.pricing import PRICES, estimate_cost
from runbound.wrappers import openai_wrapper


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, model=None, usage=None):
        self.model = model
        self.usage = usage
        self.output_text = "hello"


class FakeCompletions:
    """Shaped like openai.resources.chat.Completions."""

    def __init__(self, model="gpt-4o", tokens=(1000, 500)):
        self.model = model
        self.tokens = tokens
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        usage = FakeUsage(prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1])
        return FakeResponse(model=self.model, usage=usage)


class FakeResponses:
    """Shaped like openai.resources.Responses: ``client.responses.create``."""

    def __init__(self, model="gpt-5", tokens=(200, 100), reasoning=None, stream_cls=None):
        self.model = model
        self.tokens = tokens
        self.reasoning = reasoning
        self.stream_cls = stream_cls
        self.calls = 0
        self.last_kwargs = None
        self.streams = []

    def _usage(self):
        details = None
        if self.reasoning is not None:
            details = FakeUsage(reasoning_tokens=self.reasoning)
        return FakeUsage(
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            output_tokens_details=details,
        )

    def _build(self, kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if kwargs.get("stream"):
            stream = (self.stream_cls or FakeStream)(self._events(kwargs))
            self.streams.append(stream)
            return stream
        return FakeResponse(model=self.model, usage=self._usage())

    def _events(self, kwargs):
        events = [FakeEvent("response.output_text.delta", delta=text) for text in ("Hel", "lo")]
        if kwargs.get("usage", True):
            events.append(
                FakeEvent(
                    "response.completed",
                    response=FakeResponse(model=self.model, usage=self._usage()),
                )
            )
        return events

    def create(self, **kwargs):
        return self._build(kwargs)


class FakeAsyncResponses(FakeResponses):
    async def create(self, **kwargs):
        return self._build(kwargs)


class FakeEvent:
    """A Responses-API stream event: a type and whatever fields it carries."""

    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class FakeStream:
    """Shaped like openai.Stream: iterator, context manager, closeable."""

    def __init__(self, events):
        self._events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._events)

    def close(self):
        self.closed = True


class FakeAsyncStream:
    def __init__(self, events):
        self._events = list(events)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event


class FakeOpenAI:
    """A client with both call surfaces, either of which can be left out."""

    def __init__(self, chat=True, responses=True, **kwargs):
        if chat:
            self.chat = type("Chat", (), {})()
            self.chat.completions = FakeCompletions()
        if responses:
            self.responses = FakeResponses(**kwargs)


class FakeStreamingCompletions(FakeCompletions):
    """Chat completions that stream, with or without a usage-bearing chunk."""

    def __init__(self, usage=True, **kwargs):
        super().__init__(**kwargs)
        self.usage = usage

    def create(self, **kwargs):
        self.calls += 1
        chunks = [FakeEvent("chunk", model=self.model, usage=None) for _ in range(2)]
        if self.usage:
            chunks.append(
                FakeEvent(
                    "chunk",
                    model=self.model,
                    usage=FakeUsage(
                        prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1]
                    ),
                )
            )
        return FakeStream(chunks)


class FakeAnthropic:
    def __init__(self):
        self.messages = FakeCompletions(model="claude-sonnet-4-5")


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def unwarned(monkeypatch):
    """A process that has not yet used up its one-time warnings."""
    monkeypatch.setattr(wrappers, "_ZERO_TOKEN_STREAM_WARNED", False)
    monkeypatch.setattr(pricing, "_WARNED_MODELS", set())


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


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- the Responses API is guarded -------------------------------------------


def test_a_client_with_both_surfaces_has_both_patched_and_counted():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    client.chat.completions.create(model="gpt-4o", messages=[])
    client.responses.create(model="gpt-5", input="hi")

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (2, 1500 + 300)


def test_a_responses_only_client_is_recognized_and_guarded():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False))

    response = client.responses.create(model="gpt-5", input="hi")

    assert response.output_text == "hello"  # response passes through untouched
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 300)
    # gpt-5: $1.25/1M in, $10.00/1M out
    assert session.total_cost_usd == pytest.approx(200 / 1e6 * 1.25 + 100 / 1e6 * 10.00)


def test_responses_usage_reads_reasoning_off_output_tokens_details():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False, reasoning=40))
    spy = _recorder()

    client.responses.create(model="gpt-5", input="hi")

    event = spy.events[0]
    assert (event.model, event.tokens_in, event.tokens_out) == ("gpt-5", 200, 100)
    assert event.tokens_reasoning == 40


def test_responses_model_falls_back_to_the_request_kwargs():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False, model=None))
    spy = _recorder()

    client.responses.create(model="gpt-5-mini", input="hi")

    assert spy.events[0].model == "gpt-5-mini"


def test_responses_arguments_reach_the_original_method():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False))

    client.responses.create(model="gpt-5", input="hi", temperature=0.2)

    assert client.responses.last_kwargs["temperature"] == 0.2


def test_a_responses_error_propagates_unchanged():
    runbound.init()
    client = FakeOpenAI(chat=False)

    def boom(**kwargs):
        raise RuntimeError("upstream 500")

    client.responses.create = boom
    runbound.wrap(client)

    with pytest.raises(RuntimeError, match="upstream 500"):
        client.responses.create(model="gpt-5", input="hi")


# --- Responses streaming ----------------------------------------------------


def test_responses_stream_reads_usage_off_the_completed_event():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False, reasoning=40))
    spy = _recorder()

    events = list(client.responses.create(model="gpt-5", input="hi", stream=True))

    assert [event.type for event in events][0] == "response.output_text.delta"
    event = spy.events[0]
    assert (event.model, event.tokens_in, event.tokens_out) == ("gpt-5", 200, 100)
    assert event.tokens_reasoning == 40


def test_responses_stream_without_usage_records_one_zero_token_step(unwarned):
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False))
    spy = _recorder()

    list(client.responses.create(model="gpt-5", input="hi", stream=True, usage=False))

    assert [(e.tokens_in, e.tokens_out) for e in spy.events] == [(0, 0)]


def test_an_async_responses_stream_reports_once_at_exhaustion():
    runbound.init()
    client = FakeOpenAI(chat=False)
    client.responses = FakeAsyncResponses(stream_cls=FakeAsyncStream)
    runbound.wrap(client)

    async def main():
        stream = await client.responses.create(model="gpt-5", input="hi", stream=True)
        return [event async for event in stream]

    events = asyncio.run(main())

    assert len(events) == 3
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 300)


def test_a_hostile_responses_event_does_not_break_iteration():
    runbound.init()

    class Hostile:
        type = "response.completed"

        @property
        def response(self):
            raise RuntimeError("this SDK object is angry")

    client = FakeOpenAI(chat=False)
    client.responses.create = lambda **kwargs: FakeStream([Hostile()])
    runbound.wrap(client)

    events = list(client.responses.create(model="gpt-5", input="hi", stream=True))

    assert len(events) == 1
    assert runbound.current_session().step_count == 1  # zero-token step, still counted


# --- is_wrapped over several surfaces ---------------------------------------


def test_wrapping_twice_double_counts_neither_surface():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())
    chat_once = client.chat.completions.create
    responses_once = client.responses.create

    assert runbound.wrap(client) is client
    assert client.chat.completions.create is chat_once
    assert client.responses.create is responses_once

    client.chat.completions.create(model="gpt-4o", messages=[])
    client.responses.create(model="gpt-5", input="hi")

    assert runbound.current_session().step_count == 2


def test_is_wrapped_is_false_until_every_present_surface_is_guarded():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(responses=False))
    chat_once = client.chat.completions.create
    assert openai_wrapper.is_wrapped(client)

    client.responses = FakeResponses()  # a surface that appeared after wrapping
    assert not openai_wrapper.is_wrapped(client)

    runbound.wrap(client)
    assert openai_wrapper.is_wrapped(client)
    assert client.chat.completions.create is chat_once  # not patched a second time

    client.chat.completions.create(model="gpt-4o", messages=[])
    client.responses.create(model="gpt-5", input="hi")

    assert runbound.current_session().step_count == 2


def test_an_unpatchable_surface_does_not_cost_the_other_one(caplog):
    """One read-only resource must not leave the whole client unguarded."""
    runbound.init()

    class ReadOnlyResponses:
        def create(self, **kwargs):
            return FakeResponse(model="gpt-5", usage=FakeUsage(input_tokens=1, output_tokens=1))

        def __setattr__(self, name, value):
            raise AttributeError("this resource is frozen")

    client = FakeOpenAI(responses=False)
    client.responses = ReadOnlyResponses()

    with caplog.at_level(logging.INFO, logger="runbound"):
        runbound.wrap(client)

    assert "runbound: guarding chat.completions.create (sync)" in [
        record.getMessage() for record in caplog.records
    ]
    client.chat.completions.create(model="gpt-4o", messages=[])
    client.responses.create(model="gpt-5", input="hi")  # unguarded, but it works

    assert runbound.current_session().step_count == 1


# --- wrap() says what it guarded --------------------------------------------


def test_wrap_logs_the_surfaces_it_guarded(caplog):
    runbound.init()
    with caplog.at_level(logging.INFO, logger="runbound"):
        runbound.wrap(FakeOpenAI())

    assert "runbound: guarding chat.completions.create, responses.create (sync)" in [
        record.getMessage() for record in caplog.records
    ]


def test_wrap_logs_an_async_surface_as_async(caplog):
    runbound.init()
    client = FakeOpenAI(chat=False)
    client.responses = FakeAsyncResponses()

    with caplog.at_level(logging.INFO, logger="runbound"):
        runbound.wrap(client)

    assert "runbound: guarding responses.create (async)" in [
        record.getMessage() for record in caplog.records
    ]


def test_wrap_logs_the_anthropic_surface(caplog):
    runbound.init()
    with caplog.at_level(logging.INFO, logger="runbound"):
        runbound.wrap(FakeAnthropic())

    assert "runbound: guarding messages.create (sync)" in [
        record.getMessage() for record in caplog.records
    ]


def test_a_second_wrap_claims_no_new_surfaces(caplog):
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    with caplog.at_level(logging.INFO, logger="runbound"):
        runbound.wrap(client)

    assert not [r for r in caplog.records if "guarding" in r.getMessage()]


# --- the zero-token stream warning ------------------------------------------


def test_a_usage_free_stream_warns_once_per_process(unwarned, caplog):
    runbound.init()
    client = runbound.wrap(FakeOpenAI(chat=False))

    with caplog.at_level(logging.WARNING, logger="runbound"):
        for _ in range(2):
            list(client.responses.create(model="gpt-5", input="hi", stream=True, usage=False))

    warnings = [w for w in _warnings(caplog) if "no usage data" in w]
    assert len(warnings) == 1
    assert "stream_options={'include_usage': True}" in warnings[0]


def test_a_stream_that_carries_usage_does_not_warn(unwarned, caplog):
    runbound.init()
    client = FakeOpenAI(chat=False)
    client.chat = type("Chat", (), {})()
    client.chat.completions = FakeStreamingCompletions()
    runbound.wrap(client)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert runbound.current_session().total_tokens == 1500
    assert not [w for w in _warnings(caplog) if "no usage data" in w]


def test_a_zero_token_chat_stream_warns_too(unwarned, caplog):
    runbound.init()
    client = FakeOpenAI(responses=False)
    client.chat.completions = FakeStreamingCompletions(usage=False)
    runbound.wrap(client)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert [w for w in _warnings(caplog) if "no usage data" in w]


def test_a_non_streamed_zero_token_call_does_not_warn(unwarned, caplog):
    """Only streams can be silently unpriced; a plain call with no usage is rare."""
    runbound.init()
    client = FakeOpenAI(responses=False)
    client.chat.completions.create = lambda **kwargs: FakeResponse(model="gpt-4o")
    runbound.wrap(client)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert not [w for w in _warnings(caplog) if "no usage data" in w]


# --- the unpriced-model warning ---------------------------------------------


def test_an_unpriced_model_warns_once_per_model(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        for _ in range(3):
            assert estimate_cost("mystery-model", 1_000, 1_000, warn_unpriced=True) == 0.0
        estimate_cost("other-mystery", 1_000, 1_000, warn_unpriced=True)

    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert "no price known for model 'mystery-model'" in warnings[0]
    assert "budget_usd cannot see it" in warnings[0]
    assert "custom_prices" in warnings[0]
    assert "other-mystery" in warnings[1]


def test_an_unpriced_model_is_silent_unless_asked(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        estimate_cost("mystery-model", 1_000, 1_000)

    assert _warnings(caplog) == []


def test_a_priced_model_never_warns(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        estimate_cost("gpt-4o", 1_000, 1_000, warn_unpriced=True)
        estimate_cost("my-model-x", 1_000, 1_000, {"my-model": (1.0, 2.0)}, warn_unpriced=True)

    assert _warnings(caplog) == []


def test_the_warning_flag_never_changes_the_cost(unwarned):
    quiet = estimate_cost("gpt-4o", 1_000, 2_000)
    loud = estimate_cost("gpt-4o", 1_000, 2_000, warn_unpriced=True)
    assert quiet == loud == pytest.approx(estimate_cost("gpt-4o", 1_000, 2_000))


def test_the_api_warns_about_unpriced_models_with_a_budget_set(unwarned, caplog):
    runbound.init(budget_usd=1.0)
    client = runbound.wrap(FakeOpenAI(chat=False, model="mystery-model"))

    with caplog.at_level(logging.WARNING, logger="runbound"):
        client.responses.create(model="mystery-model", input="hi")

    assert [w for w in _warnings(caplog) if "no price known" in w]


def test_the_api_warns_about_unpriced_models_even_without_a_budget(unwarned, caplog):
    """Wave 24 (T59a): on_unpriced_model="zero" warns once per model by

    default now, not only when a dollar limit makes the blind spot matter —
    a token budget alone used to see this silently, which is exactly the case
    this test used to assert stayed quiet.
    """
    runbound.init(max_total_tokens=100_000)
    client = runbound.wrap(FakeOpenAI(chat=False, model="mystery-model"))

    with caplog.at_level(logging.WARNING, logger="runbound"):
        client.responses.create(model="mystery-model", input="hi")

    assert [w for w in _warnings(caplog) if "no price known" in w]


# --- prefix matching stops guessing across model families -------------------


@pytest.mark.parametrize(
    "model", ["o3-pro", "gpt-5-pro", "gpt-4o-audio-preview", "gpt-4o-realtime-preview"]
)
def test_a_different_family_does_not_inherit_the_base_price(model, unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert estimate_cost(model, 1_000_000, 1_000_000, warn_unpriced=True) == 0.0

    assert [w for w in _warnings(caplog) if model in w]


@pytest.mark.parametrize(
    "dated,base",
    [
        ("gpt-4o-2024-11-20", "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ],
)
def test_a_dated_variant_still_prices_as_its_base_model(dated, base):
    # T139: base models here now carry a third (cached_in) PRICES element;
    # this test only needs the plain input/output pair.
    price_in, price_out = PRICES[base][0], PRICES[base][1]
    expected = (1_000 / 1e6) * price_in + (2_000 / 1e6) * price_out
    assert estimate_cost(dated, 1_000, 2_000) == pytest.approx(expected)


def test_a_prefix_match_needs_a_separator_after_the_prefix():
    # "o3" must not price "o3mini"; the boundary is what makes a prefix a model.
    assert estimate_cost("o3mini", 1_000_000, 0) == 0.0
    assert estimate_cost("gpt-4oh-no", 1_000_000, 0) == 0.0


def test_a_family_variant_of_a_longer_prefix_is_not_demoted_to_a_shorter_one():
    # "gpt-4o-mini-audio-preview" is an audio model: not gpt-4o-mini, and not
    # gpt-4o either.
    assert estimate_cost("gpt-4o-mini-audio-preview", 1_000_000, 0) == 0.0


def test_custom_prices_keep_plain_prefix_rules():
    """The custom table is the user's own; runbound second-guesses no entry."""
    custom = {"my-model": (10.0, 20.0)}
    assert estimate_cost("my-model-pro", 1_000_000, 0, custom) == pytest.approx(10.0)
    assert estimate_cost("my-model-2025-01-01", 1_000_000, 0, custom) == pytest.approx(10.0)


def test_the_price_table_is_dated():
    """A stale table is a wrong bill: the file says when it was last true."""
    source = open(pricing.__file__, encoding="utf-8").read()
    assert "2026-09" in source
