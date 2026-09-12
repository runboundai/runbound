"""Tests for T60: a streamed response that is abandoned mid-flight now counts.

Built at the wrapper level directly — a :class:`RecordingHooks` double stands
in for the api layer's real hooks, so these tests exercise the ledger and its
``weakref.finalize`` callback (:func:`runbound.wrappers._report_abandoned`)
without depending on whether ``runbound.api._Hooks.abandoned`` has landed
yet. Provider-specific chunk parsing is covered elsewhere
(``tests/test_streaming.py``, ``tests/test_tool_requests.py``); the fakes here
use a minimal chunk shape of their own, just enough to drive the ledger.

CPython collects an object the instant its last reference drops, as long as it
is not part of a reference cycle — which none of these proxies are — so
``gc.collect()`` after ``del`` is a belt-and-braces step, not what actually
makes collection happen.
"""

import asyncio
import gc
import time
import weakref

import pytest

import runbound
import runbound.wrappers as wrappers
from runbound import api
from runbound.wrappers import (
    _NO_HOOKS,
    _AsyncGuardedStream,
    _GuardedStream,
    _await_pending_delay,
    call_before,
    estimated_tokens,
)


class FakeUsage:
    """A provider's usage object: whatever fields it happens to carry."""

    def __init__(self, tokens_in=0, tokens_out=0):
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


class FakeChunk:
    def __init__(self, text="", model="gpt-4o", usage=None):
        self.text = text
        self.model = model
        self.usage = usage


class FakeStream:
    """A minimal sync provider stream: iterable and closeable."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True


class ExplodingStream:
    """A sync provider stream that raises after its chunks run out."""

    def __init__(self, chunks, exc):
        self._chunks = iter(chunks)
        self._exc = exc

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._chunks, None)
        if chunk is None:
            raise self._exc
        return chunk


class FakeAsyncStream:
    """A minimal async provider stream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


def _usage_reader(chunk, usage):
    """A ``ChunkUsage`` for :class:`FakeChunk`: model plus its own usage."""
    if isinstance(chunk.model, str) and chunk.model:
        usage.model = chunk.model
    if chunk.usage is not None:
        usage.tokens_in = max(usage.tokens_in, chunk.usage.tokens_in)
        usage.tokens_out = max(usage.tokens_out, chunk.usage.tokens_out)


def _text_reader(chunk):
    """A ``ChunkText`` for :class:`FakeChunk`: its own ``text``."""
    return chunk.text


class RecordingHooks:
    """A hooks double that records every call, for a test to assert on.

    Implements the full contract a wrapper calls: ``before(provider,
    model=None)``, ``success``, ``error``, ``release``, ``tool_request``,
    ``abandoned`` and ``take_pending_delay`` — the two T60 adds, plus the
    5-argument ``before`` T59 is adding concurrently.
    """

    def __init__(self, estimate_tokens: bool = False) -> None:
        self.estimate_tokens = estimate_tokens
        self.before_calls: list[tuple] = []
        self.success_calls: list[str] = []
        self.error_calls: list[tuple] = []
        self.release_calls: list[str] = []
        self.tool_request_calls: list[tuple] = []
        self.abandoned_calls: list[dict] = []

    def before(self, provider: str, model: str | None = None) -> None:
        self.before_calls.append((provider, model))

    def success(self, provider: str) -> None:
        self.success_calls.append(provider)

    def error(self, model, exc, duration_s, provider) -> None:
        self.error_calls.append((model, exc, duration_s, provider))

    def release(self, provider: str) -> None:
        self.release_calls.append(provider)

    def tool_request(self, name, args_hash) -> None:
        self.tool_request_calls.append((name, args_hash))

    def abandoned(self, model, tokens_in, tokens_out, duration_s, provider, estimated) -> None:
        self.abandoned_calls.append(
            {
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "duration_s": duration_s,
                "provider": provider,
                "estimated": estimated,
            }
        )

    def take_pending_delay(self) -> float:
        return 0.0


def _guarded(stream, hooks, report=None, request_chars=0, started_at=None):
    return _GuardedStream(
        stream,
        _usage_reader,
        report or (lambda *a, **k: None),
        "gpt-4o",
        started_at=started_at,
        hooks=hooks,
        provider="openai@test",
        chunk_requests=None,
        chunk_text=_text_reader,
        request_chars=request_chars,
    )


