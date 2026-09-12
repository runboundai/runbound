"""Tests for forcing a circuit open or closed, and for reading the fleet view.

An operator (or the control plane on their behalf) can say "stop calling this
provider" without waiting for the failure threshold, and "resume now" without
waiting out the cooldown. A forced circuit is an ordinary open circuit in every
other respect: it half-opens when its hold expires, one probe decides, and a
success closes it.
"""

import threading

import pytest

from runbound.circuit import CircuitBreaker


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


# --- force_open -------------------------------------------------------------


def test_forcing_it_open_refuses_calls_immediately(breaker):
    breaker.force_open("openai", 120.0)

    assert breaker.state("openai") == "open"
    assert breaker.allow("openai") is False


def test_the_hold_lasts_as_long_as_it_was_asked_to(breaker, clock):
    breaker.force_open("openai", 120.0)

    clock.advance(119.0)
    assert breaker.state("openai") == "open"

    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_no_duration_means_the_configured_cooldown(breaker, clock):
    breaker.force_open("openai", None)

    clock.advance(29.0)
    assert breaker.state("openai") == "open"

    clock.advance(2.0)
    assert breaker.state("openai") == "half_open"


def test_a_zero_length_hold_is_over_at_once(breaker):
    breaker.force_open("openai", 0.0)

    assert breaker.state("openai") == "half_open"


def test_a_forced_circuit_half_opens_like_a_natural_one(breaker, clock):
    breaker.force_open("openai", 10.0)
    clock.advance(11.0)

    assert breaker.allow("openai") is True  # the probe
    assert breaker.allow("openai") is False
    assert breaker.allow("openai") is False


def test_a_successful_probe_closes_a_forced_circuit(breaker, clock):
    breaker.force_open("openai", 10.0)
    clock.advance(11.0)
    breaker.allow("openai")

    breaker.record_success("openai")

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True


def test_a_success_closes_a_forced_circuit_even_before_the_hold_expires(breaker):
    breaker.force_open("openai", 3_600.0)

    breaker.record_success("openai")

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True


def test_a_failure_never_ends_a_long_hold_early(breaker, clock):
    breaker.force_open("openai", 3_600.0)

    breaker.record_failure("openai")

    clock.advance(31.0)  # the natural cooldown has passed; the hold has not
    assert breaker.state("openai") == "open"


def test_a_failure_during_a_short_hold_restarts_the_natural_cooldown(breaker, clock):
    breaker.force_open("openai", 5.0)
    clock.advance(4.0)

    assert breaker.record_failure("openai") is False

    clock.advance(2.0)  # the hold would be over
    assert breaker.state("openai") == "open"
    clock.advance(29.0)
    assert breaker.state("openai") == "half_open"


def test_the_latest_instruction_wins(breaker, clock):
    breaker.force_open("openai", 3_600.0)

    breaker.force_open("openai", 5.0)

    clock.advance(6.0)
    assert breaker.state("openai") == "half_open"


def test_forcing_open_a_key_never_seen_before_works(breaker):
    assert breaker.state("brand-new") == "closed"

    breaker.force_open("brand-new", 10.0)

    assert breaker.state("brand-new") == "open"


def test_forcing_one_key_open_leaves_the_others_alone(breaker):
    breaker.force_open("openai", 10.0)

    assert breaker.allow("anthropic") is True
    assert breaker.state("anthropic") == "closed"


# --- force_close ------------------------------------------------------------


def test_forcing_it_closed_resumes_calls_at_once(breaker):
    for _ in range(3):
        breaker.record_failure("openai")

    breaker.force_close("openai")

    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True
    assert breaker.allow("openai") is True


def test_forcing_it_closed_forgets_the_failure_window(breaker):
    for _ in range(3):
        breaker.record_failure("openai")
    breaker.force_close("openai")

    assert breaker.record_failure("openai") is False
    assert breaker.record_failure("openai") is False
    assert breaker.state("openai") == "closed"

    assert breaker.record_failure("openai") is True  # the third opens it again


def test_forcing_closed_releases_a_forced_hold(breaker, clock):
    breaker.force_open("openai", 3_600.0)

    breaker.force_close("openai")

    clock.advance(1.0)
    assert breaker.state("openai") == "closed"
    assert breaker.allow("openai") is True


def test_forcing_closed_a_key_never_seen_before_is_harmless(breaker):
    breaker.force_close("never-seen")

    assert breaker.state("never-seen") == "closed"
    assert breaker.allow("never-seen") is True


def test_forcing_closed_frees_the_probe(breaker, clock):
    for _ in range(3):
        breaker.record_failure("openai")
    clock.advance(31.0)
    breaker.allow("openai")  # the one probe is taken

    breaker.force_close("openai")

    assert breaker.allow("openai") is True
    assert breaker.allow("openai") is True


# --- snapshot ---------------------------------------------------------------


def test_a_breaker_that_has_seen_nothing_snapshots_empty(breaker):
    assert breaker.snapshot() == {}


def test_a_snapshot_reports_state_remaining_hold_and_failures(breaker, clock):
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    breaker.force_open("anthropic", 100.0)
    clock.advance(40.0)

    snapshot = breaker.snapshot()

    assert set(snapshot) == {"openai", "anthropic"}
    assert snapshot["openai"] == {
        "state": "closed",
        "until_s_remaining": 0.0,
        "failures": 2,
    }
    assert snapshot["anthropic"]["state"] == "open"
    assert snapshot["anthropic"]["until_s_remaining"] == pytest.approx(60.0)
    assert snapshot["anthropic"]["failures"] == 0


def test_a_snapshot_counts_only_failures_still_inside_the_window(breaker, clock):
    breaker.record_failure("openai")
    clock.advance(61.0)
    breaker.record_failure("openai")

    assert breaker.snapshot()["openai"]["failures"] == 1


def test_a_half_open_snapshot_has_no_hold_left(breaker, clock):
    for _ in range(3):
        breaker.record_failure("openai")
    clock.advance(31.0)

    entry = breaker.snapshot()["openai"]

    assert entry["state"] == "half_open"
    assert entry["until_s_remaining"] == 0.0


def test_a_snapshot_is_a_copy_the_caller_may_keep(breaker):
    breaker.force_open("openai", 10.0)

    snapshot = breaker.snapshot()
    snapshot["openai"]["state"] = "tampered"
    snapshot["another"] = {}

    assert breaker.snapshot()["openai"]["state"] == "open"
    assert "another" not in breaker.snapshot()


# --- concurrency ------------------------------------------------------------


def test_forcing_from_many_threads_stays_consistent(clock):
    breaker = CircuitBreaker(
        failure_threshold=5, window_seconds=60.0, cooldown_seconds=30.0, now=clock
    )
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def hammer(index: int) -> None:
        try:
            start.wait()
            for _ in range(50):
                breaker.force_open(f"p{index % 3}", 60.0)
                breaker.allow(f"p{index % 3}")
                breaker.snapshot()
                breaker.force_close(f"p{index % 3}")
                breaker.record_failure(f"p{index % 3}")
                breaker.state(f"p{index % 3}")
        except BaseException as exc:  # pragma: no cover - a failure is the report
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(breaker.snapshot()) == {"p0", "p1", "p2"}

    for key in ("p0", "p1", "p2"):
        breaker.force_close(key)
        assert breaker.state(key) == "closed"
