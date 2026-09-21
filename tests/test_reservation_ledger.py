"""Money held for the lifetime of the call, so a budget cannot be crossed.

An earlier check compared a call's worst case against *settled* spend
only, so N calls
racing at the budget edge were all admitted and the overshoot window was
N × worst case. The reservation is now a :class:`~runbound.state.Hold` that
lives from admission until the call closes out, carried on the session's
``reserved`` ledger, and the invariant it buys is::

    settled + reserved >= committed spend        (INVARIANTS.md, invariant 2)

so exactly ``floor(remaining / worst_case)`` of N concurrent calls are
admitted.

Prices are pinned with ``custom_prices`` so the worst case is an exact dollar
figure rather than something that drifts with the built-in table: ``MODEL``
costs **$1.00 per output token** and nothing per input token, so a request
capped at one output token is worth exactly ``W = $1.00`` and a fake response
reporting no tokens settles at $0.00 — the reservation is then the only thing
moving, which is what these tests are about.

Every scenario ends with :func:`assert_reserved_zero`: a hold that is not
given back on some path is a budget that shrinks for the rest of the run, and
that is the one failure mode a "cannot be crossed" promise cannot survive.
"""

import asyncio
import gc
import threading
import time

import pytest

import runbound
from runbound import api, state
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import GuardrailTripped
from runbound.state import Hold, SessionState

#: A model priced so that one output token costs exactly one dollar, and input
#: costs nothing at all — the worst case of a capped call is then the cap, in
#: dollars, with no chars/4 estimate anywhere in it.
MODEL = "hold-1"
PRICES = {MODEL: (0.0, 1_000_000.0)}

#: The worst case of a ``max_tokens=1`` call on ``MODEL``.
W = 1.0

SHORT = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def assert_reserved_zero() -> None:
    """No session anywhere is still holding money.

    Reads every session this process knows — the keyed registry and the
    default one — rather than just the one a test happened to look at, so a
    hold leaked onto the wrong session is caught too.
    """
    states = list(api._REGISTRY.values())
    if api._SESSION is not None:
        states.append(api._SESSION)
    for session in states:
        assert not any(session.reserved.values()), session.reserved


