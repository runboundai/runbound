"""What a worker does with a fleet-wide halt when the control plane goes quiet.

A halt is the heaviest thing the plane can say, so it is also the first thing
a worker stops believing when the plane stops confirming it: after
``STALE_HALT_S`` without contact the halt lapses and the fleet runs again
(``shared.py``'s rule 1: the plane is an optimization, never a dependency).

No sockets and no sleeping: the plane is an in-memory double and the clock is
one the test moves by hand.
"""

from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply, PlaneStatus
from runbound.shared import STALE_HALT_S, RemoteState


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


def test_a_halt_is_enforced_inside_the_window():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S - 1)

    assert shared.halted() is True


def test_a_stale_halt_is_released_after_the_window():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S - 1)
    assert shared.halted() is True

    clock.advance(2)
    assert shared.halted() is False


def test_a_fresh_heartbeat_keeps_a_halt_alive_past_the_window():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))
    clock.advance(STALE_HALT_S - 1)
    shared.apply_hello(HelloReply(halt=True))
    clock.advance(STALE_HALT_S - 1)

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


def test_halt_stale_s_goes_back_to_none_once_release_lets_the_halt_lapse():
    clock = MovableClock()
    shared = remote(clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S + 1)

    assert shared.halted() is False
    assert shared.status().halt_stale_s is None


def test_plane_status_default_construction_is_unchanged():
    assert PlaneStatus() == PlaneStatus(
        mode="local",
        last_contact_age_s=None,
        consecutive_failures=0,
        notice=None,
        entitlements={},
        halt_stale_s=None,
    )
