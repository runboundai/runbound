"""Tests for guarded streaming responses (sync and async, both providers).

The fakes mimic the shapes the real SDKs return from ``create(stream=True)``:
an iterator of chunk objects that is also a context manager and has a
``close()``. Nothing from openai/anthropic is imported — the wrappers work on
shape alone.

The contract under test: chunks pass through untouched, and exactly one
``llm_call`` event is recorded when the stream ends (exhaustion, ``close()``
or leaving a ``with`` block). A stream that is simply abandoned instead now
counts too, as one partial call reported once the proxy is garbage collected
— see ``tests/test_streams_abandoned.py`` for that mechanism in detail; the
handful of cases here just confirm it reaches all the way through
``runbound.wrap()`` and into the session.
"""

import asyncio
import gc

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class FakeChunk:
    def __init__(self, text="", model="gpt-4o", usage=None):
        self.text = text
        self.model = model
        self.usage = usage


class FakeStream:
    """Shaped like openai.Stream: iterator, context manager, closeable."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False
        self.entered = False
        self.response = "<raw http response>"  # a provider helper attribute

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class FakeAsyncStream:
    """Shaped like openai.AsyncStream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._index = 0
        self.closed = False
        self.entered = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc_info):
        await self.close()
        return False


def _openai_chunks(model="gpt-4o", usage=True, tokens=(1000, 500)):
    """Three content chunks, plus the final usage-bearing one OpenAI sends
    only when the caller asked for ``stream_options={"include_usage": True}``.
    """
    chunks = [FakeChunk(text=text, model=model) for text in ("Hel", "lo ", "there")]
    if usage:
        chunks.append(
            FakeChunk(
                model=model,
                usage=FakeUsage(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
            )
        )
    return chunks


class FakeStreamingCompletions:
    """OpenAI-shaped ``create`` that streams when asked to."""

    def __init__(self, model="gpt-4o", tokens=(1000, 500), stream_cls=FakeStream):
        self.model = model
        self.tokens = tokens
        self.stream_cls = stream_cls
        self.calls = 0
        self.streams = []

    def _build(self, kwargs):
        self.calls += 1
        include_usage = bool((kwargs.get("stream_options") or {}).get("include_usage"))
        stream = self.stream_cls(
            _openai_chunks(model=self.model, usage=include_usage, tokens=self.tokens)
        )
        self.streams.append(stream)
        return stream

    def create(self, **kwargs):
        return self._build(kwargs)


class FakeAsyncStreamingCompletions(FakeStreamingCompletions):
    def __init__(self, **kwargs):
        super().__init__(stream_cls=FakeAsyncStream, **kwargs)

    async def create(self, **kwargs):
        return self._build(kwargs)


class FakeOpenAI:
    def __init__(self, completions=None, **kwargs):
        self.chat = type("Chat", (), {})()
        self.chat.completions = completions or FakeStreamingCompletions(**kwargs)

    @property
    def completions(self):
        return self.chat.completions


# --- Anthropic-shaped fakes -------------------------------------------------


class AnthropicEvent:
    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


def _anthropic_chunks(model="claude-sonnet-4-5", tokens=(1000, 500)):
    message = AnthropicEvent(
        "message", model=model, usage=FakeUsage(input_tokens=tokens[0], output_tokens=1)
    )
    return [
        AnthropicEvent("message_start", message=message),
        AnthropicEvent("content_block_delta", delta="Hello"),
        AnthropicEvent("message_delta", usage=FakeUsage(output_tokens=tokens[1] // 2)),
        AnthropicEvent("message_delta", usage=FakeUsage(output_tokens=tokens[1])),
        AnthropicEvent("message_stop"),
    ]


class FakeStreamingMessages:
    """Anthropic-shaped ``messages.create`` that streams when asked to."""

    def __init__(self, model="claude-sonnet-4-5", tokens=(1000, 500)):
        self.model = model
        self.tokens = tokens
        self.calls = 0
        self.streams = []

    def create(self, **kwargs):
        self.calls += 1
        stream = FakeStream(_anthropic_chunks(model=self.model, tokens=self.tokens))
        self.streams.append(stream)
        return stream


class FakeAnthropic:
    def __init__(self, **kwargs):
        self.messages = FakeStreamingMessages(**kwargs)


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def _stream(client, **kwargs):
    return client.chat.completions.create(
        model="gpt-4o", messages=[], stream=True, stream_options={"include_usage": True}, **kwargs
    )


# --- sync OpenAI streaming --------------------------------------------------


def test_chunks_arrive_untouched_and_in_order():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    chunks = list(_stream(client))

    assert [chunk.text for chunk in chunks] == ["Hel", "lo ", "there", ""]
    assert chunks[-1].usage.prompt_tokens == 1000  # the provider's own object


def test_exhausted_stream_reports_usage_exactly_once():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    for _ in _stream(client):
        pass

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)
    assert session.total_cost_usd == pytest.approx(1000 / 1e6 * 2.50 + 500 / 1e6 * 10.00)


def test_stream_without_include_usage_still_records_one_zero_token_step():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())
    spy = _recorder()

    chunks = list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert len(chunks) == 3
    assert [(e.kind, e.tokens_in, e.tokens_out) for e in spy.events] == [("llm_call", 0, 0)]
    assert runbound.current_session().step_count == 1


def test_stream_model_falls_back_to_the_request_kwargs():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(model=None))
    spy = _recorder()

    for _ in _stream(client):
        pass

    assert spy.events[0].model == "gpt-4o"


