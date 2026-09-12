"""Tests for keyed sessions: the registry, the context var, and the state and
config plumbing they rest on (Event timing fields, per-session step counters,
recent_calls, spike configuration).
"""

import asyncio
import dataclasses
import logging
import threading
import time

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.events import Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def llm_event(step: int, **fields) -> Event:
    fields.setdefault("tokens_out", 0)
    return Event(kind="llm_call", ts=time.monotonic(), step=step, **fields)


# --- Event ------------------------------------------------------------------


def test_event_timing_fields_default_to_zero():
    event = Event(kind="llm_call", ts=1.0, step=1)

    assert event.duration_s == 0.0
    assert event.tokens_reasoning == 0


def test_event_timing_fields_are_carried_and_frozen():
    event = Event(kind="llm_call", ts=1.0, step=1, duration_s=2.5, tokens_reasoning=7)

    assert (event.duration_s, event.tokens_reasoning) == (2.5, 7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.duration_s = 3.0


# --- GuardrailConfig --------------------------------------------------------


def test_spike_config_defaults():
    cfg = GuardrailConfig()

    assert cfg.spike_detection is True
    assert cfg.spike_warmup_calls == 4
    assert cfg.spike_window == 50
    assert cfg.spike_factor == 10.0
    assert cfg.spike_confirm == 2
    assert cfg.max_call_seconds is None
    assert cfg.max_tokens_out_per_call is None
    assert cfg.max_sessions == 10_000


def test_spike_defaults_validate():
    GuardrailConfig().validate()


@pytest.mark.parametrize("warmup", [1, 0, -1])
def test_warmup_below_two_raises(warmup):
    with pytest.raises(ValueError):
        GuardrailConfig(spike_warmup_calls=warmup, spike_window=50).validate()


def test_warmup_of_two_is_valid():
    GuardrailConfig(spike_warmup_calls=2, spike_window=3).validate()


@pytest.mark.parametrize("window", [10, 9])
def test_window_not_above_warmup_raises(window):
    with pytest.raises(ValueError):
        GuardrailConfig(spike_warmup_calls=10, spike_window=window).validate()


def test_window_one_above_warmup_is_valid():
    GuardrailConfig(spike_warmup_calls=10, spike_window=11).validate()


@pytest.mark.parametrize("factor", [1.0, 0.5, 0.0, -2.0])
def test_factor_not_above_one_raises(factor):
    with pytest.raises(ValueError):
        GuardrailConfig(spike_factor=factor).validate()


def test_factor_just_above_one_is_valid():
    GuardrailConfig(spike_factor=1.01).validate()


@pytest.mark.parametrize("confirm", [0, -1, 6, 50])
def test_confirm_outside_one_to_five_raises(confirm):
    with pytest.raises(ValueError):
        GuardrailConfig(spike_confirm=confirm).validate()


@pytest.mark.parametrize("confirm", [1, 5])
def test_confirm_at_the_boundaries_is_valid(confirm):
    GuardrailConfig(spike_confirm=confirm).validate()


@pytest.mark.parametrize("field", ["max_call_seconds", "max_tokens_out_per_call"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_call_caps_raise(field, value):
    with pytest.raises(ValueError):
        GuardrailConfig(**{field: value}).validate()


@pytest.mark.parametrize("field", ["max_call_seconds", "max_tokens_out_per_call"])
def test_call_caps_are_optional_and_accept_the_smallest_positive(field):
    GuardrailConfig(**{field: None}).validate()
    GuardrailConfig(**{field: 1}).validate()


@pytest.mark.parametrize("value", [0, -1])
def test_max_sessions_below_one_raises(value):
    with pytest.raises(ValueError):
        GuardrailConfig(max_sessions=value).validate()


def test_max_sessions_of_one_is_valid():
    GuardrailConfig(max_sessions=1).validate()


def test_init_accepts_the_new_options():
    runbound.init(spike_detection=False, spike_window=11, max_sessions=5)

    assert api._ENGINE.config.spike_detection is False
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


def test_recent_calls_starts_empty_with_spike_window_maxlen():
    assert SessionState("s").recent_calls.maxlen == 50
    assert list(SessionState("s").recent_calls) == []
    assert SessionState("s", spike_window=5).recent_calls.maxlen == 5


def test_record_appends_llm_calls_with_output_work():
    state = SessionState("s")
    state.record(
        llm_event(step=1, duration_s=1.5, tokens_out=100, tokens_reasoning=400, cost_usd=0.25)
    )

    # tokens_out already includes reasoning per provider semantics; the
    # separate tokens_reasoning field must not be double-counted here.
    assert list(state.recent_calls) == [(1.5, 100, 0.25)]


def test_record_appends_only_llm_calls():
    state = SessionState("s")
    state.record(Event(kind="tool_call", ts=1.0, step=1, tool_name="t", args_hash="h"))
    state.record(Event(kind="tool_error", ts=1.0, step=2, tool_name="t", error="x"))

    assert list(state.recent_calls) == []


def test_recent_calls_evicts_oldest_beyond_the_window():
    state = SessionState("s", spike_window=3)
    for i in range(5):
        state.record(llm_event(step=i + 1, duration_s=float(i), tokens_out=i))

    assert [work for _, work, _ in state.recent_calls] == [2, 3, 4]


def test_zero_token_llm_call_is_still_recorded():
    state = SessionState("s")
    state.record(llm_event(step=1))

    assert list(state.recent_calls) == [(0.0, 0, 0.0)]


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


def test_same_key_returns_the_same_state_and_accumulates():
    runbound.init()

    with api.session("k") as first:
        api._record_llm_call("m", 10, 5)
    with api.session("k") as second:
        api._record_llm_call("m", 10, 5)

    assert second is first
    assert first.step_count == 2
    assert first.total_tokens == 30
    assert len(first.recent_calls) == 2


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
        api._record_llm_call("m", 10, 5)
    with api.session("b") as b:
        api._record_llm_call("m", 100, 50)

    assert a is not b
    assert a.session_id != b.session_id
    assert (a.step_count, a.total_tokens) == (1, 15)
    assert (b.step_count, b.total_tokens) == (1, 150)


def test_a_budget_trip_in_one_key_leaves_the_others_running():
    runbound.init(
        budget_usd=1.5,
        on_anomaly="raise",
        custom_prices={"m": (1000.0, 0.0)},  # $1 per 1000 input tokens
    )

    with api.session("cheap") as cheap:
        api._record_llm_call("m", 100, 0)  # $0.10

    with api.session("greedy"):
        api._record_llm_call("m", 1000, 0)  # $1.00, under budget
        with pytest.raises(GuardrailTripped) as caught:
            api._record_llm_call("m", 1000, 0)  # $2.00 total, trips

    assert caught.value.anomaly.detector == "budget"

    with api.session("cheap") as again:  # the untouched session keeps working
        api._record_llm_call("m", 100, 0)
        api._record_llm_call("m", 100, 0)

    assert again is cheap
    assert cheap.step_count == 3
    assert abs(cheap.total_cost_usd - 0.30) < 1e-9


def test_nested_sessions_restore_the_outer_one_on_exit():
    runbound.init()

    with api.session("a") as a:
        assert api.current_session() is a
        with api.session("b") as b:
            assert api.current_session() is b
            api._record_llm_call("m", 10, 5)
        assert api.current_session() is a
        api._record_llm_call("m", 10, 5)

    assert (a.step_count, b.step_count) == (1, 1)


def test_outside_any_block_the_default_session_is_used():
    runbound.init()
    default = runbound.current_session()

    with api.session("k") as keyed:
        api._record_llm_call("m", 10, 5)

    api._record_llm_call("m", 10, 5)

    assert api.current_session() is default
    assert default is not keyed
    assert default.key is None
    assert (default.step_count, keyed.step_count) == (1, 1)


def test_default_session_step_numbering_is_unchanged():
    runbound.init()
    events: list[Event] = []
    api._ENGINE.detectors.insert(0, _Recorder(events))

    api._record_llm_call("m", 1, 1)
    with api.session("k"):
        api._record_llm_call("m", 1, 1)
    api._record_llm_call("m", 1, 1)
    api._record_llm_call("m", 1, 1)

    assert [event.step for event in events] == [1, 1, 2, 3]


class _Recorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self, events: list) -> None:
        self.events = events

    def check(self, state, event, config):
        self.events.append(event)
        return None


def test_session_is_inert_before_init():
    with api.session("k", tags={"plan": "free"}) as state:
        assert state is None
        api._record_llm_call("m", 10, 5)  # still records nothing

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
                api._record_llm_call("m", 10, 5)

    threads = [threading.Thread(target=worker, args=(f"k{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for i in range(4):
        state = api._REGISTRY[f"k{i}"]
        assert state.step_count == per_thread
        assert state.total_tokens == per_thread * 15
    assert runbound.current_session().step_count == 0  # default untouched


def test_asyncio_tasks_keep_their_own_session():
    runbound.init()

    async def worker(key: str, calls: int) -> None:
        with api.session(key) as state:
            for _ in range(calls):
                api._record_llm_call("m", 10, 5)
                await asyncio.sleep(0)  # let the other task interleave
                assert api.current_session() is state

    async def main() -> None:
        await asyncio.gather(worker("a", 3), worker("b", 5))

    asyncio.run(main())

    assert api._REGISTRY["a"].step_count == 3
    assert api._REGISTRY["b"].step_count == 5
    assert runbound.current_session().step_count == 0


# --- _record_llm_call -------------------------------------------------------


def test_record_llm_call_keeps_its_three_argument_form():
    runbound.init()
    events: list[Event] = []
    api._ENGINE.detectors.insert(0, _Recorder(events))

    api._record_llm_call("m", 10, 5)

    (event,) = events
    assert (event.duration_s, event.tokens_reasoning) == (0.0, 0)


def test_record_llm_call_carries_duration_and_reasoning_tokens():
    runbound.init()
    events: list[Event] = []
    api._ENGINE.detectors.insert(0, _Recorder(events))

    with api.session("k") as state:
        api._record_llm_call("m", 10, 5, duration_s=4.25, tokens_reasoning=900)

    (event,) = events
    assert (event.tokens_out, event.tokens_reasoning) == (5, 900)
    assert event.duration_s == 4.25
    # output work is tokens_out alone: reasoning is already inside it.
    assert list(state.recent_calls) == [(4.25, 5, event.cost_usd)]


def test_recent_calls_window_comes_from_the_configured_spike_window():
    runbound.init(spike_warmup_calls=2, spike_window=3)

    assert runbound.current_session().recent_calls.maxlen == 3
    with api.session("k") as state:
        for _ in range(5):
            api._record_llm_call("m", 10, 5)

    assert state.recent_calls.maxlen == 3
    assert len(state.recent_calls) == 3


def test_session_is_exported():
    assert runbound.session is api.session
    assert "session" in runbound.__all__
