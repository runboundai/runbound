"""Tests for the provider circuit breaker and its failure classifier.

Pure unit tests: no sessions, no engine, no clock that ticks by itself. The
breaker's notion of time is injected, so every window and cooldown here is
moved by hand.
"""

import asyncio
import concurrent.futures
import socket
import ssl
import threading

import pytest

from runbound.circuit import (
    CIRCUIT_FAULTS,
    CircuitBreaker,
    classify_failure,
    is_provider_failure,
)


class FakeClock:
    """A monotonic clock moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Failure(Exception):
    """An SDK-shaped error carrying an HTTP status, like openai.APIError."""

    def __init__(self, status_code=None):
        super().__init__(f"status {status_code}")
        if status_code is not None:
            self.status_code = status_code


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, now=clock
    )


# --- classify_failure -------------------------------------------------------


def fake_exc(name: str, *bases: type) -> type:
    """An exception class with an SDK's class name and none of its package.

    The classifier matches provider SDK errors by name, so a class built here
    is indistinguishable from the real one — which is the point: neither
    ``openai`` nor ``anthropic`` is installed or imported by these tests.
    """
    return type(name, bases or (Exception,), {})


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 599])
def test_provider_side_statuses_count_as_provider_failures(status):
    assert classify_failure(Failure(status)) == "provider"
    assert is_provider_failure(Failure(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_client_side_statuses_are_our_bug_not_the_providers(status):
    assert classify_failure(Failure(status)) == "application"
    assert is_provider_failure(Failure(status)) is False


def test_a_timeout_is_a_provider_failure():
    """Was "no status = provider": now the timeout says it, not the silence."""
    assert classify_failure(TimeoutError("read timed out")) == "provider"
    assert classify_failure(fake_exc("APITimeoutError")()) == "provider"
    assert classify_failure(fake_exc("ReadTimeout")()) == "provider"
    assert is_provider_failure(TimeoutError("read timed out")) is True


def test_a_status_less_error_of_no_known_kind_is_the_application():
    """The other half of the old rule: silence alone no longer accuses anyone."""
    assert classify_failure(Failure()) == "application"
    assert is_provider_failure(Failure()) is False


def test_a_dropped_connection_is_transport_and_still_counts():
    for exc in (
        ConnectionResetError("reset by peer"),
        ConnectionRefusedError("refused"),
        socket.gaierror("name or service not known"),
        ssl.SSLError("handshake failure"),
        OSError("network is unreachable"),
    ):
        assert classify_failure(exc) == "transport", exc
        assert is_provider_failure(exc) is True


def test_an_openai_shaped_connection_error_is_transport():
    """Matched by class name: the package is not installed and never imported."""
    assert classify_failure(fake_exc("APIConnectionError")()) == "transport"
    assert classify_failure(fake_exc("ConnectError")()) == "transport"


def test_a_subclass_is_classified_through_its_bases():
    """anthropic's APIConnectionError subclasses are found by walking the MRO."""
    connection = fake_exc("APIConnectionError")
    assert classify_failure(fake_exc("APIStatusError", connection)()) == "transport"


def test_a_timeout_that_is_also_a_connection_error_is_the_provider():
    """anthropic's APITimeoutError subclasses APIConnectionError; provider wins."""
    timeout = fake_exc("APITimeoutError", fake_exc("APIConnectionError"))
    assert classify_failure(timeout()) == "provider"


def test_the_callers_own_mistakes_are_the_application():
    for exc in (
        TypeError("str + int"),
        ValueError("bad model"),
        KeyError("choices"),
        fake_exc("ValidationError", ValueError)("2 validation errors"),
    ):
        assert classify_failure(exc) == "application", exc
        assert is_provider_failure(exc) is False


def test_a_certificate_error_is_transport_even_though_it_is_a_ValueError():
    """ssl.SSLCertVerificationError inherits both; transport is checked first."""
    exc = ssl.SSLCertVerificationError("certificate verify failed")
    assert isinstance(exc, ValueError)
    assert classify_failure(exc) == "transport"


def test_cancellation_is_not_a_failure_at_all():
    assert classify_failure(asyncio.CancelledError()) == "cancel"
    assert classify_failure(concurrent.futures.CancelledError()) == "cancel"
    assert is_provider_failure(asyncio.CancelledError()) is False


