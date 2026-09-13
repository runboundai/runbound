"""The circuit reading what the provider already told it (T144).

Two layers. The breaker's own ``note_quota`` — pure, clock-injected, and
above all *quiet*: a quota it could not read says nothing, because an agent
behind a proxy that strips rate-limit headers must keep calling a provider
that is answering perfectly. Then the engine, where an error's headers set a
429's cooldown from its own ``Retry-After`` instead of from the configured
one, and where the whole thing stays off until the customer opts in.

**Every 429 in this file is synthetic.** Wave G forbids provoking a rate
limit against a real API, so no recorded fixture contains one; the headers
below are hand-built from the two providers' documented names and the HTTP
specification. The recorded traffic that *does* exist — a plain 200 and a 404
— is exercised in the conformance kit's ``test_quota_fixtures.py``.
"""

import threading

import pytest

from runbound.circuit import CircuitBreaker
from runbound.config import GuardrailConfig
from runbound.engine import CIRCUIT_DETECTOR, Engine
from runbound.quota import MAX_COOLDOWN_S
from runbound.state import SessionState


class FakeClock:
    """A monotonic clock moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, now=clock
    )


# --- note_quota: what a spent bucket does -----------------------------------


def test_nothing_left_opens_the_circuit_until_the_reset(breaker, clock):
    assert breaker.note_quota("openai", 0, 30.0) is True

    assert breaker.state("openai") == "open"
    assert breaker.allow("openai") is False

    clock.advance(29.0)
    assert breaker.state("openai") == "open"
    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_only_the_call_that_opened_it_says_so(breaker):
    """The caller alerts on True, so exactly one alert per opening."""
    assert breaker.note_quota("openai", 0, 30.0) is True
    assert breaker.note_quota("openai", 0, 30.0) is False


def test_a_second_reading_neither_extends_nor_shortens_the_opening(breaker, clock):
    breaker.note_quota("openai", 0, 30.0)
    clock.advance(10.0)

    breaker.note_quota("openai", 0, 600.0)  # a longer reset
    breaker.note_quota("openai", 0, 1.0)  # and a shorter one

    clock.advance(19.0)  # 29s into the original 30
    assert breaker.state("openai") == "open"
    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_an_unreadable_quota_says_nothing_at_all(breaker):
    """The most important test here: a proxy that strips headers is silent.

    ``None`` is not zero. An agent behind a gateway that drops
    ``anthropic-ratelimit-*`` would otherwise stop calling a provider that is
    answering every request perfectly.
    """
    assert breaker.note_quota("openai", None, 30.0) is False

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True
    assert breaker.snapshot() == {}


def test_an_unreadable_quota_is_silent_even_with_no_reset_either(breaker):
    assert breaker.note_quota("openai", None, None) is False

    assert breaker.state("openai") == "closed"
    assert breaker.snapshot() == {}


def test_quota_left_is_good_news_and_does_nothing(breaker):
    assert breaker.note_quota("openai", 5, 30.0) is False

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True


def test_quota_left_never_closes_a_circuit_that_is_open(breaker, clock):
    for _ in range(3):
        breaker.record_failure("openai")
    assert breaker.state("openai") == "open"

    breaker.note_quota("openai", 5_000, 30.0)

    assert breaker.state("openai") == "open"


def test_no_reset_falls_back_to_the_configured_cooldown(breaker, clock):
    breaker.note_quota("openai", 0, None)

    clock.advance(29.0)
    assert breaker.state("openai") == "open"
    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


@pytest.mark.parametrize("reset_s", [0.0, -5.0])
def test_a_non_positive_reset_falls_back_to_the_configured_cooldown(breaker, clock, reset_s):
    breaker.note_quota("openai", 0, reset_s)

    clock.advance(29.0)
    assert breaker.state("openai") == "open"
    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_an_absurd_reset_is_capped_at_an_hour(breaker, clock):
    breaker.note_quota("openai", 0, 999_999.0)

    clock.advance(MAX_COOLDOWN_S - 1.0)
    assert breaker.state("openai") == "open"
    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_a_quota_opened_circuit_heals_exactly_like_a_failure_opened_one(breaker, clock):
    breaker.note_quota("openai", 0, 30.0)
    clock.advance(31.0)

    assert breaker.allow("openai") is True  # the one probe
    assert breaker.allow("openai") is False

    breaker.record_success("openai")

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True
    assert breaker.snapshot()["openai"]["until_s_remaining"] == 0.0


def test_a_failed_probe_re_opens_a_quota_opened_circuit(breaker, clock):
    breaker.note_quota("openai", 0, 30.0)
    clock.advance(31.0)
    breaker.allow("openai")

    breaker.record_failure("openai")

    assert breaker.state("openai") == "open"


def test_it_never_counts_a_failure(breaker):
    """Reading a header is not a failed call: the window is left alone."""
    breaker.record_failure("openai")
    breaker.record_failure("openai")

    breaker.note_quota("openai", 5, 30.0)
    breaker.note_quota("openai", None, 30.0)

    assert breaker.record_failure("openai") is True  # the third, as configured


def test_an_opening_leaves_the_failure_window_where_it_found_it(breaker):
    """The window belongs to ``record_failure``; a header must not edit it."""
    breaker.record_failure("openai")

    breaker.note_quota("openai", 0, 30.0)

    assert breaker.snapshot()["openai"]["failures"] == 1


def test_one_providers_quota_leaves_the_others_alone(breaker):
    breaker.note_quota("openai", 0, 30.0)

    assert breaker.state("anthropic") == "closed"
    assert breaker.allow("anthropic") is True


def test_a_quota_opening_shows_up_in_the_snapshot(breaker):
    breaker.note_quota("openai", 0, 45.0)

    entry = breaker.snapshot()["openai"]

    assert entry["state"] == "open"
    assert entry["until_s_remaining"] == pytest.approx(45.0)


def test_a_remaining_that_is_not_a_number_says_nothing(breaker):
    """``remaining`` is an ``int | None``; anything else is a caller's bug and
    is answered the way an unreadable header is."""
    assert breaker.note_quota("openai", "0", 30.0) is False
    assert breaker.note_quota("openai", object(), 30.0) is False
    assert breaker.note_quota("openai", True, 30.0) is False

    assert breaker.state("openai") == "closed"


def test_an_unreadable_reset_falls_back_to_the_configured_cooldown(breaker, clock):
    assert breaker.note_quota("openai", 0, "soon") is True

    clock.advance(29.0)
    assert breaker.state("openai") == "open"
    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_forcing_it_closed_releases_a_quota_opening(breaker):
    breaker.note_quota("openai", 0, 3_000.0)

    breaker.force_close("openai")

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True


def test_many_threads_reading_quota_and_failing_leave_a_consistent_snapshot(clock):
    breaker = CircuitBreaker(
        failure_threshold=5, window_seconds=60.0, cooldown_seconds=30.0, now=clock
    )
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def hammer(index: int) -> None:
        key = f"p{index % 3}"
        try:
            start.wait()
            for _ in range(50):
                breaker.note_quota(key, 0, 30.0)
                breaker.record_failure(key)
                breaker.note_quota(key, None, 30.0)
                breaker.allow(key)
                breaker.snapshot()
                breaker.note_quota(key, 7, 30.0)
                breaker.force_close(key)
        except BaseException as exc:  # pragma: no cover - a failure is the report
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    snapshot = breaker.snapshot()
    assert set(snapshot) == {"p0", "p1", "p2"}
    for entry in snapshot.values():
        assert entry["state"] in ("closed", "open", "half_open")
        assert entry["until_s_remaining"] >= 0.0
        assert entry["failures"] >= 0


# --- the engine: an error's headers, opt-in ---------------------------------


class RecordingObserver:
    def __init__(self) -> None:
        self.sent: list = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


class Response:
    """The ``.response`` an ``APIStatusError`` carries, headers and all."""

    def __init__(self, headers: dict) -> None:
        self.headers = dict(headers)


class RateLimited(Exception):
    """A synthetic 429. No recorded one exists: Wave G forbids provoking it.

    Shaped like ``anthropic.RateLimitError``, which is verified to expose
    ``.status_code`` and ``.response.headers`` and *not* a direct ``.headers``.
    """

    def __init__(self, headers: dict) -> None:
        super().__init__("rate limited")
        self.status_code = 429
        self.response = Response(headers)


class NotFound(Exception):
    """The one error shape that really was recorded: a 404, no quota headers."""

    def __init__(self) -> None:
        super().__init__("model not found")
        self.status_code = 404
        self.response = Response({"content-type": "application/json"})


#: A synthetic 429's headers: spent requests bucket, and the provider's own
#: statement of when to come back.
SPENT_429 = {
    "retry-after": "7",
    "anthropic-ratelimit-requests-limit": "10000",
    "anthropic-ratelimit-requests-remaining": "0",
    "anthropic-ratelimit-requests-reset": "600s",
    "anthropic-ratelimit-tokens-remaining": "12000000",
    "anthropic-ratelimit-tokens-reset": "600s",
}

#: The same 429, but with quota to spare — the provider is throttling us for
#: some other reason, so only ``Retry-After`` has anything to say.
THROTTLED_429 = dict(SPENT_429, **{"anthropic-ratelimit-requests-remaining": "5"})


def quota_engine(reads_quota: bool, observer: RecordingObserver, clock) -> Engine:
    engine = Engine(
        GuardrailConfig(
            on_provider_failure="open",
            circuit_failure_threshold=2,
            circuit_reads_quota=reads_quota,
        ),
        observers=[observer],
    )
    engine.circuit._now = clock
    return engine


def circuit_alerts(observer: RecordingObserver) -> list:
    return [a for a in observer.sent if a.detector == CIRCUIT_DETECTOR]


def test_off_by_default():
    assert GuardrailConfig().circuit_reads_quota is False


def test_with_the_option_off_a_spent_429_behaves_exactly_as_it_does_today(clock):
    observer = RecordingObserver()
    engine = quota_engine(False, observer, clock)
    state = SessionState("s1")

    engine.record_llm_error(state, "claude-haiku-4-5", RateLimited(SPENT_429), 0.1, "anthropic")

    # One failure, threshold two: nothing has opened, whatever the headers say.
    assert engine.circuit.state("anthropic") == "closed"
    assert engine.circuit_allows("anthropic") is True
    assert circuit_alerts(observer) == []

    engine.record_llm_error(state, "claude-haiku-4-5", RateLimited(SPENT_429), 0.1, "anthropic")

    # The second failure opens it — for the configured 30s, not the header's 7.
    assert engine.circuit.state("anthropic") == "open"
    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(30.0)

    alert = circuit_alerts(observer)[0]
    assert len(circuit_alerts(observer)) == 1
    assert alert.details["failures"] == 2
    assert alert.details["cooldown_seconds"] == 30.0
    assert alert.details["fault"] == "provider"
    assert alert.details["reason"] == "failures"
    assert alert.message == (
        "Provider 'anthropic' circuit opened: 2 failures in 60s; "
        "cooling down 30s — calls fail fast until it recovers"
    )

    clock.advance(29.0)
    assert engine.circuit.state("anthropic") == "open"
    clock.advance(2.0)
    assert engine.circuit.state("anthropic") == "half_open"


def test_with_it_on_a_spent_429_opens_at_once_for_its_retry_after(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    engine.record_llm_error(state, "claude-haiku-4-5", RateLimited(SPENT_429), 0.1, "anthropic")

    # One failure out of two, and yet: the provider said there is nothing left.
    assert engine.circuit.state("anthropic") == "open"
    assert engine.circuit_allows("anthropic") is False
    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(7.0)

    clock.advance(6.0)
    assert engine.circuit.state("anthropic") == "open"
    clock.advance(2.0)
    assert engine.circuit.state("anthropic") == "half_open"


def test_a_quota_opening_is_reported_as_a_circuit_anomaly_that_says_why(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    engine.record_llm_error(state, "claude-haiku-4-5", RateLimited(SPENT_429), 0.1, "anthropic")

    alerts = circuit_alerts(observer)
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].details["reason"] == "quota"
    assert alerts[0].details["provider"] == "anthropic"
    assert alerts[0].details["state"] == "open"
    assert alerts[0].details["cooldown_seconds"] == pytest.approx(7.0)


def test_a_quota_opening_is_alerted_exactly_once(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    for _ in range(4):
        engine.record_llm_error(
            state, "claude-haiku-4-5", RateLimited(SPENT_429), 0.1, "anthropic"
        )

    assert len(circuit_alerts(observer)) == 1


def test_with_it_on_a_retry_after_sets_the_cooldown_of_a_failure_opening(clock):
    """Quota to spare, so it is the failure threshold that opens it — but the
    provider still said when to come back, and that beats our 30s guess."""
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    engine.record_llm_error(
        state, "claude-haiku-4-5", RateLimited(THROTTLED_429), 0.1, "anthropic"
    )
    assert engine.circuit.state("anthropic") == "closed"

    engine.record_llm_error(
        state, "claude-haiku-4-5", RateLimited(THROTTLED_429), 0.1, "anthropic"
    )

    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(7.0)
    alert = circuit_alerts(observer)[0]
    assert alert.details["reason"] == "failures"
    assert alert.details["cooldown_seconds"] == pytest.approx(7.0)


def test_an_absurd_retry_after_is_capped_at_an_hour(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")
    headers = dict(THROTTLED_429, **{"retry-after": "864000"})  # ten days

    for _ in range(2):
        engine.record_llm_error(
            state, "claude-haiku-4-5", RateLimited(headers), 0.1, "anthropic"
        )

    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(
        MAX_COOLDOWN_S
    )


def test_the_recorded_404_shape_opens_nothing_and_says_nothing(clock):
    """A 404 carries no rate-limit headers at all — and is not even a fault
    the circuit counts."""
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    for _ in range(5):
        engine.record_llm_error(state, "no-such-model", NotFound(), 0.1, "anthropic")

    assert engine.circuit.state("anthropic") == "closed"
    assert circuit_alerts(observer) == []


def test_a_provider_failure_with_no_headers_behaves_as_it_always_did(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    for _ in range(2):
        engine.record_llm_error(state, "claude-haiku-4-5", TimeoutError("slow"), 0.1, "anthropic")

    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(30.0)
    assert circuit_alerts(observer)[0].details["reason"] == "failures"


def test_headers_that_explode_when_read_never_reach_the_host(clock):
    class Hostile(Exception):
        status_code = 429

        @property
        def response(self):  # pragma: no cover - raising is the point
            raise RuntimeError("no")

    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")

    for _ in range(2):
        engine.record_llm_error(state, "claude-haiku-4-5", Hostile(), 0.1, "anthropic")

    assert engine.circuit.state("anthropic") == "open"
    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(30.0)


def test_no_header_text_ever_reaches_an_anomaly(clock):
    """Only derived numbers leave the process — never a header's value."""
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)
    state = SessionState("s1")
    headers = dict(SPENT_429, **{"request-id": "req_secret", "cf-ray": "a3aa-BLR"})

    engine.record_llm_error(state, "claude-haiku-4-5", RateLimited(headers), 0.1, "anthropic")

    alert = circuit_alerts(observer)[0]
    blob = repr(alert.details) + alert.message
    assert "req_secret" not in blob
    assert "a3aa-BLR" not in blob
    assert "anthropic-ratelimit" not in blob
    assert "retry-after" not in blob.lower()


