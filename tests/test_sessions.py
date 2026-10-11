"""Tests for keyed sessions: the registry, the context var, and the state and
config plumbing they rest on (Event timing fields, per-session step counters).
"""

import asyncio
import dataclasses
import logging
import threading

import pytest

import runbound
from runbound import api
from runbound.config import RunboundConfig
from runbound.events import Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.state import SessionState


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@runbound.tool
def ping(n=0):
    return n


# --- Event ------------------------------------------------------------------


def test_event_timing_fields_default_to_zero():
    event = Event(kind="tool_call", ts=1.0, step=1)

    assert event.duration_s == 0.0
    assert event.tokens_reasoning == 0


def test_event_timing_fields_are_carried_and_frozen():
    event = Event(kind="tool_call", ts=1.0, step=1, duration_s=2.5, tokens_reasoning=7)

    assert (event.duration_s, event.tokens_reasoning) == (2.5, 7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.duration_s = 3.0


# --- RunboundConfig --------------------------------------------------------


def test_max_sessions_defaults_to_ten_thousand():
    assert RunboundConfig().max_sessions == 10_000


@pytest.mark.parametrize("value", [0, -1])
def test_max_sessions_below_one_raises(value):
    with pytest.raises(ValueError):
        RunboundConfig(max_sessions=value).validate()


def test_max_sessions_of_one_is_valid():
    RunboundConfig(max_sessions=1).validate()


def test_init_accepts_max_sessions():
    """``max_sessions`` is a plain ``RunboundConfig`` keyword, independent of the plane."""
    runbound.init(max_sessions=5)

    assert api._ENGINE.config.max_sessions == 5


# --- SessionState -----------------------------------------------------------


def test_key_and_tags_default_to_none_and_empty():
    state = SessionState("s")

    assert state.key is None
    assert state.tags == {}


def test_key_and_tags_are_carried():
    state = SessionState("s", key="user-42", tags={"plan": "free"})

    assert state.key == "user-42"
    assert state.tags == {"plan": "free"}


def test_tags_default_is_not_shared_between_instances():
    a, b = SessionState("a"), SessionState("b")
    a.tags["x"] = 1

    assert b.tags == {}


def test_next_step_counts_from_one_per_session():
    a, b = SessionState("a"), SessionState("b")

    assert [a.next_step() for _ in range(3)] == [1, 2, 3]
    assert b.next_step() == 1


def test_next_step_is_thread_safe():
    state = SessionState("s")
    steps: list[int] = []
    lock = threading.Lock()

    def worker():
        mine = [state.next_step() for _ in range(100)]
        with lock:
            steps.extend(mine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(steps) == list(range(1, 801))


def test_next_step_may_be_called_while_holding_the_lock():
    state = SessionState("s")
    with state.lock:
        assert state.next_step() == 1


# --- session(): the context manager -----------------------------------------


def test_session_creates_a_keyed_state_with_key_and_tags():
    runbound.init()

    with api.session("user-42", tags={"plan": "free"}) as state:
        assert isinstance(state, SessionState)
        assert state.key == "user-42"
        assert state.tags == {"plan": "free"}
        assert state.session_id
        assert api.current_session() is state


def test_session_without_tags_gets_an_empty_dict():
    runbound.init()

    with api.session("k") as state:
        assert state.tags == {}


def test_a_keyed_session_id_is_derived_from_the_key():
    """Every worker running the same code derives the same id for the same key."""
    runbound.init()

    with api.session("user-42") as first:
        pass
    api._REGISTRY.clear()
    with api.session("user-42") as second:
        pass

    assert second is not first
    assert second.session_id == first.session_id
    assert first.session_id.endswith("-0")
    with api.session("user-43") as other:
        pass
    assert other.session_id != first.session_id


def test_same_key_returns_the_same_state_and_accumulates():
    runbound.init()

    with api.session("k") as first:
        ping()
    with api.session("k") as second:
        ping()

    assert second is first
    assert first.event_count == 2
    assert first.tool_calls == {"ping": 2}


def test_reused_key_merges_newly_supplied_tags():
    runbound.init()

    with api.session("k", tags={"plan": "free"}) as first:
        pass
    with api.session("k", tags={"region": "eu"}) as second:
        pass
    with api.session("k") as third:
        pass

    assert second is first is third
    assert first.tags == {"plan": "free", "region": "eu"}


def test_different_keys_are_isolated():
    runbound.init()

    with api.session("a") as a:
        ping()
    with api.session("b") as b:
        ping()
        ping()

    assert a is not b
    assert a.session_id != b.session_id
    assert (a.event_count, a.tool_calls) == (1, {"ping": 1})
    assert (b.event_count, b.tool_calls) == (2, {"ping": 2})


def test_a_max_calls_refusal_in_one_key_leaves_the_others_running():
    runbound.init()

    @runbound.tool(max_calls=2)
    def limited():
        return "ok"

    with api.session("greedy"):
        limited()
        limited()
        with pytest.raises(PolicyViolation) as caught:
            limited()  # the third attempt, over the limit of 2

    assert caught.value.anomaly.detector == "policy"
    assert caught.value.anomaly.details["key"] == "greedy"

    with api.session("cheap") as cheap:  # an untouched key keeps its own count
        limited()
        limited()

    assert cheap.tool_calls == {"limited": 2}
    assert runbound.tool_calls("greedy") == {"limited": 3}


def test_nested_sessions_restore_the_outer_one_on_exit():
    runbound.init()

    with api.session("a") as a:
        assert api.current_session() is a
        with api.session("b") as b:
            assert api.current_session() is b
            ping()
        assert api.current_session() is a
        ping()

    assert (a.event_count, b.event_count) == (1, 1)


def test_outside_any_block_the_default_session_is_used():
    runbound.init()
    default = runbound.current_session()

    with api.session("k") as keyed:
        ping()

    ping()

    assert api.current_session() is default
    assert default is not keyed
    assert default.key is None
    assert (default.event_count, keyed.event_count) == (1, 1)


def test_default_session_step_numbering_is_unchanged():
    runbound.init()
    events: list[Event] = []
    api._ENGINE.observers.append(_Recorder(events))

    ping()
    with api.session("k"):
        ping()
    ping()
    ping()

    assert [event.step for event in events] == [1, 1, 2, 3]


class _Recorder:
    """An observer that records every event the engine hands it."""

    def __init__(self, events: list) -> None:
        self.events = events

    def on_event(self, session, event) -> None:
        if event.kind == "tool_result":  # the call-side events are what these tests are about
            return
        self.events.append(event)

    def on_anomaly(self, session, anomaly, reacted) -> None:
        pass


def test_session_is_inert_before_init():
    with api.session("k", tags={"plan": "free"}) as state:
        assert state is None
        ping()  # still records nothing

    assert runbound.current_session() is None
    assert api._REGISTRY == {}


def test_session_survives_a_broken_registry(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()

    def boom(*args, **kwargs):
        raise RuntimeError("no state for you")

    monkeypatch.setattr(api, "SessionState", boom)
    default = runbound.current_session()

    with api.session("k") as state:  # fail-open: the host's block still runs
        assert state is None
        assert api.current_session() is default

    assert "runbound" in caplog.text


def test_a_session_block_counts_as_active_until_it_exits():
    runbound.init()

    assert api._active_count() == 0
    with api.session("a"):
        with api.session("b"):
            assert api._active_count() == 2
        assert api._active_count() == 1
    assert api._active_count() == 0


def test_the_active_count_is_decremented_when_the_body_raises():
    runbound.init()

    with pytest.raises(RuntimeError):
        with api.session("a"):
            raise RuntimeError("the agent blew up")

    assert api._active_count() == 0


# --- session(): the LRU registry --------------------------------------------


def _touch(key: str) -> SessionState:
    with api.session(key) as state:
        return state


def test_least_recently_used_key_is_evicted_at_capacity():
    runbound.init(max_sessions=3)

    a, b, c = _touch("a"), _touch("b"), _touch("c")
    d = _touch("d")

    assert list(api._REGISTRY) == ["b", "c", "d"]
    assert _touch("b") is b
    assert _touch("c") is c
    assert _touch("d") is d
    assert _touch("a") is not a  # evicted, so re-entering builds a fresh state


def test_reusing_a_key_makes_it_most_recently_used():
    runbound.init(max_sessions=3)

    a, b = _touch("a"), _touch("b")
    _touch("c")
    _touch("a")  # a is now the newest, b the oldest
    _touch("d")

    assert list(api._REGISTRY) == ["c", "a", "d"]
    assert _touch("a") is a
    assert _touch("b") is not b


def test_capacity_of_one_keeps_only_the_newest_key():
    runbound.init(max_sessions=1)

    a = _touch("a")
    _touch("b")

    assert list(api._REGISTRY) == ["b"]
    assert _touch("a") is not a


def test_init_and_reset_clear_the_registry():
    runbound.init()
    keyed = _touch("k")
    default = runbound.current_session()

    runbound.reset()

    assert api._REGISTRY == {}
    assert runbound.current_session() is not default
    assert _touch("k") is not keyed

    runbound.init()

    assert api._REGISTRY == {}


def test_registry_is_cleared_on_teardown():
    runbound.init()
    _touch("k")

    api._teardown_for_tests()

    assert api._REGISTRY == {}
    assert api.current_session() is None


# --- session(): threads and asyncio -----------------------------------------


def test_concurrent_threads_keep_their_own_session():
    runbound.init()
    per_thread = 50

    def worker(key: str) -> None:
        with api.session(key):
            for _ in range(per_thread):
                ping()

    threads = [threading.Thread(target=worker, args=(f"k{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for i in range(4):
        state = api._REGISTRY[f"k{i}"]
        assert state.event_count == per_thread
        assert state.tool_calls == {"ping": per_thread}
    assert runbound.current_session().event_count == 0  # default untouched


def test_asyncio_tasks_keep_their_own_session():
    runbound.init()

    async def worker(key: str, calls: int) -> None:
        with api.session(key) as state:
            for _ in range(calls):
                ping()
                await asyncio.sleep(0)  # let the other task interleave
                assert api.current_session() is state

    async def main() -> None:
        await asyncio.gather(worker("a", 3), worker("b", 5))

    asyncio.run(main())

    assert api._REGISTRY["a"].event_count == 3
    assert api._REGISTRY["b"].event_count == 5
    assert runbound.current_session().event_count == 0


def test_session_is_exported():
    assert runbound.session is api.session
    assert "session" in runbound.__all__