def _async_guarded(stream, hooks, report=None, request_chars=0, started_at=None):
    return _AsyncGuardedStream(
        stream,
        _usage_reader,
        report or (lambda *a, **k: None),
        "gpt-4o",
        started_at=started_at,
        hooks=hooks,
        provider="openai@test",
        chunk_requests=None,
        chunk_text=_text_reader,
        request_chars=request_chars,
    )


# --- sync abandonment --------------------------------------------------------


def test_sync_abandoned_stream_reports_one_partial_call_estimated():
    hooks = RecordingHooks()
    stream = _guarded(FakeStream([FakeChunk("Hel"), FakeChunk("lo ")]), hooks)

    iterator = iter(stream)
    next(iterator)
    next(iterator)
    del stream, iterator
    gc.collect()

    assert len(hooks.abandoned_calls) == 1
    call = hooks.abandoned_calls[0]
    assert call["estimated"] is True
    assert call["tokens_out"] == estimated_tokens(len("Hel") + len("lo "))
    assert call["model"] == "gpt-4o"
    assert hooks.release_calls == ["openai@test"]
    assert hooks.success_calls == []
    assert hooks.error_calls == []


def test_sync_abandoned_stream_with_usage_chunks_uses_the_usage():
    hooks = RecordingHooks()
    chunks = [
        FakeChunk("Hel", usage=FakeUsage(tokens_in=100, tokens_out=5)),
        FakeChunk("lo ", usage=FakeUsage(tokens_in=100, tokens_out=12)),
    ]
    stream = _guarded(FakeStream(chunks), hooks)

    iterator = iter(stream)
    next(iterator)
    next(iterator)
    del stream, iterator
    gc.collect()

    assert len(hooks.abandoned_calls) == 1
    call = hooks.abandoned_calls[0]
    assert call["estimated"] is False
    assert call["tokens_in"] == 100
    assert call["tokens_out"] == 12
    assert hooks.release_calls == ["openai@test"]


# --- BLOCKER 3: duration is call time, not time spent sitting in memory -----


def test_abandoned_duration_is_measured_to_the_last_chunk_not_to_collection():
    """Two chunks arrive instantly, then the stream sits around for 0.3s.

    Measuring duration at collection time (``now - started_at``) would report
    at least 0.3s -- exactly the "call took 2.2s, cap 2.0s" false positive the
    probe found. Measuring to the last chunk actually observed reports the
    real, sub-tenth-of-a-second call time instead.
    """
    hooks = RecordingHooks()
    started_at = time.monotonic()
    stream = _guarded(
        FakeStream([FakeChunk("Hel"), FakeChunk("lo ")]), hooks, started_at=started_at
    )

    iterator = iter(stream)
    next(iterator)
    next(iterator)
    time.sleep(0.3)
    del stream, iterator
    gc.collect()

    assert len(hooks.abandoned_calls) == 1
    assert hooks.abandoned_calls[0]["duration_s"] < 0.1


def test_abandoned_duration_is_zero_when_no_chunk_was_ever_observed():
    """A stream dropped before its first chunk reports 0.0, not time-to-drop."""
    hooks = RecordingHooks()
    stream = _guarded(FakeStream([FakeChunk("Hel")]), hooks, started_at=time.monotonic())

    time.sleep(0.05)
    del stream
    gc.collect()

    assert hooks.abandoned_calls[0]["duration_s"] == 0.0


def test_abandoned_duration_is_zero_when_the_call_was_never_timed():
    """``started_at=None`` (an untimed call) reports 0.0, never a real elapsed."""
    hooks = RecordingHooks()
    stream = _guarded(FakeStream([FakeChunk("Hel")]), hooks, started_at=None)

    next(iter(stream))
    time.sleep(0.05)
    del stream
    gc.collect()

    assert hooks.abandoned_calls[0]["duration_s"] == 0.0


