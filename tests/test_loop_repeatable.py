"""Tests for Wave 24, T59b: tools marked "supposed to repeat".

``@runbound.tool(repeatable=True)`` and the config-level ``loop_ignore_tools``
both mark a tool's ``tool_call``/``tool_request`` events ``loop_exempt=True``:
the event still counts everywhere a real attempt counts (``tool_calls()``, an
action policy's ``max_calls``) but is skipped by the loop window, so it can
never trip the loop detector however many times it repeats. This marks
polling; it is not a way to raise a loop limit.
"""

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.events import Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolPolicy
from runbound.state import SessionState


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def tool_event(step: int, args_hash: str = "h1", loop_exempt: bool = False) -> Event:
    return Event(
        kind="tool_call",
        ts=float(step),
        step=step,
        tool_name="search",
        args_hash=args_hash,
        loop_exempt=loop_exempt,
    )


# --- Event / SessionState: the plumbing --------------------------------------


def test_event_loop_exempt_defaults_to_false():
    assert Event(kind="tool_call", ts=0.0, step=1).loop_exempt is False


def test_session_state_skips_the_loop_window_for_an_exempt_hash():
    session = SessionState("s1", loop_window=20)

    for step in range(1, 6):
        session.record(tool_event(step, loop_exempt=True))

    assert list(session.recent_hashes) == []
    assert session.tool_calls["search"] == 5  # still counted


def test_session_state_still_feeds_the_loop_window_when_not_exempt():
    session = SessionState("s1", loop_window=20)

    for step in range(1, 4):
        session.record(tool_event(step, loop_exempt=False))

    assert list(session.recent_hashes) == ["h1", "h1", "h1"]
    assert session.tool_calls["search"] == 3


def test_session_state_applies_the_exemption_to_tool_request_too():
    session = SessionState("s1", loop_window=20)
    event = Event(
        kind="tool_request",
        ts=1.0,
        step=1,
        tool_name="search",
        args_hash="req:h1",
        loop_exempt=True,
    )

    session.record(event)

    assert list(session.recent_hashes) == []


# --- config: loop_ignore_tools ------------------------------------------------


def test_loop_ignore_tools_defaults_to_empty():
    assert GuardrailConfig().loop_ignore_tools == ()


def test_loop_ignore_tools_accepts_a_tuple_of_names():
    GuardrailConfig(loop_ignore_tools=("poll_status", "heartbeat")).validate()


@pytest.mark.parametrize("value", [["poll_status"], ("poll_status", 1), "poll_status", None])
def test_loop_ignore_tools_rejects_non_tuple_of_str(value):
    with pytest.raises(ValueError, match="loop_ignore_tools"):
        GuardrailConfig(loop_ignore_tools=value).validate()


# --- @runbound.tool(repeatable=True): never loops, still counts -----------


def test_repeatable_tool_never_trips_the_loop_detector():
    runbound.init(loop_threshold=3, on_loop="break")

    @runbound.tool(repeatable=True)
    def poll_status(job_id):
        return "pending"

    for _ in range(10):
        poll_status("job-1")  # identical args every time; never trips

    assert runbound.tool_calls()["poll_status"] == 10


def test_repeatable_tool_events_carry_loop_exempt():
    runbound.init()

    class Spy:
        name = "spy"

        def __init__(self):
            self.events = []

        def check(self, state, event, config):
            self.events.append(event)
            return None

    spy = Spy()
    api._ENGINE.detectors.insert(0, spy)

    @runbound.tool(repeatable=True)
    def poll_status(job_id):
        return "pending"

    poll_status("job-1")

    assert spy.events[-1].loop_exempt is True


def test_a_non_repeatable_tool_still_trips_the_loop_detector():
    runbound.init(loop_threshold=3, on_loop="break")

    @runbound.tool
    def search(query):
        return "result"

    search("weather")
    search("weather")
    with pytest.raises(GuardrailTripped) as excinfo:
        search("weather")

    assert excinfo.value.anomaly.detector == "loop"


def test_repeatable_tool_still_counts_towards_max_calls():
    runbound.init(
        loop_threshold=3,
        on_loop="break",
        tool_policy=ToolPolicy(max_calls={"poll_status": 2}),
    )

    @runbound.tool(repeatable=True)
    def poll_status(job_id):
        return "pending"

    poll_status("job-1")
    poll_status("job-1")
    with pytest.raises(PolicyViolation):
        poll_status("job-1")  # the loop never fires; max_calls still does


def test_async_repeatable_tool_never_trips_the_loop_detector():
    import asyncio

    runbound.init(loop_threshold=2, on_loop="break")

    @runbound.tool(repeatable=True)
    async def poll_status(job_id):
        return "pending"

    async def drive():
        for _ in range(5):
            await poll_status("job-1")

    asyncio.run(drive())
    assert runbound.tool_calls()["poll_status"] == 5


# --- loop_ignore_tools: applies by name, not by decorator --------------------


def test_loop_ignore_tools_exempts_a_plain_tool_by_name():
    runbound.init(loop_threshold=3, on_loop="break", loop_ignore_tools=("poll_status",))

    @runbound.tool
    def poll_status(job_id):
        return "pending"

    for _ in range(10):
        poll_status("job-1")  # exempt by name alone, no repeatable=True needed

    assert runbound.tool_calls()["poll_status"] == 10


def test_loop_ignore_tools_does_not_exempt_other_tools():
    runbound.init(loop_threshold=3, on_loop="break", loop_ignore_tools=("poll_status",))

    @runbound.tool
    def search(query):
        return "result"

    search("weather")
    search("weather")
    with pytest.raises(GuardrailTripped):
        search("weather")


def test_loop_ignore_tools_applies_to_tool_requests_too():
    runbound.init(loop_threshold=3, on_loop="break", loop_ignore_tools=("poll_status",))

    for _ in range(10):
        api._HOOKS.tool_request("poll_status", "req:same-args")  # never trips

    session = runbound.current_session()
    assert session.tripped_by is None


def test_tool_request_without_loop_ignore_tools_still_loops():
    runbound.init(loop_threshold=2, on_loop="break")

    with pytest.raises(GuardrailTripped):
        for _ in range(5):
            api._HOOKS.tool_request("search", "req:same-args")
