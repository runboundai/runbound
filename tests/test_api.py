"""Tests for the public API: init/reset/current_session, @tool, fail-open."""

import logging

import pytest

import runbound
from runbound import api
from runbound.events import Event
from runbound.exceptions import GuardrailTripped


class EventRecorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def check(self, state, event, config):
        self.events.append(event)
        return None


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def recorder() -> EventRecorder:
    """Attach an event recorder to the engine built by the last init()."""
    spy = EventRecorder()
    api._ENGINE.detectors.insert(0, spy)
    return spy


# --- init / reset / current_session -----------------------------------------


def test_init_validates_config_loudly():
    with pytest.raises(ValueError):
        runbound.init(on_anomaly="explode")
    with pytest.raises(ValueError):
        runbound.init(budget_usd=-1)

    assert runbound.current_session() is None


def test_init_rejects_unknown_options():
    with pytest.raises(TypeError):
        runbound.init(budget_in_dollars=5)


def test_init_creates_a_session_and_reconfigures_on_second_call():
    runbound.init(max_steps=10)
    first = runbound.current_session()
    assert first is not None
    assert first.session_id

    runbound.init(max_steps=20)
    second = runbound.current_session()

    assert second is not first
    assert second.session_id != first.session_id
    assert api._ENGINE.config.max_steps == 20


def test_reset_starts_a_fresh_session_but_keeps_the_engine():
    runbound.init(max_steps=10)
    engine = api._ENGINE
    old = runbound.current_session()

    @runbound.tool
    def ping():
        return "pong"

    ping()
    assert old.step_count == 1

    runbound.reset()
    new = runbound.current_session()

    assert api._ENGINE is engine
    assert new.session_id != old.session_id
    assert (new.step_count, new.total_tokens, new.total_cost_usd) == (0, 0, 0.0)


def test_reset_before_init_is_a_no_op():
    runbound.reset()
    assert runbound.current_session() is None


# --- inert until initialized ------------------------------------------------


def test_tool_works_and_records_nothing_before_init():
    @runbound.tool
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    assert runbound.current_session() is None


# --- the tool decorator -----------------------------------------------------


def test_tool_bare_form_emits_a_tool_call_event():
    runbound.init()
    spy = recorder()

    @runbound.tool
    def search(query):
        return f"results for {query}"

    assert search("cats") == "results for cats"
    assert search.__name__ == "search"

    (event,) = spy.events
    assert (event.kind, event.tool_name, event.step) == ("tool_call", "search", 1)
    assert len(event.args_hash) == 64


def test_tool_named_form_overrides_the_tool_name():
    runbound.init()
    spy = recorder()

    @runbound.tool(name="web_search")
    def search(query):
        return "ok"

    search("cats")

    assert spy.events[0].tool_name == "web_search"
    assert search.__name__ == "search"


def test_identical_arguments_hash_identically():
    runbound.init()
    spy = recorder()

    @runbound.tool
    def search(query, limit=10):
        return "ok"

    search("cats", limit=5)
    search("cats", limit=5)

    assert spy.events[0].args_hash == spy.events[1].args_hash


def test_different_arguments_hash_differently():
    runbound.init()
    spy = recorder()

    @runbound.tool
    def search(query, limit=10):
        return "ok"

    search("cats")
    search("dogs")

    assert spy.events[0].args_hash != spy.events[1].args_hash


def test_keyword_order_does_not_change_the_hash():
    runbound.init()
    spy = recorder()

    @runbound.tool
    def search(query=None, limit=None):
        return "ok"

    search(query="cats", limit=5)
    search(limit=5, query="cats")

    assert spy.events[0].args_hash == spy.events[1].args_hash


def test_different_tool_names_hash_differently():
    runbound.init()
    spy = recorder()

    @runbound.tool(name="a")
    def first(query):
        return "ok"

    @runbound.tool(name="b")
    def second(query):
        return "ok"

    first("x")
    second("x")

    assert spy.events[0].args_hash != spy.events[1].args_hash


def test_tool_error_is_emitted_and_the_original_exception_re_raised():
    runbound.init()
    spy = recorder()

    @runbound.tool
    def broken():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        broken()

    call, error = spy.events
    assert call.kind == "tool_call"
    assert (error.kind, error.tool_name, error.error) == ("tool_error", "broken", "kaboom")
    assert error.step == 2


def test_tool_error_message_is_truncated():
    runbound.init()
    spy = recorder()

    @runbound.tool
    def broken():
        raise ValueError("x" * 5000)

    with pytest.raises(ValueError):
        broken()

    assert len(spy.events[1].error) == api.ERROR_MAX_CHARS


def test_loop_trips_before_the_tool_body_runs():
    runbound.init(loop_threshold=3, on_anomaly="raise")
    calls: list[str] = []

    @runbound.tool
    def search(query):
        calls.append(query)
        return "ok"

    search("cats")
    search("cats")

    with pytest.raises(GuardrailTripped) as excinfo:
        search("cats")

    assert excinfo.value.anomaly.detector == "loop"
    assert calls == ["cats", "cats"]  # the tripping call never ran the tool


def test_event_limit_trips_through_the_decorator():
    """T134: steps are model turns now, so tool calls (what @runbound.tool
    records) trip max_events, not max_steps — this test used to pin the old
    "steps = every event" meaning with max_steps; it now exercises max_events
    instead, which is what that meaning became."""
    runbound.init(max_events=2, on_anomaly="raise")

    @runbound.tool
    def ping(n):
        return n

    ping(1)
    ping(2)

    with pytest.raises(GuardrailTripped) as excinfo:
        ping(3)

    assert excinfo.value.anomaly.detector == "events"


def test_step_limit_trips_through_a_model_call():
    """T134: max_steps now counts model turns, which the tool decorator does
    not produce — this exercises the redefined meaning directly."""
    runbound.init(max_steps=2, on_anomaly="raise")

    runbound.record_call("gpt-4o", tokens_in=1, tokens_out=1)
    runbound.record_call("gpt-4o", tokens_in=1, tokens_out=1)

    with pytest.raises(GuardrailTripped) as excinfo:
        runbound.record_call("gpt-4o", tokens_in=1, tokens_out=1)

    assert excinfo.value.anomaly.detector == "steps"


# --- fail-open --------------------------------------------------------------


def test_engine_failure_never_reaches_the_host(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()

    def boom(session, event):
        raise RuntimeError("engine is broken")

    monkeypatch.setattr(api._ENGINE, "process", boom)

    @runbound.tool
    def ping():
        return "pong"

    assert ping() == "pong"
    assert "runbound" in caplog.text


def test_unhashable_arguments_do_not_break_the_call(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()
    spy = recorder()

    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    @runbound.tool
    def ping(payload):
        return "pong"

    assert ping(Hostile()) == "pong"
    assert spy.events[0].args_hash is None  # unhashable => no loop signal, no crash


def test_concurrent_tool_calls_get_unique_steps():
    import threading

    runbound.init()
    spy = recorder()

    @runbound.tool
    def ping(n):
        return n

    threads = [
        threading.Thread(target=lambda i=i: [ping(f"{i}-{j}") for j in range(10)])
        for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    steps = [event.step for event in spy.events]
    assert sorted(steps) == list(range(1, 81))
    assert runbound.current_session().step_count == 80


def test_exports():
    for name in (
        "init",
        "reset",
        "tool",
        "wrap",
        "current_session",
        "GuardrailTripped",
        "Anomaly",
        "Event",
        "GuardrailConfig",
        "SessionState",
        "__version__",
    ):
        assert hasattr(runbound, name), name
        assert name in runbound.__all__
