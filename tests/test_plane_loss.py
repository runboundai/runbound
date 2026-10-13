"""What a worker does with a fleet-wide halt when the control plane goes quiet.

A halt is the heaviest thing the plane can say, so it holds until the plane lifts it
(the enforcement floor, F1): going quiet does not release it. ``halt_stale_s`` only
reports how long the halt has gone unconfirmed.

No sockets and no sleeping: the plane is an in-memory double and the clock is
one the test moves by hand.
"""

from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply, PlaneStatus
from runbound.shared import RemoteState

QUIET_S = 24 * 3600  # a day without a word from the plane


class MovableClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Plane:
    """A control-plane double that is never asked anything in these tests."""

    service = "checkout"
    worker_id = "host-1:42"
    key_state = "unknown"
    consecutive_failures = 0

    def policy(self, service: str):  # pragma: no cover - unused here
        return None


def config(**kwargs) -> RunboundConfig:
    fields = {
        "control_plane_url": "https://plane.example",
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    fields.update(kwargs)
    return RunboundConfig(**fields)


def remote(clock: MovableClock, **kwargs) -> RemoteState:
    return RemoteState(Plane(), None, config(**kwargs), now=clock)


def test_a_halt_is_enforced_while_the_plane_is_quiet():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(59)

    assert shared.halted() is True


def test_a_halt_is_never_released_by_silence():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(59)
    assert shared.halted() is True

    clock.advance(QUIET_S)
    assert shared.halted() is True


def test_a_fresh_heartbeat_confirms_a_halt_that_holds_anyway():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))
    clock.advance(QUIET_S)
    shared.apply_hello(HelloReply(halt=True))
    clock.advance(QUIET_S)

    assert shared.halted() is True


def test_a_heartbeat_saying_no_halt_lifts_it_at_once():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    shared.apply_hello(HelloReply(halt=False))

    assert shared.halted() is False


# --- halt_stale_s on PlaneStatus ---------------------------------------------


def test_halt_stale_s_is_none_with_no_halt():
    shared = remote(MovableClock())

    assert shared.status().halt_stale_s is None


def test_halt_stale_s_counts_seconds_since_last_contact_while_enforced():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(30.0)

    status = shared.status()
    assert status.halt_stale_s == 30.0
    assert status.halt_stale_s == status.last_contact_age_s


def test_halt_stale_s_keeps_counting_because_the_halt_is_never_released_by_silence():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(QUIET_S)

    assert shared.halted() is True
    assert shared.status().halt_stale_s == QUIET_S


def test_plane_status_default_construction_is_unchanged():
    assert PlaneStatus() == PlaneStatus(
        mode="local",
        last_contact_age_s=None,
        consecutive_failures=0,
        notice=None,
        entitlements={},
        halt_stale_s=None,
    )
