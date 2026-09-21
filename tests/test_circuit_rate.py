"""Rate mode, half-open probes, ``prevented`` and ``retry_after``.

Pure unit tests against :class:`~runbound.circuit.CircuitBreaker` directly —
no engine, no session — with an injected, hand-moved clock, exactly like
``test_circuit.py``. Count mode's own tests are untouched elsewhere; this
file is entirely about the behavior ``mode="rate"`` and the two mode-agnostic
additions (``half_open_calls``, ``prevented``, ``retry_after``) add on top.
"""

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


def rate_breaker(clock: FakeClock, **kwargs) -> CircuitBreaker:
    defaults = dict(
        failure_threshold=999,  # never reached; rate mode ignores it
        window_seconds=60.0,
        cooldown_seconds=30.0,
        now=clock,
        mode="rate",
        min_calls=5,
        failure_rate=0.4,
        slow_rate=0.5,
    )
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)


# --- min_calls: the boundary -------------------------------------------------


def test_rate_mode_ignores_failures_below_min_calls(clock):
    breaker = rate_breaker(clock, min_calls=5)

    for _ in range(4):
        assert breaker.record_failure("openai") is False

    assert breaker.state("openai") == "closed"


def test_exactly_min_calls_counts_one_fewer_does_not(clock):
    """4 failing calls (below min_calls=5) never opens it; the 5th does,
    since 5/5 = 100% > the 40% failure_rate line."""
    breaker = rate_breaker(clock, min_calls=5, failure_rate=0.4)

    for _ in range(4):
        breaker.record_failure("openai")
    assert breaker.state("openai") == "closed"

    assert breaker.record_failure("openai") is True
    assert breaker.state("openai") == "open"


# --- failure_rate: the boundary ----------------------------------------------


def test_exactly_at_failure_rate_does_not_open_one_more_does(clock):
    """2 failures out of 5 calls is exactly 40% -- not *above* 40%, so it
    stays closed; a 6th call, failing, makes it 3/6 = 50% > 40%, which opens
    it. The line is strict '>', never '>='."""
    breaker = rate_breaker(clock, min_calls=5, failure_rate=0.4)

    assert breaker.record_success("openai") is False
    assert breaker.record_success("openai") is False
    assert breaker.record_success("openai") is False
    assert breaker.record_failure("openai") is False
    assert breaker.record_failure("openai") is False  # 2/5 = 40%, not > 40%
    assert breaker.state("openai") == "closed"

    assert breaker.record_failure("openai") is True  # 3/6 = 50% > 40%
    assert breaker.state("openai") == "open"


# --- slow calls: opening with zero errors ------------------------------------


def test_a_window_of_all_slow_zero_error_calls_opens(clock):
    """Every one of these calls *succeeded*; none is a failure. The circuit
    still opens, on the slow-call rate alone."""
    breaker = rate_breaker(clock, min_calls=3, slow_rate=0.5)

    assert breaker.record_success("openai", slow=True) is False
    assert breaker.record_success("openai", slow=True) is False
    assert breaker.record_success("openai", slow=True) is True

    assert breaker.state("openai") == "open"


def test_exactly_at_slow_rate_does_not_open_one_more_does(clock):
    breaker = rate_breaker(clock, min_calls=4, slow_rate=0.5)

    assert breaker.record_success("openai", slow=True) is False
    assert breaker.record_success("openai", slow=True) is False
    assert breaker.record_success("openai", slow=False) is False
    assert breaker.record_success("openai", slow=False) is False  # 2/4 = 50%, not > 50%
    assert breaker.state("openai") == "closed"

    assert breaker.record_success("openai", slow=True) is True  # 3/5 = 60% > 50%
    assert breaker.state("openai") == "open"


def test_a_mostly_healthy_window_with_one_slow_call_stays_closed(clock):
    breaker = rate_breaker(clock, min_calls=5, failure_rate=0.4, slow_rate=0.5)

    for _ in range(4):
        breaker.record_success("openai", slow=False)
    assert breaker.record_success("openai", slow=True) is False
    assert breaker.state("openai") == "closed"


# --- the rolling window itself ------------------------------------------------


def test_calls_older_than_the_window_do_not_count_toward_the_rate(clock):
    breaker = rate_breaker(clock, window_seconds=60.0, min_calls=3, failure_rate=0.4)

    breaker.record_failure("openai")
    breaker.record_failure("openai")
    clock.advance(61.0)  # both age out

    # A 3rd failure lands alone in the window -- 1 call, below min_calls.
    assert breaker.record_failure("openai") is False
    assert breaker.state("openai") == "closed"


