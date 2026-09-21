"""Tests for SessionState accounting and thread safety."""

import threading
import time

from runbound.events import Event
from runbound.state import SessionState


def llm_event(step: int, tokens_in: int = 10, tokens_out: int = 5, cost: float = 0.01) -> Event:
    return Event(
        kind="llm_call",
        ts=time.monotonic(),
        step=step,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        model="gpt-4o",
    )


def tool_event(step: int, args_hash: str | None = "abc") -> Event:
    return Event(
        kind="tool_call",
        ts=time.monotonic(),
        step=step,
        tool_name="search",
        args_hash=args_hash,
    )


def test_initial_state_is_empty():
    before = time.monotonic()
    state = SessionState("sess-1")
    after = time.monotonic()

    assert state.session_id == "sess-1"
    assert state.step_count == 0
    assert state.total_tokens == 0
    assert state.total_cost_usd == 0.0
    assert before <= state.started_at <= after
    assert list(state.recent_hashes) == []
    assert list(state.token_timestamps) == []
    assert isinstance(state.lock, type(threading.RLock()))


def test_recent_hashes_maxlen_defaults_to_loop_window():
    assert SessionState("s").recent_hashes.maxlen == 20
    assert SessionState("s", loop_window=5).recent_hashes.maxlen == 5


def test_record_llm_call_updates_tokens_and_cost():
    state = SessionState("s")
    state.record(llm_event(step=1, tokens_in=100, tokens_out=50, cost=0.25))

    assert state.total_tokens == 150
    assert state.total_cost_usd == 0.25
    assert state.step_count == 1
    assert list(state.recent_hashes) == []


def test_record_accumulates_across_events():
    state = SessionState("s")
    state.record(llm_event(step=1, tokens_in=10, tokens_out=5, cost=0.01))
    state.record(llm_event(step=2, tokens_in=20, tokens_out=1, cost=0.02))

    assert state.total_tokens == 36
    assert abs(state.total_cost_usd - 0.03) < 1e-9
    assert state.step_count == 2


def test_record_tool_call_appends_hash():
    state = SessionState("s")
    state.record(tool_event(step=1, args_hash="deadbeef"))

    assert list(state.recent_hashes) == ["deadbeef"]
    assert state.total_tokens == 0
    assert state.total_cost_usd == 0.0


def test_tool_call_without_hash_appends_nothing():
    state = SessionState("s")
    state.record(tool_event(step=1, args_hash=None))
    state.record(tool_event(step=2, args_hash=""))

    assert list(state.recent_hashes) == []


def test_non_tool_call_events_never_contribute_hashes():
    state = SessionState("s")
    state.record(
        Event(kind="llm_call", ts=time.monotonic(), step=1, args_hash="h1")
    )
    state.record(
        Event(kind="tool_error", ts=time.monotonic(), step=2, args_hash="h2")
    )

    assert list(state.recent_hashes) == []


def test_recent_hashes_evicts_oldest_beyond_window():
    state = SessionState("s", loop_window=3)
    for i in range(5):
        state.record(tool_event(step=i + 1, args_hash=f"h{i}"))

    assert list(state.recent_hashes) == ["h2", "h3", "h4"]


def test_token_timestamps_record_ts_and_total_tokens():
    state = SessionState("s")
    event = llm_event(step=1, tokens_in=7, tokens_out=3)
    state.record(event)

    assert list(state.token_timestamps) == [(event.ts, 10)]


def test_zero_token_events_are_not_timestamped():
    state = SessionState("s")
    state.record(llm_event(step=1, tokens_in=0, tokens_out=0, cost=0.0))
    state.record(tool_event(step=2))

    assert list(state.token_timestamps) == []


def test_step_count_tracks_max_step_seen():
    state = SessionState("s")
    state.record(llm_event(step=5))
    assert state.step_count == 5

    state.record(llm_event(step=3))
    assert state.step_count == 5

    state.record(llm_event(step=9))
    assert state.step_count == 9


def test_repeated_events_on_same_step_do_not_inflate_step_count():
    state = SessionState("s")
    state.record(llm_event(step=1))
    state.record(tool_event(step=1))
    state.record(llm_event(step=1))

    assert state.step_count == 1


def test_record_is_thread_safe_under_concurrency():
    threads_count, per_thread = 8, 100
    state = SessionState("s", loop_window=threads_count * per_thread)

    def worker(thread_index: int) -> None:
        for i in range(per_thread):
            step = thread_index * per_thread + i + 1
            state.record(
                Event(
                    kind="llm_call",
                    ts=time.monotonic(),
                    step=step,
                    tokens_in=2,
                    tokens_out=3,
                    cost_usd=0.001,
                )
            )
            state.record(tool_event(step=step, args_hash=f"h{step}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total_events = threads_count * per_thread
    assert state.total_tokens == total_events * 5
    assert abs(state.total_cost_usd - total_events * 0.001) < 1e-6
    assert state.step_count == total_events
    assert len(state.recent_hashes) == total_events
    assert len(set(state.recent_hashes)) == total_events
    assert len(state.token_timestamps) == total_events


def test_lock_is_reentrant():
    state = SessionState("s")
    with state.lock:
        state.record(llm_event(step=1))

    assert state.step_count == 1