# --- Engine.note_quota, the success-path door -------------------------------


def test_note_quota_is_a_no_op_unless_the_customer_opted_in(clock):
    observer = RecordingObserver()
    engine = quota_engine(False, observer, clock)

    engine.note_quota("anthropic", {"anthropic-ratelimit-requests-remaining": "0"})

    assert engine.circuit.state("anthropic") == "closed"
    assert circuit_alerts(observer) == []


def test_note_quota_opens_on_a_spent_bucket_from_a_raw_response(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)

    engine.note_quota(
        "anthropic",
        {
            "anthropic-ratelimit-requests-remaining": "0",
            "anthropic-ratelimit-requests-reset": "45s",
        },
    )

    assert engine.circuit.state("anthropic") == "open"
    assert engine.circuit.snapshot()["anthropic"]["until_s_remaining"] == pytest.approx(45.0)
    assert circuit_alerts(observer)[0].details["reason"] == "quota"


def test_note_quota_says_nothing_when_the_headers_are_unreadable(clock):
    observer = RecordingObserver()
    engine = quota_engine(True, observer, clock)

    for headers in (None, {}, {"content-type": "application/json"}, "garbage", object()):
        engine.note_quota("anthropic", headers)

    assert engine.circuit.state("anthropic") == "closed"
    assert circuit_alerts(observer) == []


