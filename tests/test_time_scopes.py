"""Integration tests for T133: ``max_session_seconds`` measures the *run*,
not the end-user's whole history with the key.

Before this change, ``SessionState.started_at`` was set once, at creation,
and never touched again — so a keyed chatbot session's wall clock counted
from the user's very first message ever, and a user who came back the next
day tripped the timeout on their next word. These tests exercise the fix
through the public ``runbound.session()`` entry point, with a hand-driven
monotonic clock, rather than at the detector unit level (see
``test_timeout_costcap.py`` for that).
"""

import pytest

import runbound
from runbound import api
from runbound import state as state_module
from runbound.exceptions import GuardrailTripped

HOUR = 3600.0


class FakeClock:
    """Stands in for ``api``'s ``time`` module; moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture()
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    # Both modules keep their own monotonic reference: api.time for event
    # timestamps and the run-clock reset, state.time for a SessionState's
    # own started_at/run_started_at set at construction. Both must move
    # together or the two clocks drift apart from the test's point of view.
    monkeypatch.setattr(api, "time", fake)
    monkeypatch.setattr(state_module, "time", fake)
    return fake


def _do_work(cost: float = 0.0) -> None:
    runbound.record_call("gpt-4o", tokens_in=1, tokens_out=1, duration_s=0.0)


def test_a_keyed_session_entered_three_times_over_70_minutes_never_trips(clock):
    runbound.init(max_session_seconds=HOUR, on_anomaly="raise")

    for _ in range(3):
        with runbound.session("user:1"):
            _do_work()
        clock.advance(70 * 60 / 3)  # spread 70 minutes across the three gaps

    assert runbound.is_tripped("user:1") is None


def test_a_single_block_running_3601_seconds_trips(clock):
    runbound.init(max_session_seconds=HOUR, on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("user:1"):
            _do_work()
            clock.advance(3601.0)
            _do_work()

    assert excinfo.value.anomaly.detector == "timeout"
    assert excinfo.value.anomaly.details["scope"] == "run"


def test_lifetime_cap_trips_only_when_set(clock):
    """Three short entries spread over more than an hour: max_session_seconds
    never trips (the run clock resets each time), but max_session_lifetime_seconds
    — measuring since the key's first-ever entry — does."""
    runbound.init(
        max_session_seconds=HOUR,
        max_session_lifetime_seconds=HOUR,
        on_anomaly="raise",
    )

    with runbound.session("user:1"):
        _do_work()
    clock.advance(HOUR + 1.0)

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("user:1"):
            _do_work()

    assert excinfo.value.anomaly.detector == "timeout"
    assert excinfo.value.anomaly.details["scope"] == "lifetime"


def test_lifetime_cap_off_by_default_even_across_a_long_history(clock):
    runbound.init(max_session_seconds=HOUR, on_anomaly="raise")

    with runbound.session("user:1"):
        _do_work()
    clock.advance(10 * HOUR)

    with runbound.session("user:1"):
        _do_work()  # would have tripped the old, identity-scoped semantics

    assert runbound.is_tripped("user:1") is None


def test_nested_blocks_reset_only_the_innermost_sessions_clock(clock):
    """A parent keyed session's run clock must not be disturbed by a child
    session's own entry and exit."""
    runbound.init(max_session_seconds=HOUR, on_anomaly="raise")

    with runbound.session("parent"):
        _do_work()
        clock.advance(30 * 60)
        with runbound.session("child"):
            _do_work()
        clock.advance(30 * 60 + 1)  # parent now at 3601s since its own entry
        with pytest.raises(GuardrailTripped) as excinfo:
            _do_work()

    assert excinfo.value.anomaly.details["key"] == "parent"


def test_the_default_unkeyed_session_run_clock_is_its_creation(clock):
    """The default session has no session() entry to reset on, so its run
    clock is simply the process's own age — unchanged from before T133."""
    runbound.init(max_session_seconds=HOUR, on_anomaly="raise")

    _do_work()
    clock.advance(3601.0)

    with pytest.raises(GuardrailTripped) as excinfo:
        _do_work()

    assert excinfo.value.anomaly.detector == "timeout"
    assert excinfo.value.anomaly.details["scope"] == "run"
