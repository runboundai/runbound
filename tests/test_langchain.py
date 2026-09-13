"""Tests for the LangChain callback handler.

``langchain_core`` is never installed for these tests: a minimal fake package
is injected into ``sys.modules``, which also proves the handler only needs the
shapes it documents.
"""

import logging
import sys
import types

import pytest

import runbound
from runbound import api
from runbound.events import Event
from runbound.exceptions import GuardrailTripped
from runbound.integrations import langchain as lc  # must import without langchain_core


# --- fakes ------------------------------------------------------------------


class EventRecorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self) -> None:
        self.events: list[Event] = []

    def check(self, state, event, config):
        self.events.append(event)
        return None


class FakeGeneration:
    """A ChatGeneration-shaped object carrying ``message.usage_metadata``."""

    def __init__(self, usage_metadata):
        self.message = types.SimpleNamespace(usage_metadata=usage_metadata)


class FakeLLMResult:
    """An LLMResult-shaped object."""

    def __init__(self, llm_output=None, generations=None):
        self.llm_output = llm_output
        self.generations = [] if generations is None else generations


class HostileResult:
    """Every attribute access blows up."""

    @property
    def llm_output(self):
        raise RuntimeError("no llm_output for you")

    @property
    def generations(self):
        raise RuntimeError("no generations for you")