def test_close_before_exhaustion_reports_once_and_closes_the_stream():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    stream = _stream(client)
    next(iter(stream))
    stream.close()

    assert client.completions.streams[0].closed is True
    assert runbound.current_session().step_count == 1


def test_close_after_exhaustion_does_not_report_twice():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    stream = _stream(client)
    for _ in stream:
        pass
    stream.close()
    stream.close()

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)


def test_an_abandoned_stream_is_recorded_as_one_partial_call():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    stream = _stream(client)
    next(iter(stream))  # one chunk, "Hel" — never exhausted, closed or exited
    del stream
    gc.collect()

    session = runbound.current_session()
    assert session.step_count == 1
    # FakeChunk carries its text on `.text`, not the `choices[].delta.content`
    # shape the chat surface's chunk-text reader knows -- same as every other
    # test in this file -- so no chars are ever seen and the chars/4 fallback
    # reports zero rather than guessing wrong.
    assert session.total_tokens == 0


def test_context_manager_delegates_and_reports_on_exit():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    with _stream(client) as stream:
        texts = [chunk.text for chunk in stream]

    underlying = client.completions.streams[0]
    assert texts == ["Hel", "lo ", "there", ""]
    assert (underlying.entered, underlying.closed) == (True, True)
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)


def test_unknown_attributes_pass_through_to_the_provider_stream():
    runbound.init()
    client = runbound.wrap(FakeOpenAI())

    stream = _stream(client)

    assert stream.response == "<raw http response>"


def test_non_streaming_calls_on_a_streaming_client_are_unaffected():
    runbound.init()

    class Completions(FakeStreamingCompletions):
        def create(self, **kwargs):
            if kwargs.get("stream"):
                return self._build(kwargs)
            self.calls += 1
            usage = FakeUsage(prompt_tokens=10, completion_tokens=20)
            return type("R", (), {"model": "gpt-4o", "usage": usage})()

    client = runbound.wrap(FakeOpenAI(completions=Completions()))
    client.chat.completions.create(model="gpt-4o", messages=[])

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 30)


# --- tripping ---------------------------------------------------------------


def test_budget_trip_at_stream_end_raises_out_of_the_consumer_loop():
    # 0.0075 USD for the stream; the limit is crossed as it ends.
    runbound.init(budget_usd=0.005, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    seen = []

    with pytest.raises(GuardrailTripped) as excinfo:
        for chunk in _stream(client):
            seen.append(chunk.text)

    assert excinfo.value.anomaly.detector == "budget"
    assert seen == ["Hel", "lo ", "there", ""]  # every chunk was delivered first


# --- fail-open --------------------------------------------------------------


def test_a_chunk_that_raises_on_usage_does_not_break_iteration():
    runbound.init()

    class HostileChunk:
        text = "boom"

        @property
        def usage(self):
            raise RuntimeError("no usage for you")

        @property
        def model(self):
            raise RuntimeError("no model either")

    class Completions(FakeStreamingCompletions):
        def create(self, **kwargs):
            self.calls += 1
            return FakeStream([HostileChunk(), FakeChunk(text="ok")])

    client = runbound.wrap(FakeOpenAI(completions=Completions()))

    chunks = list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert [chunk.text for chunk in chunks] == ["boom", "ok"]
    assert runbound.current_session().step_count == 1  # zero-token step, still counted


def test_an_error_mid_stream_propagates_unchanged():
    runbound.init()

    class Exploding(FakeStreamingCompletions):
        def create(self, **kwargs):
            self.calls += 1

            def chunks():
                yield FakeChunk(text="Hel")
                raise RuntimeError("connection reset")

            return chunks()

    client = runbound.wrap(FakeOpenAI(completions=Exploding()))

    with pytest.raises(RuntimeError, match="connection reset"):
        list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))


