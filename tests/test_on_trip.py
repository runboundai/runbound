"""``on_trip``: whether a critical trip latches the session or stops one call.

The latch is the default because a caught exception must not let a blocked
user spend on; ``"once"`` is the explicit opt-out for hosts that handle
blocking themselves.
"""

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.exceptions import GuardrailTripped


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def _spend(user: str) -> None:
    with runbound.session(user):
        api._record_llm_call("gpt-4o", 2000, 500)  # ~$0.01


def _trips(user: str, calls: int) -> int:
    trips = 0
    for _ in range(calls):
        try:
            _spend(user)
        except GuardrailTripped:
            trips += 1
    return trips


def test_on_trip_validation():
    with pytest.raises(ValueError):
        GuardrailConfig(on_trip="forever").validate()
    GuardrailConfig(on_trip="latch").validate()
    GuardrailConfig(on_trip="once").validate()


def test_default_is_latch_every_later_call_is_refused():
    runbound.init(budget_usd=0.02, on_anomaly="raise")

    assert _trips("user:a", 10) == 8  # 2 fit, the breach + 7 refusals
    assert runbound.is_tripped("user:a") is not None


def test_once_stops_only_the_breaching_call():
    """A caught exception lets the user continue: the customer chose that."""
    runbound.init(budget_usd=0.02, on_anomaly="raise", on_trip="once")

    trips = _trips("user:a", 10)

    assert trips == 1  # fire-once detector, no latch
    assert runbound.is_tripped("user:a") is None
    with runbound.session("user:a"):
        pass  # entering never raises under "once"


def test_once_applies_to_loop_break_too():
    runbound.init(on_anomaly="raise", on_loop="break", on_trip="once")
    runs = 0

    @runbound.tool
    def search(q):
        nonlocal runs
        runs += 1

    with runbound.session("user:a"):
        with pytest.raises(GuardrailTripped):
            for _ in range(3):
                search("same")
        search("same")  # 4th call is not refused by a latch under "once"

    assert runs == 3
    assert runbound.is_tripped("user:a") is None


def test_once_with_callback_invokes_it_once_not_per_event():
    seen = []
    runbound.init(
        budget_usd=0.02, on_anomaly="callback", callback=seen.append, on_trip="once"
    )

    _trips("user:a", 10)

    assert [a.detector for a in seen] == ["budget"]
