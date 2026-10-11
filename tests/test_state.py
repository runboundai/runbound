"""Tests for SessionState accounting and thread safety."""

import threading
import time

from runbound.events import Event
from runbound.state import SessionState


def tool_event(step: int, name: str = "search", kind: str = "tool_call") -> Event:
    return Event(kind=kind, ts=time.monotonic(), step=step, tool_name=name, args_hash="abc")


def test_initial_state_is_empty():
    before = time.monotonic()
    state = SessionState("sess-1")
    after = time.monotonic()

    assert state.session_id == "sess-1"
    assert state.key is None
    assert state.tags == {}
    assert state.event_count == 0
    assert state.requested_actions == 0
    assert (state.admitted_actions, state.executed_actions, state.refused_actions) == (0, 0, 0)
    assert state.tool_calls == {}
    assert state.total_errors == 0
    assert state.tripped_by is None and state.tripped_at is None
    assert before <= state.started_at <= after
    assert isinstance(state.lock, type(threading.RLock()))


def test_key_and_tags_identify_a_keyed_session_and_tags_are_copied():
    tags = {"plan": "free"}
    state = SessionState("s", key="user-1", tags=tags)
    tags["plan"] = "pro"

    assert state.key == "user-1"
    assert state.tags == {"plan": "free"}


def test_record_tool_call_counts_per_tool_name():
    state = SessionState("s")
    state.record(tool_event(1, "search"))
    state.record(tool_event(2, "search"))
    state.record(tool_event(3, "lookup"))

    assert state.tool_calls == {"search": 2, "lookup": 1}
    assert state.event_count == 3


def test_a_tool_call_without_a_name_is_not_tallied():
    state = SessionState("s")
    state.record(Event(kind="tool_call", ts=time.monotonic(), step=1))

    assert state.tool_calls == {}
    assert state.event_count == 1


def test_tool_request_counts_requested_actions_not_tool_calls():
    state = SessionState("s")
    state.record(tool_event(1, "search", kind="tool_request"))
    state.record(tool_event(2, "search", kind="tool_request"))

    assert state.requested_actions == 2
    assert state.tool_calls == {}


def test_tool_error_counts_errors_and_not_calls():
    state = SessionState("s")
    state.record(tool_event(1, kind="tool_error"))

    assert state.total_errors == 1
    assert state.tool_calls == {}


def test_event_count_tracks_max_step_seen():
    state = SessionState("s")
    state.record(tool_event(5))
    assert state.event_count == 5

    state.record(tool_event(3))
    assert state.event_count == 5

    state.record(tool_event(9))
    assert state.event_count == 9


def test_repeated_events_on_same_step_do_not_inflate_event_count():
    state = SessionState("s")
    state.record(tool_event(1))
    state.record(tool_event(1, kind="tool_error"))
    state.record(tool_event(1, kind="tool_request"))

    assert state.event_count == 1


def test_next_step_is_one_based_and_increasing():
    state = SessionState("s")

    assert [state.next_step() for _ in range(3)] == [1, 2, 3]


def test_admitted_and_refused_actions_are_counted_separately():
    state = SessionState("s")
    state.mark_action_admitted()
    state.mark_action_admitted()
    state.mark_action_refused()

    assert (state.admitted_actions, state.executed_actions, state.refused_actions) == (2, 2, 1)


def test_record_is_thread_safe_under_concurrency():
    threads_count, per_thread = 8, 100
    state = SessionState("s")

    def worker() -> None:
        for _ in range(per_thread):
            step = state.next_step()
            state.record(tool_event(step))
            state.record(tool_event(step, kind="tool_error"))
            state.mark_action_admitted()
            state.mark_action_refused()

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = threads_count * per_thread
    assert state.tool_calls == {"search": total}
    assert state.total_errors == total
    assert state.event_count == total
    assert state.executed_actions == total
    assert state.refused_actions == total


def test_lock_is_reentrant():
    state = SessionState("s")
    with state.lock:
        state.record(tool_event(1))

    assert state.event_count == 1