def test_a_broken_report_or_usage_hook_never_reaches_the_consumer():
    """The guard itself, driven directly: everything it does is fail-open."""
    from runbound.wrappers import _GuardedStream

    def exploding_hook(chunk, usage):
        raise RuntimeError("hook is broken")

    def exploding_report(model, tokens_in, tokens_out):
        raise RuntimeError("report is broken")

    stream = _GuardedStream(
        FakeStream(_openai_chunks()), exploding_hook, exploding_report, "gpt-4o"
    )

    assert [chunk.text for chunk in stream] == ["Hel", "lo ", "there", ""]
    stream.close()  # a second end, after a failed report, is still quiet


def test_streaming_is_inert_before_init():
    client = runbound.wrap(FakeOpenAI())

    chunks = list(_stream(client))

    assert len(chunks) == 4
    assert runbound.current_session() is None


# --- Anthropic streaming ----------------------------------------------------


def test_anthropic_stream_accumulates_message_start_and_message_delta_usage():
    runbound.init()
    client = runbound.wrap(FakeAnthropic())
    spy = _recorder()

    events = list(client.messages.create(model="claude-sonnet-4-5", messages=[], stream=True))

    assert [event.type for event in events][0] == "message_start"
    event = spy.events[0]
    assert (event.model, event.tokens_in, event.tokens_out) == ("claude-sonnet-4-5", 1000, 500)
    session = runbound.current_session()
    assert session.total_cost_usd == pytest.approx(1000 / 1e6 * 3.00 + 500 / 1e6 * 15.00)


def test_anthropic_stream_context_manager_reports_once():
    runbound.init()
    client = runbound.wrap(FakeAnthropic())

    with client.messages.create(model="claude-sonnet-4-5", messages=[], stream=True) as stream:
        for _ in stream:
            pass

    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)


def test_abandoned_anthropic_stream_is_recorded_as_one_partial_call():
    runbound.init()
    client = runbound.wrap(FakeAnthropic())

    client.messages.create(model="claude-sonnet-4-5", messages=[], stream=True)
    gc.collect()  # the guard is never even assigned; it drops right away

    session = runbound.current_session()
    assert session.step_count == 1
    assert session.total_tokens == 0  # not one chunk was ever read


# --- async streaming --------------------------------------------------------


def test_async_stream_reports_once_at_exhaustion():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(completions=FakeAsyncStreamingCompletions()))

    async def main():
        stream = await client.chat.completions.create(
            model="gpt-4o", messages=[], stream=True, stream_options={"include_usage": True}
        )
        return [chunk.text async for chunk in stream]

    texts = asyncio.run(main())

    assert texts == ["Hel", "lo ", "there", ""]
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)


def test_async_stream_aclose_before_exhaustion_reports_once():
    runbound.init()
    completions = FakeAsyncStreamingCompletions()
    client = runbound.wrap(FakeOpenAI(completions=completions))

    async def main():
        stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        async for _ in stream:
            break
        await stream.aclose()
        await stream.aclose()

    asyncio.run(main())

    assert completions.streams[0].closed is True
    assert runbound.current_session().step_count == 1


def test_async_stream_context_manager_reports_once():
    runbound.init()
    completions = FakeAsyncStreamingCompletions()
    client = runbound.wrap(FakeOpenAI(completions=completions))

    async def main():
        stream = await client.chat.completions.create(
            model="gpt-4o", messages=[], stream=True, stream_options={"include_usage": True}
        )
        async with stream as guarded:
            async for _ in guarded:
                pass

    asyncio.run(main())

    assert completions.streams[0].entered is True
    session = runbound.current_session()
    assert (session.step_count, session.total_tokens) == (1, 1500)


def test_abandoned_async_stream_is_recorded_as_one_partial_call():
    runbound.init()
    client = runbound.wrap(FakeOpenAI(completions=FakeAsyncStreamingCompletions()))

    async def main():
        stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        async for _ in stream:
            break  # one chunk, "Hel" — never exhausted, closed or exited

    asyncio.run(main())
    gc.collect()

    session = runbound.current_session()
    assert session.step_count == 1
    # FakeChunk carries its text on `.text`, not the `choices[].delta.content`
    # shape the chat surface's chunk-text reader knows -- same as every other
    # test in this file -- so no chars are ever seen and the chars/4 fallback
    # reports zero rather than guessing wrong.
    assert session.total_tokens == 0


def test_async_stream_budget_trip_raises_out_of_the_consumer_loop():
    runbound.init(budget_usd=0.005, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(completions=FakeAsyncStreamingCompletions()))
    seen = []

    async def main():
        stream = await client.chat.completions.create(
            model="gpt-4o", messages=[], stream=True, stream_options={"include_usage": True}
        )
        async for chunk in stream:
            seen.append(chunk.text)

    with pytest.raises(GuardrailTripped):
        asyncio.run(main())

    assert seen == ["Hel", "lo ", "there", ""]


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
