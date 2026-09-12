"""Tests for wrapper call timing and reasoning-token extraction.

The stopwatch lives in one place — ``runbound.wrappers._now()``, which calls
``time.monotonic()`` off the ``runbound.wrappers`` module namespace — so a
single monkeypatch of ``runbound.wrappers.time`` gives every path (sync,
async, sync stream, async stream) a controllable clock.

Nothing from openai/anthropic is imported; the fakes are duck-typed, like the
rest of the wrapper suite.
"""

import asyncio

import pytest

import runbound
from runbound import api
from runbound import wrappers
from runbound.wrappers import anthropic_wrapper, openai_wrapper


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(wrappers, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


# --- fakes ------------------------------------------------------------------


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class ExplodingDetails:
    """A usage object whose details field raises on attribute access."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    @property
    def completion_tokens_details(self):
        raise RuntimeError("this SDK object is angry")


class FakeResponse:
    def __init__(self, model=None, usage=None):
        self.model = model
        self.usage = usage
        self.content = "hello"


class FakeChunk:
    def __init__(self, text="", model="gpt-4o", usage=None):
        self.text = text
        self.model = model
        self.usage = usage


class SlowCompletions:
    """OpenAI-shaped ``create`` that burns ``elapsed`` seconds of fake time."""

    def __init__(self, clock, elapsed=2.0, usage=None, model="gpt-4o"):
        self.clock = clock
        self.elapsed = elapsed
        self.usage = usage
        self.model = model
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.clock.advance(self.elapsed)
        return FakeResponse(model=self.model, usage=self.usage)


class SlowAsyncCompletions(SlowCompletions):
    async def create(self, **kwargs):
        self.calls += 1
        self.clock.advance(self.elapsed)
        return FakeResponse(model=self.model, usage=self.usage)


class SlowMessages:
    """Anthropic-shaped ``create`` that burns fake time."""

    def __init__(self, clock, elapsed=2.0, usage=None, model="claude-sonnet-4-5"):
        self.clock = clock
        self.elapsed = elapsed
        self.usage = usage
        self.model = model
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.clock.advance(self.elapsed)
        return FakeResponse(model=self.model, usage=self.usage)


class TickingStream:
    """A stream whose every chunk costs the clock ``per_chunk`` seconds."""

    def __init__(self, chunks, clock, per_chunk=1.0):
        self._chunks = iter(chunks)
        self.clock = clock
        self.per_chunk = per_chunk
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._chunks)
        self.clock.advance(self.per_chunk)
        return chunk

    def close(self):
        self.closed = True


class TickingAsyncStream:
    def __init__(self, chunks, clock, per_chunk=1.0):
        self._chunks = list(chunks)
        self._index = 0
        self.clock = clock
        self.per_chunk = per_chunk

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        self.clock.advance(self.per_chunk)
        return chunk


class StreamingCompletions:
    """Streams ``chunks`` when asked, after ``elapsed`` seconds of setup."""

    def __init__(self, chunks, clock, elapsed=0.5, per_chunk=1.0, stream_cls=TickingStream):
        self.chunks = chunks
        self.clock = clock
        self.elapsed = elapsed
        self.per_chunk = per_chunk
        self.stream_cls = stream_cls
        self.stream = None

    def create(self, **kwargs):
        self.clock.advance(self.elapsed)
        self.stream = self.stream_cls(self.chunks, self.clock, self.per_chunk)
        return self.stream


class AsyncStreamingCompletions(StreamingCompletions):
    async def create(self, **kwargs):
        self.clock.advance(self.elapsed)
        self.stream = self.stream_cls(self.chunks, self.clock, self.per_chunk)
        return self.stream


def _openai_client(completions):
    client = type("FakeOpenAI", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = completions
    return client


def _anthropic_client(messages):
    client = type("FakeAnthropic", (), {})()
    client.messages = messages
    return client


class _EventRecorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self) -> None:
        self.events: list = []

    def check(self, state, event, config):
        self.events.append(event)
        return None


def _recorder() -> _EventRecorder:
    spy = _EventRecorder()
    api._ENGINE.detectors.insert(0, spy)
    return spy


# --- duration: non-streaming ------------------------------------------------


def test_sync_call_duration_reaches_the_event_and_the_session(clock):
    runbound.init()
    client = runbound.wrap(_openai_client(SlowCompletions(clock, elapsed=2.5)))
    spy = _recorder()

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert spy.events[0].duration_s == pytest.approx(2.5)
    duration, _work, _cost = runbound.current_session().recent_calls[-1]
    assert duration == pytest.approx(2.5)


def test_async_call_duration_reaches_the_event(clock):
    runbound.init()
    client = runbound.wrap(_openai_client(SlowAsyncCompletions(clock, elapsed=4.0)))
    spy = _recorder()

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert spy.events[0].duration_s == pytest.approx(4.0)
    assert runbound.current_session().recent_calls[-1][0] == pytest.approx(4.0)


def test_anthropic_call_is_timed_too(clock):
    runbound.init()
    client = runbound.wrap(_anthropic_client(SlowMessages(clock, elapsed=1.25)))
    spy = _recorder()

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert spy.events[0].duration_s == pytest.approx(1.25)


def test_an_untimed_call_reports_zero_not_a_negative_duration(clock):
    """A clock that goes backwards (NTP-proof paranoia) never reports < 0."""
    runbound.init()
    completions = SlowCompletions(clock, elapsed=-5.0)
    client = runbound.wrap(_openai_client(completions))
    spy = _recorder()

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert spy.events[0].duration_s == 0.0


# --- duration: streaming ----------------------------------------------------


def test_sync_stream_duration_runs_from_the_call_to_exhaustion(clock):
    runbound.init()
    chunks = [FakeChunk(text=text) for text in ("a", "b", "c")]
    completions = StreamingCompletions(chunks, clock, elapsed=0.5, per_chunk=1.0)
    client = runbound.wrap(_openai_client(completions))
    spy = _recorder()

    consumed = list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert [chunk.text for chunk in consumed] == ["a", "b", "c"]
    # 0.5 setup + 3 chunks * 1.0
    assert spy.events[0].duration_s == pytest.approx(3.5)


def test_sync_stream_duration_stops_at_an_early_close(clock):
    runbound.init()
    chunks = [FakeChunk(text=text) for text in ("a", "b", "c")]
    completions = StreamingCompletions(chunks, clock, elapsed=0.5, per_chunk=1.0)
    client = runbound.wrap(_openai_client(completions))
    spy = _recorder()

    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    next(iter(stream))
    stream.close()

    assert completions.stream.closed is True
    assert spy.events[0].duration_s == pytest.approx(1.5)  # 0.5 setup + one chunk


def test_async_stream_duration_runs_to_exhaustion(clock):
    runbound.init()
    chunks = [FakeChunk(text=text) for text in ("a", "b")]
    completions = AsyncStreamingCompletions(
        chunks, clock, elapsed=0.5, per_chunk=2.0, stream_cls=TickingAsyncStream
    )
    client = runbound.wrap(_openai_client(completions))
    spy = _recorder()

    async def drive():
        stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        return [chunk async for chunk in stream]

    assert len(asyncio.run(drive())) == 2
    assert spy.events[0].duration_s == pytest.approx(4.5)  # 0.5 + 2 * 2.0


# --- reasoning tokens -------------------------------------------------------


def test_openai_reasoning_tokens_are_read_and_counted_as_output_work(clock):
    runbound.init()
    usage = FakeUsage(
        prompt_tokens=1000,
        completion_tokens=500,
        completion_tokens_details=FakeUsage(reasoning_tokens=200),
    )
    client = runbound.wrap(_openai_client(SlowCompletions(clock, usage=usage)))
    spy = _recorder()

    client.chat.completions.create(model="gpt-4o", messages=[])

    event = spy.events[0]
    assert (event.tokens_in, event.tokens_out, event.tokens_reasoning) == (1000, 500, 200)
    # completion_tokens already contains the 200 reasoning tokens; output
    # work must not add them again.
    assert runbound.current_session().recent_calls[-1][1] == 500


def test_openai_reasoning_tokens_arrive_on_the_final_stream_chunk(clock):
    runbound.init()
    usage = FakeUsage(
        prompt_tokens=10,
        completion_tokens=20,
        completion_tokens_details=FakeUsage(reasoning_tokens=64),
    )
    chunks = [FakeChunk(text="a"), FakeChunk(usage=usage)]
    completions = StreamingCompletions(chunks, clock, elapsed=0.0, per_chunk=0.0)
    client = runbound.wrap(_openai_client(completions))
    spy = _recorder()

    list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    event = spy.events[0]
    assert (event.tokens_out, event.tokens_reasoning) == (20, 64)
    # output work is tokens_out alone: reasoning is already inside it.
    assert runbound.current_session().recent_calls[-1][1] == 20


def test_openai_without_reasoning_details_reports_zero(clock):
    runbound.init()
    usage = FakeUsage(prompt_tokens=10, completion_tokens=20)
    client = runbound.wrap(_openai_client(SlowCompletions(clock, usage=usage)))
    spy = _recorder()

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert spy.events[0].tokens_reasoning == 0


def test_malformed_reasoning_details_reads_as_zero_and_the_call_survives(clock):
    runbound.init()
    usage = ExplodingDetails(prompt_tokens=10, completion_tokens=20)
    client = runbound.wrap(_openai_client(SlowCompletions(clock, usage=usage)))
    spy = _recorder()

    response = client.chat.completions.create(model="gpt-4o", messages=[])

    assert response.content == "hello"
    event = spy.events[0]
    assert (event.tokens_out, event.tokens_reasoning) == (20, 0)


def test_anthropic_reports_no_reasoning_tokens_today(clock):
    runbound.init()
    usage = FakeUsage(input_tokens=1000, output_tokens=500)
    client = runbound.wrap(_anthropic_client(SlowMessages(clock, usage=usage)))
    spy = _recorder()

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert spy.events[0].tokens_reasoning == 0


def test_anthropic_thinking_tokens_are_read_if_a_future_sdk_sends_them(clock):
    runbound.init()
    usage = FakeUsage(input_tokens=1000, output_tokens=500, thinking_tokens=300)
    client = runbound.wrap(_anthropic_client(SlowMessages(clock, usage=usage)))
    spy = _recorder()

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert spy.events[0].tokens_reasoning == 300


def test_anthropic_stream_reads_thinking_tokens_off_message_delta(clock):
    """message_delta restates the running counts; a future thinking field folds in."""
    usage = wrappers._StreamUsage(model="claude-sonnet-4-5")
    start = FakeUsage(
        type="message_start",
        message=FakeUsage(model="claude-sonnet-4-5", usage=FakeUsage(input_tokens=10)),
    )
    delta = FakeUsage(type="message_delta", usage=FakeUsage(output_tokens=20, thinking_tokens=7))

    anthropic_wrapper._chunk_usage(start, usage)
    anthropic_wrapper._chunk_usage(delta, usage)

    assert (usage.tokens_in, usage.tokens_out, usage.tokens_reasoning) == (10, 20, 7)


# --- the report contract ----------------------------------------------------


def test_a_five_argument_report_receives_duration_and_reasoning(clock):
    seen = []
    usage = FakeUsage(
        prompt_tokens=1,
        completion_tokens=2,
        completion_tokens_details=FakeUsage(reasoning_tokens=3),
    )
    client = _openai_client(SlowCompletions(clock, elapsed=7.0, usage=usage))

    def report(model, tokens_in, tokens_out, duration_s, tokens_reasoning):
        seen.append((model, tokens_in, tokens_out, duration_s, tokens_reasoning))

    openai_wrapper.install(client, report)
    client.chat.completions.create(model="gpt-4o", messages=[])

    assert seen == [("gpt-4o", 1, 2, 7.0, 3)]


def test_a_legacy_three_argument_report_still_works(clock):
    """Pre-timing callbacks keep working: the call falls back to three args."""
    seen = []
    client = _openai_client(SlowCompletions(clock, elapsed=7.0))

    def report(model, tokens_in, tokens_out):
        seen.append((model, tokens_in, tokens_out))

    openai_wrapper.install(client, report)
    response = client.chat.completions.create(model="gpt-4o", messages=[])

    assert response.content == "hello"
    assert seen == [("gpt-4o", 0, 0)]


def test_a_legacy_three_argument_report_still_works_for_streams(clock):
    seen = []
    chunks = [FakeChunk(text="a")]
    client = _openai_client(StreamingCompletions(chunks, clock, elapsed=1.0, per_chunk=1.0))

    def report(model, tokens_in, tokens_out):
        seen.append((model, tokens_in, tokens_out))

    openai_wrapper.install(client, report)
    list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert seen == [("gpt-4o", 0, 0)]


def test_a_type_error_from_inside_report_is_not_retried(clock):
    """The fallback is for argument binding only — a broken report runs once."""
    calls = []
    client = _openai_client(SlowCompletions(clock))

    def report(model, tokens_in, tokens_out, duration_s, tokens_reasoning):
        calls.append(model)
        raise TypeError("report's own bug")

    openai_wrapper.install(client, report)

    with pytest.raises(TypeError, match="report's own bug"):
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert calls == ["gpt-4o"]


def test_anthropic_install_also_honours_the_legacy_report(clock):
    seen = []
    client = _anthropic_client(SlowMessages(clock, elapsed=2.0))

    def report(model, tokens_in, tokens_out):
        seen.append((model, tokens_in, tokens_out))

    anthropic_wrapper.install(client, report)
    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert seen == [("claude-sonnet-4-5", 0, 0)]
