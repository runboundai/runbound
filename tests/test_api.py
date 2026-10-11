"""Tests for the public API: init/reset/current_session, @tool, fail-open."""

import logging

import pytest

import runbound
from runbound import api
from runbound.events import Event
from runbound.exceptions import GuardrailTripped


class EventRecorder:
    """An observer that records every event the engine hands it."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def on_event(self, session, event) -> None:
        if event.kind == "tool_result":  # the call-side events are what these tests are about
            return
        self.events.append(event)

    def on_anomaly(self, session, anomaly, reacted) -> None:
        pass


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def recorder() -> EventRecorder:
    """Attach an event recorder to the engine built by the last init()."""
    spy = EventRecorder()
    api._ENGINE.observers.append(spy)
    return spy


# --- init / reset / current_session -----------------------------------------


def test_init_validates_config_loudly():
    with pytest.raises(ValueError):
        runbound.init(on_halt="explode")
    with pytest.raises(ValueError):
        runbound.init(max_actions_per_run=-1)

    assert runbound.current_session() is None


def test_init_rejects_unknown_options():
    with pytest.raises(TypeError):
        runbound.init(budget_usd=5)


def test_init_creates_a_session_and_reconfigures_on_second_call():
    runbound.init(max_actions_per_run=10)
    first = runbound.current_session()
    assert first is not None
    assert first.session_id

    runbound.init(max_actions_per_run=20)
    second = runbound.current_session()

    assert second is not first
    assert second.session_id != first.session_id
    assert api._ENGINE.config.max_actions_per_run == 20


def test_reset_starts_a_fresh_session_but_keeps_the_engine():
    runbound.init(max_actions_per_run=10)
    engine = api._ENGINE
    old = runbound.current_session()

    @runbound.tool
    def ping():
        return "pong"

    ping()
    assert old.event_count == 1

    runbound.reset()
    new = runbound.current_session()

    assert api._ENGINE is engine
    assert new.session_id != old.session_id
    assert (new.event_count, new.tool_calls, new.executed_actions) == (0, {}, 0)


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


def test_a_refused_call_never_runs_the_tool_body():
    runbound.init()
    calls: list[str] = []

    @runbound.tool(max_calls=2)
    def search(query):
        calls.append(query)
        return "ok"

    search("cats")
    search("cats")

    with pytest.raises(GuardrailTripped) as excinfo:
        search("cats")

    assert excinfo.value.anomaly.detector == "policy"
    assert calls == ["cats", "cats"]  # the refused call never ran the tool


def test_the_action_cap_trips_through_the_decorator():
    runbound.init(max_actions_per_run=2)

    @runbound.tool
    def ping(n):
        return n

    ping(1)
    ping(2)

    with pytest.raises(GuardrailTripped) as excinfo:
        ping(3)

    assert excinfo.value.anomaly.detector == "fanout"


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
    assert spy.events[0].args_hash is None  # unhashable => no hash, no crash


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
    assert runbound.current_session().event_count == 80


def test_exports():
    for name in (
        "init",
        "reset",
        "tool",
        "current_session",
        "GuardrailTripped",
        "ExecutionRefused",
        "ToolPolicy",
        "Anomaly",
        "Event",
        "RunboundConfig",
        "SessionState",
        "__version__",
    ):
        assert hasattr(runbound, name), name
        assert name in runbound.__all__