def test_note_quota_never_raises_even_when_the_breaker_is_broken(clock):
    class Broken:
        def note_quota(self, key, remaining, reset_s):
            raise RuntimeError("boom")

    engine = quota_engine(True, RecordingObserver(), clock)
    engine.circuit = Broken()

    engine.note_quota("anthropic", {"anthropic-ratelimit-requests-remaining": "0"})


# --- the wrappers' side of the door -----------------------------------------


class RecordingHooks:
    """Everything a wrapper's ``_finish`` calls, recorded."""

    estimate_tokens = False

    def __init__(self) -> None:
        self.quotas: list = []

    def success(self, provider) -> None:
        pass

    def tool_request(self, name, args_hash) -> None:
        pass

    def quota(self, provider, headers) -> None:
        self.quotas.append((provider, headers))


class Parsed:
    """What both SDKs actually return: a parsed model, no headers on it."""

    usage = None


class RawResponse:
    """What ``with_raw_response`` / ``.parse()`` leaves the customer holding."""

    usage = None

    def __init__(self, headers: dict) -> None:
        self.headers = dict(headers)


def finishers():
    """Both wrappers' ``_finish``, each already given its surface."""
    import functools

    from runbound.wrappers import anthropic_wrapper, openai_wrapper

    return [
        ("anthropic", anthropic_wrapper._finish),
        (
            "openai",
            functools.partial(openai_wrapper._finish, surface=openai_wrapper._CHAT_SURFACE),
        ),
    ]