def test_exhausted_stream_then_dropped_reports_once_not_twice():
    hooks = RecordingHooks()
    reports = []
    stream = _guarded(
        FakeStream([FakeChunk("Hel"), FakeChunk("lo ")]),
        hooks,
        report=lambda *a, **k: reports.append(a),
    )

    for _ in stream:
        pass
    del stream
    gc.collect()

    assert len(reports) == 1
    assert hooks.abandoned_calls == []
    assert hooks.release_calls == ["openai@test"]
    assert hooks.success_calls == ["openai@test"]


def test_closed_stream_then_dropped_reports_once():
    hooks = RecordingHooks()
    reports = []
    stream = _guarded(
        FakeStream([FakeChunk("Hel"), FakeChunk("lo ")]),
        hooks,
        report=lambda *a, **k: reports.append(a),
    )

    next(iter(stream))
    stream.close()
    del stream
    gc.collect()

    assert len(reports) == 1
    assert hooks.abandoned_calls == []
    assert hooks.release_calls == ["openai@test"]


def test_failed_stream_reports_error_once_and_no_abandoned_call():
    hooks = RecordingHooks()
    stream = _guarded(ExplodingStream([FakeChunk("Hel")], RuntimeError("connection reset")), hooks)

    with pytest.raises(RuntimeError, match="connection reset"):
        list(stream)

    del stream
    gc.collect()

    assert len(hooks.error_calls) == 1
    assert hooks.abandoned_calls == []
    assert hooks.release_calls == ["openai@test"]


def test_finalizer_holds_no_strong_reference_to_the_proxy():
    """The proof the task asks for: the proxy is actually collected.

    A finalizer callback that closed over ``self`` (directly, or through a
    bound method) would keep every guarded stream alive for the life of the
    process; ``weakref.ref(...)() is None`` after the drop proves it does not.
    """
    hooks = RecordingHooks()
    stream = _guarded(FakeStream([FakeChunk("Hel"), FakeChunk("lo ")]), hooks)
    next(iter(stream))
    ref = weakref.ref(stream)

    del stream
    gc.collect()

    assert ref() is None
    assert len(hooks.abandoned_calls) == 1


# --- SHOULD 4: never reports during interpreter teardown ---------------------


def test_finalizer_is_not_registered_for_atexit():
    """A process that exits with the stream still referenced never reports it.

    ``weakref.finalize`` defaults to ``atexit=True``, which would instead run
    this finalizer during interpreter teardown -- calling out over
    ``urllib`` and logging when threads and handlers may already be gone.
    """
    hooks = RecordingHooks()
    stream = _guarded(FakeStream([FakeChunk("Hel")]), hooks)

    assert stream._finalizer.atexit is False


# --- async abandonment --------------------------------------------------------


def test_async_abandoned_stream_reports_one_partial_call_estimated():
    hooks = RecordingHooks()
    stream = _async_guarded(FakeAsyncStream([FakeChunk("Hel"), FakeChunk("lo ")]), hooks)

    async def consume_two():
        iterator = stream.__aiter__()
        await iterator.__anext__()
        await iterator.__anext__()

    asyncio.run(consume_two())
    del stream
    gc.collect()

    assert len(hooks.abandoned_calls) == 1
    call = hooks.abandoned_calls[0]
    assert call["estimated"] is True
    assert call["tokens_out"] == estimated_tokens(len("Hel") + len("lo "))
    assert hooks.release_calls == ["openai@test"]


def test_async_abandoned_stream_with_usage_chunks_uses_the_usage():
    hooks = RecordingHooks()
    chunks = [
        FakeChunk("Hel", usage=FakeUsage(tokens_in=50, tokens_out=3)),
        FakeChunk("lo ", usage=FakeUsage(tokens_in=50, tokens_out=9)),
    ]
    stream = _async_guarded(FakeAsyncStream(chunks), hooks)

    async def consume_two():
        iterator = stream.__aiter__()
        await iterator.__anext__()
        await iterator.__anext__()

    asyncio.run(consume_two())
    del stream
    gc.collect()

    assert len(hooks.abandoned_calls) == 1
    call = hooks.abandoned_calls[0]
    assert call["estimated"] is False
    assert call["tokens_out"] == 9
    assert hooks.release_calls == ["openai@test"]