class HostileSerialized(dict):
    """A ``serialized`` mapping whose lookup raises — simulates a handler bug."""

    def get(self, *args, **kwargs):
        raise RuntimeError("no name for you")


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine SDK and no cached class."""
    api._teardown_for_tests()
    lc._HANDLER_CLASS = None
    yield
    api._teardown_for_tests()
    lc._HANDLER_CLASS = None


@pytest.fixture
def base_handler_class(monkeypatch):
    """Inject a fake ``langchain_core`` and return its BaseCallbackHandler."""

    class BaseCallbackHandler:
        raise_error = False

    callbacks = types.ModuleType("langchain_core.callbacks")
    callbacks.BaseCallbackHandler = BaseCallbackHandler
    core = types.ModuleType("langchain_core")
    core.callbacks = callbacks

    monkeypatch.setitem(sys.modules, "langchain_core", core)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)
    return BaseCallbackHandler


@pytest.fixture
def handler(base_handler_class):
    return lc.GuardrailCallbackHandler()


def recorder() -> EventRecorder:
    """Attach an event recorder to the engine built by the last init()."""
    spy = EventRecorder()
    api._ENGINE.detectors.insert(0, spy)
    return spy


# --- lazy import ------------------------------------------------------------


def test_missing_langchain_core_raises_with_an_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "langchain_core", None)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", None)

    with pytest.raises(ImportError, match="pip install langchain-core"):
        _ = lc.GuardrailCallbackHandler


def test_unknown_module_attributes_still_raise_attribute_error():
    with pytest.raises(AttributeError):
        _ = lc.NoSuchThing


def test_the_handler_class_is_built_once_and_cached(base_handler_class):
    assert lc.GuardrailCallbackHandler is lc.GuardrailCallbackHandler


def test_handler_subclasses_the_langchain_base_and_propagates_errors(base_handler_class):
    handler_class = lc.GuardrailCallbackHandler

    assert issubclass(handler_class, base_handler_class)
    assert handler_class.raise_error is True


# --- on_tool_start ----------------------------------------------------------


def test_on_tool_start_emits_a_tool_call_event(handler):
    runbound.init()
    spy = recorder()

    handler.on_tool_start({"name": "search"}, "cats", run_id="run-1")

    (event,) = spy.events
    assert (event.kind, event.tool_name, event.step) == ("tool_call", "search", 1)
    assert len(event.args_hash) == 64


def test_on_tool_start_puts_the_tool_in_the_report_as_undecorated(handler):
    """A framework's tools carry no decorator, so the callback is where they

    become known — and the console shows them as unguarded, which they are."""
    runbound.init()

    handler.on_tool_start({"name": "search"}, "cats", run_id="run-1")

    assert runbound.tools() == [
        {
            "name": "search",
            "decorated": False,
            "params": [],
            "doc": None,
            "module": None,
            "rules": {},
        }
    ]


@pytest.mark.parametrize("serialized", [None, {}, {"name": ""}, "not-a-mapping"])
def test_tool_name_falls_back_when_serialized_carries_none(handler, serialized):
    runbound.init()
    spy = recorder()

    handler.on_tool_start(serialized, "cats")

    assert spy.events[0].tool_name == "<tool>"


def test_the_same_input_hashes_identically(handler):
    runbound.init()
    spy = recorder()

    handler.on_tool_start({"name": "search"}, "cats")
    handler.on_tool_start({"name": "search"}, "cats")

    assert spy.events[0].args_hash == spy.events[1].args_hash


def test_a_different_input_hashes_differently(handler):
    runbound.init()
    spy = recorder()

    handler.on_tool_start({"name": "search"}, "cats")
    handler.on_tool_start({"name": "search"}, "dogs")

    assert spy.events[0].args_hash != spy.events[1].args_hash


def test_repeated_identical_tool_calls_trip_the_loop_detector(handler):
    runbound.init(on_anomaly="raise", loop_threshold=3)

    handler.on_tool_start({"name": "search"}, "cats")
    handler.on_tool_start({"name": "search"}, "cats")

    with pytest.raises(GuardrailTripped) as excinfo:
        handler.on_tool_start({"name": "search"}, "cats")

    assert excinfo.value.anomaly.detector == "loop"


def test_distinct_inputs_never_trip_the_loop_detector(handler):
    runbound.init(on_anomaly="raise", loop_threshold=3)

    for query in ("cats", "dogs", "birds", "fish"):
        handler.on_tool_start({"name": "search"}, query)

    assert runbound.current_session().step_count == 4


# --- on_llm_end -------------------------------------------------------------


def test_on_llm_end_records_tokens_and_cost_from_llm_output(handler):
    runbound.init()
    spy = recorder()

    handler.on_llm_end(
        FakeLLMResult(
            llm_output={
                "token_usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                "model_name": "gpt-4o",
            }
        ),
        run_id="run-1",
    )

    (event,) = spy.events
    assert (event.kind, event.model) == ("llm_call", "gpt-4o")
    assert (event.tokens_in, event.tokens_out) == (1000, 500)
    assert event.cost_usd == pytest.approx(0.0075)

    session = runbound.current_session()
    assert session.total_tokens == 1500
    assert session.total_cost_usd == pytest.approx(0.0075)


def test_on_llm_end_falls_back_to_generation_usage_metadata(handler):
    runbound.init()
    spy = recorder()

    handler.on_llm_end(
        FakeLLMResult(
            llm_output=None,
            generations=[[FakeGeneration({"input_tokens": 7, "output_tokens": 3})]],
        )
    )

    (event,) = spy.events
    assert (event.tokens_in, event.tokens_out, event.model) == (7, 3, None)
    assert event.cost_usd == 0.0


def test_llm_output_without_usage_still_uses_the_generation_fallback(handler):
    runbound.init()
    spy = recorder()

    handler.on_llm_end(
        FakeLLMResult(
            llm_output={"model_name": "gpt-4o-mini"},
            generations=[[FakeGeneration({"input_tokens": 1000, "output_tokens": 1000})]],
        )
    )

    (event,) = spy.events
    assert (event.model, event.tokens_in, event.tokens_out) == ("gpt-4o-mini", 1000, 1000)
    assert event.cost_usd == pytest.approx(0.00075)


@pytest.mark.parametrize(
    "response",
    [
        FakeLLMResult(),
        FakeLLMResult(llm_output=None, generations=None),
        FakeLLMResult(llm_output={"token_usage": None, "model_name": None}),
        FakeLLMResult(generations=[[FakeGeneration(None)]]),
        FakeLLMResult(llm_output={"token_usage": {"prompt_tokens": "lots"}}),
        HostileResult(),
        None,
    ],
)
def test_unusable_usage_records_a_zero_token_step(handler, response):
    runbound.init()
    spy = recorder()

    handler.on_llm_end(response)

    (event,) = spy.events
    assert (event.kind, event.tokens_in, event.tokens_out) == ("llm_call", 0, 0)
    assert event.cost_usd == 0.0
    assert runbound.current_session().step_count == 1


def test_a_budget_trip_on_an_llm_call_reaches_the_chain(handler):
    runbound.init(on_anomaly="raise", budget_usd=0.001)

    with pytest.raises(GuardrailTripped) as excinfo:
        handler.on_llm_end(
            FakeLLMResult(
                llm_output={
                    "token_usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                    "model_name": "gpt-4o",
                }
            )
        )

    assert excinfo.value.anomaly.detector == "budget"


# --- errors -----------------------------------------------------------------


def test_on_tool_error_emits_a_tool_error_event(handler):
    runbound.init()
    spy = recorder()

    handler.on_tool_error(ValueError("kaboom"), name="search", run_id="run-1")

    (event,) = spy.events
    assert (event.kind, event.tool_name, event.error) == ("tool_error", "search", "kaboom")


def test_on_tool_error_reads_the_tool_name_from_serialized_too(handler):
    runbound.init()
    spy = recorder()

    handler.on_tool_error(ValueError("kaboom"), serialized={"name": "search"})
    handler.on_tool_error(ValueError("kaboom"))

    assert [event.tool_name for event in spy.events] == ["search", "<tool>"]


def test_on_tool_error_never_raises_even_when_it_trips(handler):
    runbound.init(on_anomaly="raise", max_steps=1)

    handler.on_tool_start({"name": "search"}, "cats")
    handler.on_tool_error(ValueError("kaboom"), name="search")  # step 2 > max_steps

    assert runbound.current_session().step_count == 2


def test_llm_and_chain_errors_are_only_logged(handler, caplog):
    caplog.set_level(logging.DEBUG, logger="runbound")
    runbound.init()
    spy = recorder()

    handler.on_llm_error(ValueError("rate limited"), run_id="run-1")
    handler.on_chain_error(ValueError("chain broke"), run_id="run-2")

    assert spy.events == []
    assert runbound.current_session().step_count == 0


# --- fail-open --------------------------------------------------------------


def test_a_bug_inside_a_handler_method_never_reaches_the_host(handler, caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()
    spy = recorder()

    assert handler.on_tool_start(HostileSerialized(), "cats") is None

    assert spy.events == []
    assert "on_tool_start" in caplog.text


def test_an_engine_failure_never_reaches_the_host(handler, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()

    def boom(session, event):
        raise RuntimeError("engine is broken")

    monkeypatch.setattr(api._ENGINE, "process", boom)

    handler.on_tool_start({"name": "search"}, "cats")
    handler.on_llm_end(FakeLLMResult())

    assert "runbound" in caplog.text


def test_the_handler_is_inert_before_init(handler):
    handler.on_tool_start({"name": "search"}, "cats")
    handler.on_llm_end(FakeLLMResult(llm_output={"token_usage": {"prompt_tokens": 10}}))
    handler.on_tool_error(ValueError("kaboom"), name="search")
    handler.on_llm_error(ValueError("kaboom"))
    handler.on_chain_error(ValueError("kaboom"))

    assert runbound.current_session() is None
