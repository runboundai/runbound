"""What a worker reports when it turns a key away at the door.

The plane counts these and must not latch on them, and it decides that from
one explicit field. Pin the field, and that a real trip does not carry it.
"""

import pytest

from runbound import api
from runbound.events import Anomaly

import test_ladder_api as ladder_tests
from test_ladder_api import KEY, _plane, _uninitialized, clock  # noqa: F401


def _report(door: bool, ttl):
    ladder_tests.configure()
    state = api._registered(KEY, None, api._ENGINE._effective_config())
    anomaly = Anomaly(detector="spike", severity="critical", message="m", details={})
    return api._SHARED._trip_report("d" * 64, state, anomaly, ttl, door, "door" if door else "raise", KEY)


def test_a_refused_entry_is_reported_with_the_explicit_door_flag_and_no_expiry(clock):
    report = _report(True, None)

    assert report.refused_at_door is True
    assert report.latch_ttl_s is None


def test_a_real_trip_does_not_carry_the_flag(clock):
    report = _report(False, 30.0)

    assert report.refused_at_door is False
    assert report.latch_ttl_s == 30.0


def test_the_flag_is_named_refused_at_door_on_the_wire(clock):
    from runbound.plane_types import to_wire

    assert to_wire(_report(True, None))["refused_at_door"] is True