def until(predicate, timeout: float = 10.0) -> bool:
    """Wait for ``predicate()``, polling. False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --- fakes ------------------------------------------------------------------


class _Usage:
    def __init__(self, tokens_out: int = 0):
        self.prompt_tokens = 0
        self.completion_tokens = tokens_out


class _Response:
    def __init__(self, model, tokens_out: int = 0):
        self.model = model
        self.usage = _Usage(tokens_out)


class _AnthropicUsage:
    def __init__(self, tokens_out: int = 0):
        self.input_tokens = 0
        self.output_tokens = tokens_out


class _AnthropicResponse:
    def __init__(self, model, tokens_out: int = 0):
        self.model = model
        self.usage = _AnthropicUsage(tokens_out)
        self.content = []


class Completions:
    """One ``create``, shaped to order: blocking, erroring, or streaming.

    ``gate`` parks every admitted call until it is set, which is how N calls
    are held in flight at once; ``inside`` is how many are parked right now.
    """

    def __init__(
        self,
        *,
        gate=None,
        tokens_out: int = 0,
        error: BaseException | None = None,
        stream_factory=None,
        response=_Response,
    ):
        self.gate = gate
        self.tokens_out = tokens_out
        self.error = error
        self.stream_factory = stream_factory
        self.response = response
        self.calls = 0
        self.inside = 0
        self.streams: list = []
        self._lock = threading.Lock()

    def _enter(self) -> None:
        with self._lock:
            self.calls += 1
            self.inside += 1

    def _leave(self) -> None:
        with self._lock:
            self.inside -= 1

    def create(self, **kwargs):
        self._enter()
        try:
            if self.gate is not None:
                self.gate.wait(10)
            if self.error is not None:
                raise self.error
            return self._answer(kwargs)
        finally:
            self._leave()

    async def acreate(self, **kwargs):
        self._enter()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return self._answer(kwargs)
        finally:
            self._leave()

    def _answer(self, kwargs):
        if kwargs.get("stream") and self.stream_factory is not None:
            stream = self.stream_factory()
            self.streams.append(stream)
            return stream
        return self.response(kwargs.get("model", MODEL), self.tokens_out)


class AsyncCompletions(Completions):
    async def create(self, **kwargs):  # type: ignore[override]
        return await self.acreate(**kwargs)


def openai_client(completions: Completions):
    client = type("FakeOpenAI", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = completions
    return runbound.wrap(client)


def anthropic_client(messages: Completions):
    client = type("FakeAnthropic", (), {})()
    client.messages = messages
    return runbound.wrap(client)


def call_openai(client, **kwargs):
    return client.chat.completions.create(model=MODEL, messages=SHORT, **kwargs)


def call_anthropic(client, **kwargs):
    return client.messages.create(model=MODEL, messages=SHORT, **kwargs)


PROVIDERS = {
    "openai": (openai_client, call_openai, _Response),
    "anthropic": (anthropic_client, call_anthropic, _AnthropicResponse),
}


# --- provider streams -------------------------------------------------------


class _Chunk:
    def __init__(self, text="", model=MODEL, usage=None):
        self.text = text
        self.model = model
        self.usage = usage


class Stream:
    """Shaped like ``openai.Stream``: iterator, context manager, closeable."""

    def __init__(self, chunks=None, error: BaseException | None = None):
        self._chunks = iter(chunks if chunks is not None else _openai_chunks())
        self._error = error
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._chunks, None)
        if chunk is None:
            if self._error is not None:
                raise self._error
            raise StopIteration
        return chunk

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class AsyncStream:
    """Shaped like ``openai.AsyncStream``."""

    def __init__(self, chunks=None, error: BaseException | None = None):
        self._chunks = list(chunks if chunks is not None else _openai_chunks())
        self._error = error
        self._index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            if self._error is not None:
                raise self._error
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def close(self):
        self.closed = True


def _openai_chunks():
    """Two content chunks and a usage chunk reporting no tokens at all."""
    return [
        _Chunk(text="he"),
        _Chunk(text="llo"),
        _Chunk(usage=_Usage(0)),
    ]


class _Event:
    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


def _anthropic_chunks():
    message = _Event("message", model=MODEL, usage=_AnthropicUsage(0))
    return [
        _Event("message_start", message=message),
        _Event("content_block_delta", delta="hello"),
        _Event("message_stop"),
    ]


def streaming_openai(**kwargs):
    return Completions(stream_factory=lambda: Stream(), **kwargs)


def streaming_anthropic(**kwargs):
    return Completions(
        stream_factory=lambda: Stream(_anthropic_chunks()),
        response=_AnthropicResponse,
        **kwargs,
    )


STREAMS = {
    "openai": (openai_client, call_openai, streaming_openai),
    "anthropic": (anthropic_client, call_anthropic, streaming_anthropic),
}


# --- the Hold itself --------------------------------------------------------


def test_a_hold_raises_the_ledger_and_releasing_it_puts_it_back():
    session = SessionState("s1")
    assert session.reserved == {}
    hold = session.hold("usd", 0.25)
    assert session.reserved["usd"] == pytest.approx(0.25)
    assert (hold.resource, hold.amount) == ("usd", 0.25)
    hold.release()
    assert session.reserved == {}


def test_release_is_idempotent_so_two_close_out_paths_release_once():
    session = SessionState("s1")
    first = session.hold("usd", 1.0)
    session.hold("usd", 1.0)
    first.release()
    first.release()
    first.release()
    assert session.reserved["usd"] == pytest.approx(1.0)  # the second hold, intact


def test_the_ledger_is_floored_and_never_goes_negative():
    session = SessionState("s1")
    hold = session.hold("usd", 1.0)
    session.reserved["usd"] = 0.25  # something else already took it back
    hold.release()
    assert session.reserved.get("usd", 0.0) == 0.0


def test_settle_records_the_delta_for_evidence_and_then_releases():
    session = SessionState("s1")
    hold = session.hold("usd", 1.0)
    hold.settle(0.4)
    assert hold.actual == pytest.approx(0.4)
    assert hold.delta == pytest.approx(-0.6)  # the call cost less than its cap
    assert session.reserved == {}
    assert hold.released is True


def test_settle_releases_the_hold_in_full_whatever_the_call_actually_cost():
    session = SessionState("s1")
    hold = session.hold("usd", 1.0)
    hold.settle(9.0)  # an actual far above the reservation
    assert session.reserved == {}  # the ledger only ever gives back what it took


def test_adjust_is_a_documented_no_op_for_now():
    session = SessionState("s1")
    hold = session.hold("usd", 1.0)
    hold.adjust(0.5)
    assert session.reserved["usd"] == pytest.approx(1.0)
    hold.release()


def test_a_hold_with_no_owner_holds_nothing_and_still_releases():
    hold = Hold("usd", 1.0)
    hold.release()
    hold.settle(0.5)
    assert hold.released is True


def test_a_hold_whose_owner_raises_never_raises_at_the_caller():
    class Broken:
        lock = threading.RLock()

        @property
        def reserved(self):
            raise RuntimeError("no ledger")

    hold = Hold("usd", 1.0, owner=Broken())
    hold.release()  # fail-open: logged, swallowed
    assert hold.released is True


def test_two_resources_are_held_apart():
    session = SessionState("s1")
    session.hold("usd", 1.0)
    session.hold("tokens", 500)
    assert session.reserved == {"usd": 1.0, "tokens": 500}


# --- the admission stage ----------------------------------------------------


def _engine(**kwargs) -> Engine:
    config = GuardrailConfig(custom_prices=PRICES, **kwargs)
    config.validate()
    return Engine(config)


def test_admission_returns_the_hold_it_took():
    engine = _engine(budget_usd=3.0)
    session = SessionState("s1")
    hold = engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    assert hold is not None
    assert session.reserved["usd"] == pytest.approx(W)
    hold.release()


def test_an_uncapped_call_under_capped_takes_no_hold():
    engine = _engine(budget_usd=3.0)
    session = SessionState("s1")
    assert engine.admit(session, "openai", MODEL, {"messages": SHORT}) is None
    assert session.reserved == {}


def test_admission_off_takes_no_hold():
    engine = _engine(budget_usd=3.0, budget_admission=False)
    session = SessionState("s1")
    assert engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1}) is None
    assert session.reserved == {}


def test_what_is_already_reserved_is_subtracted_from_what_is_left():
    engine = _engine(budget_usd=3.0)
    session = SessionState("s1")
    session.hold("usd", 2.5)
    with pytest.raises(GuardrailTripped) as caught:
        engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    details = caught.value.anomaly.details
    assert details["reason"] == "reservation"
    assert details["reserved_usd"] == pytest.approx(2.5)
    assert details["remaining_usd"] == pytest.approx(0.5)  # 3.0 - 0 settled - 2.5 held
    assert details["worst_case_usd"] == pytest.approx(W)


def test_settled_spend_and_a_hold_are_both_subtracted():
    engine = _engine(budget_usd=3.0)
    session = SessionState("s1")
    session.total_cost_usd = 1.0
    session.spend_offset_usd = 0.5
    session.hold("usd", 1.0)
    with pytest.raises(GuardrailTripped) as caught:
        engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    assert caught.value.anomaly.details["remaining_usd"] == pytest.approx(0.5)


def test_the_assumed_cap_refusal_names_what_is_reserved_too():
    engine = _engine(budget_usd=3.0, budget_admission=True, admission_output_tokens=4)
    session = SessionState("s1")
    session.hold("usd", 2.0)
    with pytest.raises(GuardrailTripped) as caught:
        engine.admit(session, "openai", MODEL, {"messages": SHORT})
    details = caught.value.anomaly.details
    assert details["rule"] == "admission"
    assert details["reserved_usd"] == pytest.approx(2.0)
    assert details["remaining_usd"] == pytest.approx(1.0)


def test_a_refused_call_holds_nothing():
    engine = _engine(budget_usd=1.0)
    session = SessionState("s1")
    session.total_cost_usd = 0.5
    with pytest.raises(GuardrailTripped):
        engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    assert session.reserved == {}


def test_an_estimate_exactly_equal_to_what_is_left_is_admitted():
    engine = _engine(budget_usd=W)
    session = SessionState("s1")
    hold = engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    assert hold is not None
    with pytest.raises(GuardrailTripped):  # nothing left now
        engine.admit(session, "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    hold.release()


def test_an_unpriced_model_is_skipped_and_holds_nothing():
    engine = _engine(budget_usd=3.0)
    session = SessionState("s1")
    assert engine.admit(session, "openai", "no-such-model", {"max_tokens": 1}) is None
    assert session.reserved == {}


def test_a_broken_ledger_fails_open_and_the_call_proceeds():
    class Exploding:
        """A session shaped object whose ledger cannot be read at all."""

        lock = threading.RLock()
        total_cost_usd = 0.0
        spend_offset_usd = 0.0

        @property
        def reserved(self):
            raise RuntimeError("no ledger")

    engine = _engine(budget_usd=3.0)
    admitted = engine.admit(Exploding(), "openai", MODEL, {"messages": SHORT, "max_tokens": 1})
    assert admitted is None  # no hold, and no refusal either: the call proceeds


# --- budget(): what is held -------------------------------------------------


def test_budget_reports_what_is_reserved_and_subtracts_it_from_remaining():
    runbound.init(budget_usd=3.0, custom_prices=PRICES)
    view = runbound.budget()
    assert view.reserved == 0.0
    assert view.remaining == pytest.approx(3.0)
    hold = api._SESSION.hold("usd", 1.25)
    held = runbound.budget()
    assert held.reserved == pytest.approx(1.25)
    assert held.remaining == pytest.approx(1.75)
    assert held.spent == 0.0  # a hold is not spend
    hold.release()
    assert_reserved_zero()


def test_reserved_reaches_session_status_too():
    runbound.init(budget_usd=3.0, custom_prices=PRICES)
    with runbound.session("user:1") as session:
        session.hold("usd", 0.5)
        status = runbound.session_status("user:1")["budget"]
        assert status["reserved"] == pytest.approx(0.5)
        assert status["remaining"] == pytest.approx(2.5)
        session.reserved.clear()
    assert_reserved_zero()


def test_remaining_never_goes_below_zero_with_a_hold_outstanding():
    runbound.init(budget_usd=1.0, custom_prices=PRICES, on_anomaly="warn")
    api._SESSION.hold("usd", 5.0)
    assert runbound.budget().remaining == 0.0
    api._SESSION.reserved.clear()


# --- the invariant, under concurrency ---------------------------------------


def test_exactly_three_of_ten_concurrent_capped_calls_are_admitted():
    runbound.init(budget_usd=3.0, custom_prices=PRICES, on_anomaly="raise")
    gate = threading.Event()
    completions = Completions(gate=gate)
    client = openai_client(completions)
    barrier = threading.Barrier(10)
    refusals: list = []
    lock = threading.Lock()

    def worker():
        barrier.wait(10)
        try:
            call_openai(client, max_tokens=1)
        except GuardrailTripped as exc:
            with lock:
                refusals.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    assert until(lambda: len(refusals) == 7 and completions.inside == 3)

    view = runbound.budget()
    assert view.reserved == pytest.approx(3.0)
    assert view.remaining == 0.0
    # INVARIANTS.md invariant 2, while three calls are in flight:
    assert view.spent + view.reserved >= 3 * W

    gate.set()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert completions.calls == 3
    assert len(refusals) == 7
    assert all(exc.anomaly.details["reason"] == "reservation" for exc in refusals)
    assert all(exc.anomaly.details["rule"] == "reservation" for exc in refusals)
    assert runbound.is_tripped() is None  # a reservation refusal never latches
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_a_budget_that_is_not_a_multiple_of_the_worst_case_admits_the_floor():
    runbound.init(budget_usd=3.5, custom_prices=PRICES, on_anomaly="raise")
    gate = threading.Event()
    completions = Completions(gate=gate)
    client = openai_client(completions)
    barrier = threading.Barrier(6)
    refusals: list = []
    lock = threading.Lock()

    def worker():
        barrier.wait(10)
        try:
            call_openai(client, max_tokens=1)
        except GuardrailTripped as exc:
            with lock:
                refusals.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    assert until(lambda: len(refusals) == 3 and completions.inside == 3)
    assert runbound.budget().reserved == pytest.approx(3.0)  # floor(3.5 / 1.0)
    assert runbound.budget().remaining == pytest.approx(0.5)  # left, and unusable
    gate.set()
    for thread in threads:
        thread.join(10)
    assert completions.calls == 3
    assert_reserved_zero()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_the_same_bound_holds_for_concurrent_async_calls(provider):
    build, call, _ = PROVIDERS[provider]
    runbound.init(budget_usd=3.0, custom_prices=PRICES, on_anomaly="raise")

    async def main():
        gate = asyncio.Event()
        completions = AsyncCompletions(gate=gate)
        client = build(completions)

        async def one():
            try:
                await call(client, max_tokens=1)
            except GuardrailTripped as exc:
                return exc
            return None

        pending = asyncio.gather(*[one() for _ in range(10)])
        for _ in range(500):
            if completions.inside == 3:
                break
            await asyncio.sleep(0.005)
        held = runbound.budget().reserved
        gate.set()
        outcomes = await pending
        return completions, held, outcomes

    completions, held, outcomes = asyncio.run(main())
    assert held == pytest.approx(3.0)
    assert completions.calls == 3
    refusals = [exc for exc in outcomes if exc is not None]
    assert len(refusals) == 7
    assert all(exc.anomaly.details["reason"] == "reservation" for exc in refusals)
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


# --- streams hold for as long as they are open ------------------------------


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_an_open_stream_holds_the_money_a_second_call_needs(provider):
    build, call, streaming = STREAMS[provider]
    runbound.init(budget_usd=W, custom_prices=PRICES, on_anomaly="raise")
    completions = streaming()
    client = build(completions)

    stream = call(client, max_tokens=1, stream=True)
    assert runbound.budget().reserved == pytest.approx(W)
    with pytest.raises(GuardrailTripped) as caught:
        call(client, max_tokens=1)
    assert caught.value.anomaly.details["reason"] == "reservation"
    assert completions.calls == 1  # the second call never reached the provider

    list(stream)  # exhausting it frees the hold
    assert runbound.budget().reserved == 0.0
    call(client, max_tokens=1)  # and now the same call fits
    assert completions.calls == 2
    assert_reserved_zero()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_an_open_async_stream_holds_the_money_too(provider):
    build, call, _ = STREAMS[provider]
    runbound.init(budget_usd=W, custom_prices=PRICES, on_anomaly="raise")

    async def main():
        chunks = _openai_chunks() if provider == "openai" else _anthropic_chunks()
        completions = AsyncCompletions(
            stream_factory=lambda: AsyncStream(chunks),
            response=_Response if provider == "openai" else _AnthropicResponse,
        )
        client = build(completions)
        stream = await call(client, max_tokens=1, stream=True)
        open_hold = runbound.budget().reserved
        with pytest.raises(GuardrailTripped):
            await call(client, max_tokens=1)
        async for _ in stream:
            pass
        return completions, open_hold

    completions, open_hold = asyncio.run(main())
    assert open_hold == pytest.approx(W)
    assert completions.calls == 1
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_closing_a_stream_early_releases_its_hold():
    runbound.init(budget_usd=2.0, custom_prices=PRICES, on_anomaly="raise")
    client = openai_client(streaming_openai())
    stream = call_openai(client, max_tokens=1, stream=True)
    next(iter(stream))
    assert runbound.budget().reserved == pytest.approx(W)
    stream.close()
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_a_stream_that_dies_mid_flight_releases_its_hold():
    runbound.init(budget_usd=2.0, custom_prices=PRICES, on_anomaly="raise")
    boom = RuntimeError("the provider dropped the stream")
    completions = Completions(stream_factory=lambda: Stream(_openai_chunks()[:1], error=boom))
    client = openai_client(completions)
    stream = call_openai(client, max_tokens=1, stream=True)
    with pytest.raises(RuntimeError):
        list(stream)
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_a_failed_then_closed_stream_releases_its_hold_exactly_once():
    runbound.init(budget_usd=2.0, custom_prices=PRICES, on_anomaly="raise")
    boom = RuntimeError("the provider dropped the stream")
    completions = Completions(stream_factory=lambda: Stream(_openai_chunks()[:1], error=boom))
    client = openai_client(completions)
    other = api._SESSION.hold("usd", W)  # a second call's money, held alongside

    stream = call_openai(client, max_tokens=1, stream=True)
    assert runbound.budget().reserved == pytest.approx(2 * W)
    with pytest.raises(RuntimeError):
        list(stream)
    stream.close()  # the second close-out path for the same stream
    # Exactly one hold was given back: a double release would show up here as
    # 0.0 instead, having taken the other call's money with it.
    assert runbound.budget().reserved == pytest.approx(W)
    other.release()
    assert_reserved_zero()


def test_an_abandoned_stream_releases_its_hold_when_it_is_collected():
    runbound.init(budget_usd=2.0, custom_prices=PRICES, on_anomaly="raise")
    client = openai_client(streaming_openai())
    other = api._SESSION.hold("usd", W)

    stream = call_openai(client, max_tokens=1, stream=True)
    next(iter(stream))
    assert runbound.budget().reserved == pytest.approx(2 * W)
    del stream
    gc.collect()
    assert runbound.budget().reserved == pytest.approx(W)  # once, not twice
    other.release()
    assert_reserved_zero()


# --- every other close-out --------------------------------------------------


def test_a_provider_error_releases_the_hold_in_full():
    runbound.init(budget_usd=2.0, custom_prices=PRICES, on_anomaly="raise")
    completions = Completions(error=RuntimeError("provider down"))
    client = openai_client(completions)
    with pytest.raises(RuntimeError):
        call_openai(client, max_tokens=1)
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_a_post_call_trip_still_releases_the_hold():
    runbound.init(budget_usd=2.0, custom_prices=PRICES, on_anomaly="raise")
    completions = Completions(tokens_out=5)  # $5.00 actually spent: over the wall
    client = openai_client(completions)
    with pytest.raises(GuardrailTripped) as caught:
        call_openai(client, max_tokens=1)
    assert caught.value.anomaly.details["limit_hit"] == "budget_usd"
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_an_in_flight_refusal_after_the_money_was_admitted_gives_the_hold_back():
    runbound.init(
        budget_usd=5.0, custom_prices=PRICES, max_inflight_calls=1, on_anomaly="raise"
    )
    completions = streaming_openai()
    client = openai_client(completions)
    stream = call_openai(client, max_tokens=1, stream=True)  # holds money and the slot
    assert runbound.budget().reserved == pytest.approx(W)

    with pytest.raises(GuardrailTripped) as caught:
        call_openai(client, max_tokens=1)
    assert caught.value.anomaly.details["limit"] == 1  # the in-flight cap, not money
    # The refused call's money was admitted first and must not be left behind:
    assert runbound.budget().reserved == pytest.approx(W)

    list(stream)
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_the_llm_decorator_holds_its_assumed_cap_for_the_life_of_the_body():
    runbound.init(
        budget_usd=3.0,
        budget_admission=True,
        admission_output_tokens=2,
        custom_prices=PRICES,
        on_anomaly="raise",
    )
    seen: list = []

    @runbound.llm(model=MODEL)
    def generate():
        seen.append(runbound.budget().reserved)
        return "hello"

    generate()
    assert seen == [pytest.approx(2.0)]  # 2 tokens at $1.00 each
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_the_llm_decorator_releases_its_hold_when_the_body_raises():
    runbound.init(
        budget_usd=3.0,
        budget_admission=True,
        admission_output_tokens=2,
        custom_prices=PRICES,
        on_anomaly="raise",
    )

    @runbound.llm(model=MODEL)
    def generate():
        raise RuntimeError("inference failed")

    with pytest.raises(RuntimeError):
        generate()
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_an_async_llm_decorator_holds_and_releases_too():
    runbound.init(
        budget_usd=3.0,
        budget_admission=True,
        admission_output_tokens=2,
        custom_prices=PRICES,
        on_anomaly="raise",
    )
    seen: list = []

    @runbound.llm(model=MODEL)
    async def generate():
        seen.append(runbound.budget().reserved)
        return "hello"

    asyncio.run(generate())
    assert seen == [pytest.approx(2.0)]
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_record_call_never_holds_anything():
    runbound.init(budget_usd=3.0, custom_prices=PRICES, on_anomaly="warn")
    runbound.record_call(MODEL, 0, 1)  # $1.00, recorded after the fact
    assert api._SESSION.reserved == {}
    assert runbound.budget().spent == pytest.approx(1.0)
    assert runbound.budget().reserved == 0.0
    assert_reserved_zero()


def test_an_estimate_equal_to_what_is_left_goes_out():
    runbound.init(budget_usd=W, custom_prices=PRICES, on_anomaly="raise")
    completions = Completions()
    client = openai_client(completions)
    call_openai(client, max_tokens=1)  # estimate == remaining, exactly
    assert completions.calls == 1
    assert_reserved_zero()


def test_a_hold_whose_release_raises_is_logged_and_the_response_still_returns(
    monkeypatch, caplog
):
    runbound.init(budget_usd=3.0, custom_prices=PRICES, on_anomaly="raise")
    completions = Completions()
    client = openai_client(completions)

    def boom(self):
        raise RuntimeError("the ledger is broken")

    monkeypatch.setattr(Hold, "release", boom)
    monkeypatch.setattr(Hold, "settle", boom)
    with caplog.at_level("WARNING", logger="runbound"):
        response = call_openai(client, max_tokens=1)
    assert response is not None  # the host's call is never failed by our bug
    assert completions.calls == 1


def test_a_keyed_session_holds_its_own_money():
    runbound.init(budget_usd=W, custom_prices=PRICES, on_anomaly="raise")
    client = openai_client(streaming_openai())
    with runbound.session("user:1"):
        stream = call_openai(client, max_tokens=1, stream=True)
        assert runbound.budget("user:1").reserved == pytest.approx(W)
        with pytest.raises(GuardrailTripped):
            call_openai(client, max_tokens=1)
        list(stream)
    with runbound.session("user:2"):  # a different session, its own budget
        call_openai(client, max_tokens=1)
    assert_reserved_zero()


def test_nothing_is_held_when_admission_is_off():
    runbound.init(
        budget_usd=W, custom_prices=PRICES, budget_admission=False, on_anomaly="raise"
    )
    completions = streaming_openai()
    client = openai_client(completions)
    stream = call_openai(client, max_tokens=1, stream=True)
    assert runbound.budget().reserved == 0.0
    call_openai(client, max_tokens=1)  # nothing holds anything back
    assert completions.calls == 2
    list(stream)
    assert_reserved_zero()


def test_a_session_with_no_budget_holds_nothing():
    runbound.init(custom_prices=PRICES)
    completions = Completions()
    client = openai_client(completions)
    call_openai(client, max_tokens=1)
    assert api._SESSION.reserved == {}
    assert runbound.budget() is None