def test_an_unreadable_status_counts_as_a_provider_failure():
    """An exception carrying a status came off an HTTP response, readable or not."""

    class Hostile(Exception):
        @property
        def status_code(self):
            raise RuntimeError("boom")

    assert classify_failure(Hostile()) == "provider"
    assert classify_failure(Failure("not-a-number")) == "provider"
    assert is_provider_failure(Hostile()) is True


def test_classify_never_raises_whatever_it_is_handed():
    """Fail-open: an exotic exception gets a default, never a second exception."""

    class Liar(Exception):
        @property
        def __class__(self):
            raise RuntimeError("boom")

    class Exotic(BaseException):
        pass

    for exc in (Liar(), Exotic(), BaseException(), object(), None):
        assert classify_failure(exc) in {"provider", "transport", "application", "cancel"}


def test_only_provider_and_transport_count_toward_the_circuit():
    assert CIRCUIT_FAULTS == frozenset({"provider", "transport"})


# --- opening ----------------------------------------------------------------


def test_a_closed_breaker_allows_everything(breaker):
    assert breaker.allow("openai") is True
    assert breaker.state("openai") == "closed"


def test_it_opens_exactly_at_the_failure_threshold(breaker):
    assert breaker.record_failure("openai") is False
    assert breaker.record_failure("openai") is False
    assert breaker.state("openai") == "closed"

    assert breaker.record_failure("openai") is True

    assert breaker.state("openai") == "open"
    assert breaker.allow("openai") is False


def test_only_the_opening_failure_reports_that_it_opened(breaker):
    for _ in range(3):
        breaker.record_failure("openai")
    assert breaker.record_failure("openai") is False


def test_failures_older_than_the_window_do_not_count(breaker, clock):
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    clock.advance(61.0)

    assert breaker.record_failure("openai") is False
    assert breaker.state("openai") == "closed"


def test_keys_are_independent(breaker):
    for _ in range(3):
        breaker.record_failure("openai")

    assert breaker.allow("openai") is False
    assert breaker.allow("anthropic") is True
    assert breaker.state("anthropic") == "closed"


# --- half-open --------------------------------------------------------------


def test_the_cooldown_lets_exactly_one_probe_through(breaker, clock):
    for _ in range(3):
        breaker.record_failure("openai")

    clock.advance(29.0)
    assert breaker.allow("openai") is False

    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"
    assert breaker.allow("openai") is True
    assert breaker.allow("openai") is False
    assert breaker.allow("openai") is False


def test_a_successful_probe_closes_the_breaker(breaker, clock):
    for _ in range(3):
        breaker.record_failure("openai")
    clock.advance(31.0)
    breaker.allow("openai")

    breaker.record_success("openai")

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True
    assert breaker.allow("openai") is True


def test_a_failed_probe_re_opens_with_a_fresh_cooldown(breaker, clock):
    for _ in range(3):
        breaker.record_failure("openai")
    clock.advance(31.0)
    breaker.allow("openai")  # the probe

    assert breaker.record_failure("openai") is False  # it was never closed
    assert breaker.state("openai") == "open"
    assert breaker.allow("openai") is False

    clock.advance(31.0)
    assert breaker.state("openai") == "half_open"
    assert breaker.allow("openai") is True


def test_success_on_a_closed_breaker_clears_the_failure_count(breaker):
    breaker.record_failure("openai")
    breaker.record_failure("openai")

    breaker.record_success("openai")

    assert breaker.record_failure("openai") is False
    assert breaker.state("openai") == "closed"


def test_an_unknown_key_reads_closed(breaker):
    assert breaker.state("never-seen") == "closed"
    assert breaker.allow("never-seen") is True


# --- concurrency ------------------------------------------------------------


def test_concurrent_failures_open_it_once_and_stay_consistent(clock):
    breaker = CircuitBreaker(
        failure_threshold=5, window_seconds=60.0, cooldown_seconds=30.0, now=clock
    )
    opened: list[bool] = []
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def hammer() -> None:
        try:
            start.wait()
            for _ in range(25):
                opened.append(breaker.record_failure("openai"))
                breaker.state("openai")
                breaker.allow("openai")
        except BaseException as exc:  # pragma: no cover - a failure is the report
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(opened) == 1  # exactly one caller was told it opened it
    assert breaker.state("openai") == "open"
