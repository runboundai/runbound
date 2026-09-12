"""Tests for the provider circuit breaker and its failure classifier.

Pure unit tests: no sessions, no engine, no clock that ticks by itself. The
breaker's notion of time is injected, so every window and cooldown here is
moved by hand.
"""

import threading

import pytest

from runbound.circuit import CircuitBreaker, is_provider_failure


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


# --- is_provider_failure ----------------------------------------------------


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 599])
def test_provider_side_statuses_count_as_provider_failures(status):
    assert is_provider_failure(Failure(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_client_side_statuses_are_our_bug_not_the_providers(status):
    assert is_provider_failure(Failure(status)) is False


def test_an_error_without_a_status_is_a_provider_failure():
    """Timeouts and connection errors carry no status; they are the provider."""
    assert is_provider_failure(TimeoutError("read timed out")) is True
    assert is_provider_failure(Failure()) is True


def test_an_unreadable_status_counts_as_a_provider_failure():
    class Hostile(Exception):
        @property
        def status_code(self):
            raise RuntimeError("boom")

    assert is_provider_failure(Hostile()) is True
    assert is_provider_failure(Failure("not-a-number")) is True


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