def test_async_exhausted_stream_then_dropped_reports_once_not_twice():
    hooks = RecordingHooks()
    reports = []
    stream = _async_guarded(
        FakeAsyncStream([FakeChunk("Hel"), FakeChunk("lo ")]),
        hooks,
        report=lambda *a, **k: reports.append(a),
    )

    async def drain():
        async for _ in stream:
            pass

    asyncio.run(drain())
    del stream
    gc.collect()

    assert len(reports) == 1
    assert hooks.abandoned_calls == []
    assert hooks.release_calls == ["openai@test"]


# --- the request-side estimate still requires opting in ----------------------


def test_abandoned_tokens_in_estimate_only_applies_when_the_hooks_opted_in():
    """Output chars are always counted; the input-side estimate stays opt-in.

    Matches the non-streamed and normal-completion paths: ``estimate_tokens``
    gates whether a *request's own* char count is ever turned into a guessed
    ``tokens_in`` — turning it on for every abandoned stream regardless would
    silently start pricing calls nobody asked to have estimated.
    """
    not_estimating = RecordingHooks(estimate_tokens=False)
    stream = _guarded(FakeStream([FakeChunk("Hel")]), not_estimating, request_chars=400)
    next(iter(stream))
    del stream
    gc.collect()
    assert not_estimating.abandoned_calls[0]["tokens_in"] == 0

    estimating_hooks = RecordingHooks(estimate_tokens=True)
    stream = _guarded(FakeStream([FakeChunk("Hel")]), estimating_hooks, request_chars=400)
    next(iter(stream))
    del stream
    gc.collect()
    assert estimating_hooks.abandoned_calls[0]["tokens_in"] == estimated_tokens(400)


# --- SHOULD 10: a truthful zero usage is trusted, not re-estimated ----------


def test_abandoned_stream_with_a_truthful_zero_usage_is_not_re_estimated():
    """A stream whose only usage block said 0 tokens is believed, not guessed.

    Both "the endpoint told us zero" and "the endpoint told us nothing" leave
    the running usage at its zero defaults -- only whether a usage block was
    actually parsed tells them apart. Before the fix, ``usage_seen`` was only
    set on a nonzero count, so this case fell back to the chars/4 estimate
    (0 chars streamed -> 0 estimated tokens, but wrongly marked estimated).
    """
    hooks = RecordingHooks()
    chunks = [FakeChunk("", usage=FakeUsage(tokens_in=0, tokens_out=0))]
    stream = _guarded(FakeStream(chunks), hooks)

    next(iter(stream))
    del stream
    gc.collect()

    assert len(hooks.abandoned_calls) == 1
    call = hooks.abandoned_calls[0]
    assert call["estimated"] is False
    assert call["tokens_in"] == 0
    assert call["tokens_out"] == 0


# --- call_before: the model kwarg, tolerantly -------------------------------


def test_call_before_passes_the_model_when_hooks_accept_it():
    hooks = RecordingHooks()
    call_before(hooks, "openai@test", "gpt-4o")
    assert hooks.before_calls == [("openai@test", "gpt-4o")]


def test_call_before_falls_back_for_an_older_one_argument_before():
    calls = []

    class OldHooks:
        def before(self, provider):
            calls.append(provider)

    call_before(OldHooks(), "openai@test", "gpt-4o")
    assert calls == ["openai@test"]


def test_call_before_lets_a_deliberate_typeerror_from_inside_before_propagate():
    class BrokenHooks:
        def before(self, provider, model=None):
            raise TypeError("not a signature mismatch")

    with pytest.raises(TypeError, match="not a signature mismatch"):
        call_before(BrokenHooks(), "openai@test", "gpt-4o")


# --- take_pending_delay: async throttle catch-up -----------------------------


def test_await_pending_delay_sleeps_for_a_positive_delay():
    class DelayHooks:
        def take_pending_delay(self):
            return 0.01

    async def main():
        loop = asyncio.get_event_loop()
        started = loop.time()
        await _await_pending_delay(DelayHooks())
        return loop.time() - started

    assert asyncio.run(main()) >= 0.01


def test_await_pending_delay_is_a_no_op_without_the_hook():
    class NoDelayHooks:
        pass

    asyncio.run(_await_pending_delay(NoDelayHooks()))  # must not raise


def test_await_pending_delay_is_fail_open_when_the_hook_raises():
    class ExplodingHooks:
        def take_pending_delay(self):
            raise RuntimeError("boom")

    asyncio.run(_await_pending_delay(ExplodingHooks()))  # must not raise