def test_an_ordinary_success_in_rate_mode_does_not_reset_the_window(clock):
    """Unlike count mode, one success does not wipe out failures already in
    the window -- the whole point of a rate is tolerating some of them."""
    breaker = rate_breaker(clock, min_calls=3, failure_rate=0.4)

    breaker.record_failure("openai")
    breaker.record_success("openai")
    assert breaker.state("openai") == "closed"  # only 2 calls so far

    assert breaker.record_failure("openai") is True  # 2/3 = 67% > 40%
    assert breaker.state("openai") == "open"


# --- half-open: N probes, the (N+1)th refused --------------------------------


def test_two_half_open_calls_are_admitted_the_third_is_refused(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0,
        now=clock, half_open_calls=2,
    )
    breaker.record_failure("openai")
    clock.advance(31.0)

    assert breaker.state("openai") == "half_open"
    assert breaker.allow("openai") is True   # probe 1
    assert breaker.allow("openai") is True   # probe 2
    assert breaker.allow("openai") is False  # refused


def test_the_default_half_open_calls_is_still_exactly_one(clock):
    """No half_open_calls given -- byte-for-byte the old single-probe rule."""
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.record_failure("openai")
    clock.advance(31.0)

    assert breaker.allow("openai") is True
    assert breaker.allow("openai") is False


# --- prevented: refused calls, per label -------------------------------------


def test_prevented_counts_calls_actually_turned_away(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.record_failure("openai")

    assert breaker.allow("openai") is False  # still cooling down
    assert breaker.allow("openai") is False
    assert breaker.snapshot()["openai"]["prevented"] == 2

    clock.advance(31.0)
    assert breaker.allow("openai") is True  # the probe: not prevented
    assert breaker.allow("openai") is False  # someone else, refused
    assert breaker.snapshot()["openai"]["prevented"] == 3


def test_prevented_resets_when_the_breaker_fully_closes(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.record_failure("openai")
    breaker.allow("openai")
    clock.advance(31.0)
    breaker.allow("openai")

    breaker.record_success("openai")

    assert breaker.snapshot()["openai"]["prevented"] == 0


def test_an_untouched_key_has_prevented_zero():
    breaker = CircuitBreaker(failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0)
    assert breaker.allow("openai") is True
    assert breaker.snapshot() == {}


# --- retry_after: derived from the clock, not from when someone asked -------


def test_retry_after_is_none_while_closed(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    assert breaker.retry_after("openai") is None


def test_retry_after_counts_down_on_two_separate_reads(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.record_failure("openai")

    first = breaker.retry_after("openai")
    clock.advance(10.0)
    second = breaker.retry_after("openai")

    assert first == pytest.approx(30.0)
    assert second == pytest.approx(20.0)
    assert first != second


def test_retry_after_floors_at_zero_past_the_cooldown(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.record_failure("openai")
    clock.advance(90.0)

    assert breaker.retry_after("openai") == 0.0


def test_retry_after_follows_a_forced_holds_own_cooldown(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.force_open("openai", 100.0)

    assert breaker.retry_after("openai") == pytest.approx(100.0)
    clock.advance(40.0)
    assert breaker.retry_after("openai") == pytest.approx(60.0)


def test_retry_after_is_none_again_once_closed(clock):
    breaker = CircuitBreaker(
        failure_threshold=1, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    breaker.record_failure("openai")
    breaker.record_success("openai")

    assert breaker.retry_after("openai") is None


# --- count mode: untouched by any of the above -------------------------------


def test_count_mode_is_the_default_and_ignores_the_rate_knobs(clock):
    """Constructing with no mode= at all behaves exactly as before rate mode existed:
    a single failure_threshold-th failure opens it, min_calls/failure_rate/
    slow_rate never enter into it."""
    breaker = CircuitBreaker(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    assert breaker.mode == "count"

    assert breaker.record_failure("openai") is False
    assert breaker.record_failure("openai") is False
    assert breaker.record_failure("openai") is True
    assert breaker.state("openai") == "open"


def test_count_mode_success_always_returns_false(clock):
    breaker = CircuitBreaker(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, now=clock,
    )
    assert breaker.record_success("openai") is False
    breaker.record_failure("openai")
    assert breaker.record_success("openai") is False