@pytest.mark.parametrize("name, finish", finishers())
def test_an_ordinary_response_never_reaches_the_quota_hook(name, finish):
    """The whole cost on the common path is one attribute lookup that misses."""
    hooks = RecordingHooks()

    finish(Parsed(), {}, lambda *a: None, 0.0, hooks, name, is_async=False)

    assert hooks.quotas == []


@pytest.mark.parametrize("name, finish", finishers())
def test_a_raw_response_hands_its_headers_over_once(name, finish):
    hooks = RecordingHooks()

    finish(RawResponse(SPENT_429), {}, lambda *a: None, 0.0, hooks, name, is_async=False)

    assert len(hooks.quotas) == 1
    assert hooks.quotas[0][0] == name
    assert hooks.quotas[0][1] == SPENT_429


@pytest.mark.parametrize("name, finish", finishers())
def test_a_broken_quota_hook_never_reaches_the_caller(name, finish):
    class Exploding(RecordingHooks):
        def quota(self, provider, headers):
            raise RuntimeError("boom")

    response = RawResponse(SPENT_429)

    assert (
        finish(response, {}, lambda *a: None, 0.0, Exploding(), name, is_async=False)
        is response
    )


def test_hooks_that_predate_the_quota_hook_are_tolerated():
    from runbound.wrappers import report_quota

    class Ancient:
        pass

    report_quota(Ancient(), "anthropic", RawResponse(SPENT_429))  # must not raise


def test_the_default_no_op_hooks_accept_a_quota_report():
    from runbound.wrappers import _NO_HOOKS, report_quota

    assert _NO_HOOKS.quota("anthropic", SPENT_429) is None
    report_quota(_NO_HOOKS, "anthropic", RawResponse(SPENT_429))