def test_no_hooks_abandoned_and_take_pending_delay_are_no_ops():
    _NO_HOOKS.abandoned("gpt-4o", 1, 2, 0.1, "openai@test", True)  # must not raise
    assert _NO_HOOKS.take_pending_delay() == 0.0


# --- wired into the shared async create wrapper ------------------------------


def test_acreate_awaits_the_pending_delay_after_a_non_streamed_report():
    hooks = RecordingHooks()
    hooks.take_pending_delay = lambda: 0.01

    async def original(**kwargs):
        return FakeChunk("hi")

    def finish(response, kwargs, report, started_at, hooks, label, is_async):
        hooks.success(label)
        return response

    guarded = wrappers.guarded_create(
        original, lambda *a, **k: None, hooks, wrappers.fixed_label("openai@test"), finish
    )

    async def main():
        loop = asyncio.get_event_loop()
        started = loop.time()
        await guarded(model="gpt-4o")
        return loop.time() - started

    assert asyncio.run(main()) >= 0.01


def test_acreate_does_not_await_a_delay_for_a_streamed_response():
    calls = []
    hooks = RecordingHooks()

    def take_pending_delay():
        calls.append(1)
        return 0.0

    hooks.take_pending_delay = take_pending_delay

    async def original(**kwargs):
        return FakeAsyncStream([FakeChunk("hi")])

    def finish(response, kwargs, report, started_at, hooks, label, is_async):
        return _async_guarded(response, hooks)

    guarded = wrappers.guarded_create(
        original, lambda *a, **k: None, hooks, wrappers.fixed_label("openai@test"), finish
    )

    async def main():
        return await guarded(model="gpt-4o", stream=True)

    result = asyncio.run(main())
    assert isinstance(result, _AsyncGuardedStream)
    assert calls == []


# --- BLOCKER 2: the abandoned report is charged to the caller's session -----
#
# Built through the real ``runbound.wrap()`` install path and the real
# ``api._Hooks``, unlike the rest of this file, because the bug is specific
# to how ``runbound.session``'s contextvar and the finalizer's arbitrary
# thread/task interact -- a ``RecordingHooks`` double has no session to get
# this wrong about.


class _OneShotChunk:
    """OpenAI-chat-shaped chunk: enough for ``openai_wrapper``'s own readers."""

    def __init__(self, text=""):
        self.text = text
        self.model = "gpt-4o"
        self.usage = None


class _OneShotStream:
    """A sync provider stream, iterable exactly once, never stashed anywhere."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)


class _OneShotCompletions:
    """OpenAI-shaped ``create`` that always streams a fresh, unreferenced stream."""

    def create(self, **kwargs):
        return _OneShotStream([_OneShotChunk("Hel"), _OneShotChunk("lo ")])


class _OneShotOpenAI:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _OneShotCompletions()


def test_abandoned_stream_is_charged_to_the_session_it_was_opened_in():
    """Opened inside session("alice"), collected inside session("bob").

    Before the fix, the finalizer ran ``hooks.abandoned`` on whatever session
    the ``contextvars`` machinery happened to report as current on the
    collector's thread -- here, bob's, purely because that is where the last
    reference to the stream was dropped. ``_StreamLedger`` now snapshots the
    caller's context at construction time and the finalizer replays the
    report inside that snapshot, so the call lands on alice's session however
    long it waits to be collected or on what thread that happens.
    """
    api._teardown_for_tests()
    try:
        runbound.init(auto_wrap=False)
        client = runbound.wrap(_OneShotOpenAI())

        with runbound.session("alice"):
            stream = client.chat.completions.create(
                model="gpt-4o", messages=[], stream=True
            )
            next(iter(stream))
        # Reference still held here, outside alice's block -- collection has
        # not happened yet: CPython drops it the instant the last reference
        # goes, which must happen inside bob's block for this to probe the
        # right thing.

        with runbound.session("bob"):
            del stream
            gc.collect()

        with runbound.session("alice") as alice:
            pass
        with runbound.session("bob") as bob:
            pass

        assert alice.step_count == 1
        assert bob.step_count == 0
    finally:
        api._teardown_for_tests()
