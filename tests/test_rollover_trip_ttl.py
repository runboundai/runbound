"""A ladder-closed key is let back in across the fleet when its cooldown ends.

The ladder's closing call is reported to the plane as a trip, and the plane
latches the key for as long as the trip says. A trip with no expiry is
forever, so a key the ladder closed could never be served by any worker again
without an operator's clear. The closing trip now carries the cooldown the
key's fresh session will serve, the last strike's block stays permanent, and
the two numbers come from one place.
"""

import logging

import pytest

import runbound
from runbound import api, ladder
from runbound.events import Anomaly
from runbound.plane_types import HelloReply
from spike_test_helpers import spike_controls_body

import test_ladder_api as ladder_tests
from test_ladder_api import COOLDOWN, KEY, _plane, _uninitialized, clock  # noqa: F401


@pytest.fixture
def trips(monkeypatch):
    """Every trip the worker hands the fleet, as (key, ttl, action)."""
    seen: list = []

    def spy(key, state, anomaly, ttl, door):
        seen.append((key, ttl, (anomaly.details or {}).get("action"), door))

    ladder_tests.configure()
    monkeypatch.setattr(api._SHARED, "trip", spy)
    return seen


def _closing(trips) -> list:
    """The trips the ladder's close made (not the door trips that follow it)."""
    return [t for t in trips if t[2] == "rollover" and t[3] is False]


# --- C1: the ttl on the close ------------------------------------------------------


def test_c1_the_closing_trip_carries_the_cooldown(clock, trips):
    ladder_tests.to_the_limit()
    ladder_tests.close_the_session()

    ((key, ttl, action, door),) = _closing(trips)
    assert (key, ttl, action, door) == (KEY, COOLDOWN, "rollover", False)


def test_c1_a_sessions_own_override_wins(clock, trips):
    """A session that already carries its own expiry reports that one."""
    engine = api._ENGINE
    session = api._registered(KEY, None, engine._effective_config())
    session.latch_ttl_override = 7.0
    closing = Anomaly(detector="spike", severity="critical", message="m", details={
        "action": "rollover", "strikes": 1, "cooldown_seconds": COOLDOWN})
    seen = []
    api._SHARED.trip = lambda k, s, a, ttl, door: seen.append(ttl)

    engine._report_trip(session, closing, door=False)

    assert seen == [7.0]


def test_c1_a_trip_that_is_not_a_close_keeps_the_configured_ttl(clock, monkeypatch):
    ladder_tests.configure(latch_ttl_seconds=45.0)
    seen = []
    monkeypatch.setattr(api._SHARED, "trip", lambda k, s, a, ttl, door: seen.append(ttl))
    engine = api._ENGINE
    session = api._REGISTRY.get(KEY) or api._registered(KEY, None, engine._effective_config())
    other = Anomaly(detector="budget", severity="critical", message="m", details={})

    engine._report_trip(session, other, door=False)

    assert seen == [45.0]


@pytest.mark.parametrize("cooldown", [None, "soon", float("nan"), 0, -5, True])
def test_c1_a_missing_or_unusable_cooldown_falls_back_to_the_effective_one(clock, trips, cooldown, caplog):
    engine = api._ENGINE
    session = api._registered(KEY, None, engine._effective_config())
    details = {"action": "rollover", "strikes": 1, "max_strikes": 2, "cooldown_seconds": cooldown}
    anomaly = Anomaly(detector="spike", severity="critical", message="m", details=details)
    seen = []
    api._SHARED.trip = lambda k, s, a, ttl, door: seen.append(ttl)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        engine._report_trip(session, anomaly, door=False)

    assert seen == [COOLDOWN]  # never None for a rollover
    assert "could not report a trip" not in caplog.text


def test_c1_a_failure_choosing_the_ttl_is_logged_and_never_raised(clock, trips, monkeypatch, caplog):
    engine = api._ENGINE
    session = api._registered(KEY, None, engine._effective_config())
    monkeypatch.setattr(type(engine), "_effective_config", lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    anomaly = Anomaly(detector="spike", severity="critical", message="m",
                      details={"action": "rollover", "strikes": 1, "cooldown_seconds": 9})

    with caplog.at_level(logging.WARNING, logger="runbound"):
        engine._report_trip(session, anomaly, door=False)  # must not raise

    assert "could not report a trip" in caplog.text


# --- C2: one cooldown, one number --------------------------------------------------------


def test_c2_the_trip_ttl_equals_the_fresh_sessions_cooldown(clock, trips):
    ladder_tests.to_the_limit()
    ladder_tests.close_the_session()
    ladder_tests.refused()  # the rollover

    fresh = api._REGISTRY[KEY]
    assert _closing(trips)[0][1] == fresh.latch_ttl_override == COOLDOWN


def test_c2_they_agree_when_the_plane_states_a_longer_cooldown(clock, trips):
    """The effective cooldown is the longer of the code's and the plane's; the
    trip and the fresh session must both use it."""
    longer = COOLDOWN * 2.5
    ladder_tests._PLANE.controls_body = {"version": 2, "controls": spike_controls_body(
        "limit", spike_warmup_calls=4, spike_confirm=2, spike_limit_calls=2,
        spike_cooldown_seconds=longer, spike_max_strikes=2, spike_min_duration_s=1.0)}
    api._SHARED.apply_hello(HelloReply(controls_version=2))
    assert api._ENGINE._effective_config().spike_cooldown_seconds == longer

    ladder_tests.to_the_limit()
    ladder_tests.close_the_session()
    ladder_tests.refused()

    assert _closing(trips)[0][1] == api._REGISTRY[KEY].latch_ttl_override == longer


# --- C3: the terminal block stays permanent -----------------------------------------------


def test_c3_the_last_strikes_close_reports_no_expiry(clock, trips):
    """Out of strikes (`strikes >= spike_max_strikes`, the predicate the
    rollover itself uses to choose a block over a cooldown): permanent until
    clear(). The first close, with a strike left, carries the cooldown."""
    ladder_tests.rolled_over()
    clock.advance(COOLDOWN + 1)
    ladder_tests.to_the_limit()
    ladder_tests.close_the_session()

    first, second = _closing(trips)
    assert first[1] == COOLDOWN
    assert second[1] is None
    assert ladder_tests.refused().details["action"] == "blocked"
    assert api._REGISTRY[KEY].latch_ttl_override is None  # agrees with the local block


def test_c3_the_predicate_is_the_ladders_own(clock, trips):
    config = api._ENGINE._effective_config()
    for strikes in range(1, config.spike_max_strikes + 2):
        engine = api._ENGINE
        session = api._registered(KEY, None, engine._effective_config())
        anomaly = Anomaly(detector="spike", severity="critical", message="m", details={
            "action": "rollover", "strikes": strikes, "cooldown_seconds": COOLDOWN})
        seen = []
        api._SHARED.trip = lambda k, s, a, ttl, door: seen.append(ttl)
        engine._report_trip(session, anomaly, door=False)
        terminal = ladder.entry_observation(strikes, config) == ladder.Observation.ENTRY_OUT_OF_STRIKES
        assert seen == [None if terminal else COOLDOWN], strikes
