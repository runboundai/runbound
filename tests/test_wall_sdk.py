"""The SDK's side of the wall: what a worker does when the plane says halt.

Two things the plane's heartbeat reply controls on the worker: how often the
heartbeat runs (``HelloReply.poll_s``) and whether tools may run at all (the
halt). A halt is enforced per tool call, in the engine, from the cached state
:class:`~runbound.shared.RemoteState` holds; ``on_halt`` chooses between
refusing and warning.

No sockets and no sleeping: the plane is an in-memory fake and the clock is
one the test moves by hand.
"""

import logging

import pytest

import runbound
from runbound import api
from runbound.config import RunboundConfig
from runbound.exceptions import ExecutionRefused
from runbound.plane_types import HelloReply
from runbound.shared import RemoteState


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Plane:
    service = "checkout"
    worker_id = "host-1:42"
    key_state = "ok"
    consecutive_failures = 0

    def hello(self, payload: dict):
        return None

    def events(self, batch: dict) -> bool:
        return True

    def policy(self, service: str):
        return None


class FakePoller:
    def __init__(self) -> None:
        self.intervals: list[float] = []

    def set_interval(self, poll_s: float) -> None:
        self.intervals.append(poll_s)


def config(**kwargs) -> RunboundConfig:
    fields = {
        "control_plane_url": "https://plane.test",
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    fields.update(kwargs)
    return RunboundConfig(**fields)


def remote(clock: Clock, **kwargs) -> RemoteState:
    return RemoteState(Plane(), None, config(**kwargs), now=clock)


# --- HelloReply.poll_s ---------------------------------------------------------


def test_a_poll_interval_the_plane_states_changes_the_pollers_interval():
    state = remote(Clock())
    poller = state._poller = FakePoller()
    state.apply_hello(HelloReply(poll_s=17.5))
    assert poller.intervals == [17.5]
    state.apply_hello(HelloReply(poll_s=2.0))
    assert poller.intervals == [17.5, 2.0]


@pytest.mark.parametrize("poll_s", [0.0, -3.0])
def test_a_reply_that_states_no_interval_leaves_the_poller_alone(poll_s):
    state = remote(Clock())
    poller = state._poller = FakePoller()
    state.apply_hello(HelloReply(poll_s=poll_s))
    assert poller.intervals == []


def test_the_real_poller_waits_the_interval_the_plane_stated():
    from runbound.plane import Poller

    poller = Poller(Plane(), 5.0, lambda reply: None, dict)
    assert poller._poll_s == 5.0
    poller.set_interval(42.0)
    assert poller._poll_s == 42.0


# --- the halt, per tool call -----------------------------------------------------


@pytest.fixture
def halted_worker():
    """An initialised SDK whose shared state is a RemoteState the test halts."""
    api._teardown_for_tests()
    runbound.init()
    clock = Clock()
    state = remote(clock)
    api._ENGINE.shared = state
    yield state, clock
    api._teardown_for_tests()


def halt(state: RemoteState, mode: str = "stop") -> None:
    state.apply_hello(HelloReply(halt=True, halt_mode=mode))
    assert state.halted()


def make_tools():
    ran: list[str] = []

    @runbound.tool(effects={"read"})
    def look() -> str:
        ran.append("look")
        return "ok"

    @runbound.tool(effects={"write"})
    def save() -> str:
        ran.append("save")
        return "ok"

    @runbound.tool(effects={"financial"})
    def pay() -> str:
        ran.append("pay")
        return "ok"

    return look, save, pay, ran


def test_a_halted_fleet_refuses_a_tool_call_under_on_halt_raise(halted_worker):
    state, _ = halted_worker
    look, _, _, ran = make_tools()
    assert look() == "ok"
    halt(state)
    with pytest.raises(ExecutionRefused) as info:
        look()
    assert info.value.boundary == "halt"
    assert ran == ["look"]


def test_the_tool_runs_again_once_the_plane_lifts_the_halt(halted_worker):
    state, _ = halted_worker
    look, _, _, ran = make_tools()
    halt(state)
    with pytest.raises(ExecutionRefused):
        look()
    state.apply_hello(HelloReply(halt=False))
    assert look() == "ok"
    assert ran == ["look"]


def test_a_halt_the_plane_went_quiet_about_stops_being_enforced(halted_worker):
    from runbound.shared import STALE_HALT_S

    state, clock = halted_worker
    look, _, _, _ = make_tools()
    halt(state)
    clock.advance(STALE_HALT_S + 1)
    assert look() == "ok"


def test_on_halt_warn_logs_and_lets_the_tool_run(caplog):
    api._teardown_for_tests()
    try:
        runbound.init(on_halt="warn")
        state = remote(Clock(), on_halt="warn")
        api._ENGINE.shared = state
        look, _, pay, ran = make_tools()
        halt(state)
        with caplog.at_level(logging.WARNING, logger="runbound"):
            assert look() == "ok"
            assert pay() == "ok"
        assert ran == ["look", "pay"]
        warnings = [r for r in caplog.records if "Fleet halted" in r.getMessage()]
        assert len(warnings) == 1  # once a minute, not once a call
    finally:
        api._teardown_for_tests()


def test_a_narrow_halt_is_a_full_halt(halted_worker):
    state, _ = halted_worker
    look, save, pay, ran = make_tools()
    halt(state, "narrow")
    for tool in (look, save, pay):
        with pytest.raises(ExecutionRefused):
            tool()
    assert ran == []
