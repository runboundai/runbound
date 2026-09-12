"""``auto_wrap``: the provider SDKs guarded without a ``wrap()`` call.

Every client below is built **after** ``init()`` and is never wrapped. That is
the whole point: the customer who forgets ``wrap(client)`` — or never sees the
client, because a framework built it — is the customer this exists for. The
clients are real ``openai`` / ``anthropic`` objects over a mock transport, so
what is asserted is that runbound patched the shapes the SDKs actually have,
not the shapes a fake says they have.

Two things get their own tests because they are the ways this could quietly go
wrong: the endpoint label has to be read per *call* (one patched function
serves every client in the process), and a class patch and an instance patch
have to recognize each other's work in either order, or one call would be
counted twice.
"""

import asyncio
import contextlib
import importlib
import logging
import sys
import types

import pytest

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")

import runbound
from runbound import api, autowrap
from runbound.exceptions import GuardrailTripped
from runbound.wrappers import ClassTarget, anthropic_wrapper, openai_wrapper, resource_label
from test_real_sdk import (
    CHAT_MODEL,
    RATE_LIMITED,
    Transport,
    anthropic_client,
    anthropic_transport,
    chat,
    chat_completion,
    json_transport,
    message,
    messages_body,
    responses_body,
    sse,
    stream_chunks,
    weather_tool_call,
)


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def totals() -> tuple[int, float, int]:
    """``(tokens, cost, events)`` on the session work is accounted to."""
    state = runbound.current_session()
    with state.lock:
        return state.total_tokens, state.total_cost_usd, state.step_count


def openai_at(url: str, transport: Transport):
    """A real ``openai.OpenAI`` pointed at ``url``, over ``transport``."""
    return openai.OpenAI(
        api_key="test",
        base_url=url,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )


class _Blocker:
    """A meta-path finder that makes one package unimportable."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def find_spec(self, name, path=None, target=None):
        if name == self.prefix or name.startswith(self.prefix + "."):
            raise ImportError(f"{name} is blocked for this test")
        return None


@contextlib.contextmanager
def blocked(prefix: str):
    """Make ``prefix`` look like an SDK that is simply not installed.

    The real modules are put back exactly as they were — the same objects, so
    nothing else in the suite ends up holding a second copy of ``openai``.
    """
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    for name in saved:
        del sys.modules[name]
    finder = _Blocker(prefix)
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(saved)


# --- the shape we are patching ----------------------------------------------


def test_every_class_target_exists_and_its_resources_carry_a_client():
    """The assumption the whole module rests on, asserted rather than hoped.

    If an SDK upgrade moves ``create`` or renames ``_client``, this fails here
    with the target's name rather than silently guarding nothing.
    """
    for target in autowrap.TARGETS:
        module = importlib.import_module(target.module)
        cls = getattr(module, target.attr)
        assert callable(getattr(cls, "create", None)), target.label

    client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
    assert client.chat.completions._client is client
    assert client.responses._client is client


def test_a_resource_that_names_no_client_labels_as_the_default_endpoint():
    resolve = resource_label("openai")

    assert resolve(()) == "openai@default"
    assert resolve((object(),)) == "openai@default"


# --- calls counted with no wrap() -------------------------------------------


def test_a_chat_completion_is_counted_without_any_wrap_call():
    runbound.init()
    transport = json_transport(chat_completion())
    client = openai_at("http://mock.local/v1", transport)

    response = chat(client)

    assert response.usage.completion_tokens == 50  # the SDK object, untouched
    tokens, cost, events = totals()
    assert (tokens, events) == (150, 1)
    assert cost == pytest.approx(100 / 1e6 * 2.50 + 50 / 1e6 * 10.00)
    assert transport.calls == 1


def test_the_responses_api_is_auto_instrumented_too():
    runbound.init()
    client = openai_at("http://mock.local/v1", json_transport(responses_body()))

    client.responses.create(model=CHAT_MODEL, input="hi")

    assert totals()[0] == 260


def test_an_async_client_is_auto_instrumented():
    runbound.init()
    transport = json_transport(chat_completion())
    client = openai.AsyncOpenAI(
        api_key="test",
        base_url="http://mock.local/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )

    async def call():
        return await chat(client)

    response = asyncio.run(call())

    assert response.model == CHAT_MODEL
    assert totals()[:1] == (150,)
    assert asyncio.iscoroutinefunction(client.chat.completions.create)


def test_an_anthropic_messages_call_is_counted_without_any_wrap_call():
    runbound.init()
    client = anthropic_client(anthropic_transport(messages_body()))

    response = message(client)

    assert response.content[0].text == "hello"
    assert totals()[0] == 380


def test_a_streamed_call_through_the_class_patch_records_once_at_the_end():
    runbound.init()
    transport = Transport(
        httpx.Response(
            200, content=sse(stream_chunks()), headers={"content-type": "text/event-stream"}
        )
    )
    client = openai_at("http://mock.local/v1", transport)

    stream = chat(client, stream=True, stream_options={"include_usage": True})
    assert totals()[2] == 0  # nothing while the stream is open

    chunks = list(stream)

    assert len(chunks) == 3
    assert totals()[0] == 18
    assert totals()[2] == 1


def test_a_model_looping_on_one_tool_trips_with_nothing_wrapped():
    runbound.init(on_anomaly="raise")
    client = openai_at(
        "http://mock.local/v1", json_transport(chat_completion(tool_calls=weather_tool_call()))
    )

    chat(client)
    chat(client)
    with pytest.raises(GuardrailTripped) as excinfo:
        chat(client)

    assert excinfo.value.anomaly.detector == "loop"


def test_a_failed_call_reaches_the_caller_and_is_recorded_against_the_endpoint():
    runbound.init(circuit_failure_threshold=2)
    transport = json_transport(RATE_LIMITED, status=429, headers={"retry-after": "1"})
    client = openai_at("http://mock.local/v1", transport)

    for _ in range(2):
        with pytest.raises(openai.RateLimitError):
            chat(client)

    assert runbound.circuit_state("openai@mock.local") == "open"
    assert runbound.coverage()["guarded_calls"] == 2


# --- one patched function, many endpoints -----------------------------------


def test_the_endpoint_label_is_read_per_call_not_per_patch():
    """Two clients, two hosts, one patched ``create``: two circuits.

    A class patch is shared by every client in the process, so a label worked
    out once at patch time would put a dead self-hosted box and OpenAI proper
    on the same circuit — and refuse traffic to a provider that is perfectly
    healthy.
    """
    runbound.init(circuit_failure_threshold=2)
    failing = openai_at(
        "http://a.local/v1", json_transport(RATE_LIMITED, status=429, headers={"retry-after": "1"})
    )
    healthy = openai_at("http://b.local/v1", json_transport(chat_completion()))

    for _ in range(2):
        with pytest.raises(openai.RateLimitError):
            chat(failing)
    chat(healthy)

    assert runbound.circuit_state("openai@a.local") == "open"
    assert runbound.circuit_state("openai@b.local") == "closed"


# --- living alongside wrap() ------------------------------------------------


def test_wrap_is_a_no_op_on_a_fully_class_patched_client():
    runbound.init()
    transport = json_transport(chat_completion())
    client = openai_at("http://mock.local/v1", transport)

    assert openai_wrapper.is_wrapped(client)
    assert runbound.wrap(client) is client
    chat(client)

    assert totals()[2] == 1  # one call, one event
    assert runbound.coverage()["wrapped_clients"] == 0  # nothing left to wrap


def test_wrap_fills_the_gap_when_only_some_classes_are_patched():
    """A half-patched SDK degrades gracefully instead of double-counting."""
    runbound.init(auto_wrap=False)
    chat_target = openai_wrapper.CLASS_TARGETS[0]
    assert autowrap._patch(chat_target, api._record_llm_call, api._HOOKS)

    client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
    assert not openai_wrapper.is_wrapped(client)  # responses.create is not
    runbound.wrap(client)

    chat(client)

    assert totals()[2] == 1  # the class patch, not both patches


def test_a_client_wrapped_before_the_classes_were_patched_counts_once():
    runbound.init(auto_wrap=False)
    client = runbound.wrap(
        openai_at("http://mock.local/v1", json_transport(chat_completion()))
    )
    runbound.init()  # ...and now the classes are patched underneath it

    chat(client)

    assert totals()[2] == 1


# --- switching it off, and taking it off again ------------------------------


def test_auto_wrap_false_leaves_the_classes_untouched():
    runbound.init(auto_wrap=False)
    transport = json_transport(chat_completion())
    client = openai_at("http://mock.local/v1", transport)

    chat(client)

    assert autowrap.patched() == []
    assert totals()[2] == 0  # nothing was watching
    assert transport.calls == 1


def test_unpatch_restores_the_exact_original_functions():
    originals = {}
    for target in autowrap.TARGETS:
        module = importlib.import_module(target.module)
        originals[target.label] = getattr(module, target.attr).create

    runbound.init()
    assert autowrap.patched()
    runbound.unpatch()

    assert autowrap.patched() == []
    for target in autowrap.TARGETS:
        module = importlib.import_module(target.module)
        assert getattr(module, target.attr).create is originals[target.label], target.label


def test_calls_are_no_longer_counted_after_unpatch():
    runbound.init()
    client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
    runbound.unpatch()

    chat(client)

    assert totals()[2] == 0


def test_reset_does_not_unpatch():
    runbound.init()
    runbound.reset()

    assert "openai:chat" in autowrap.patched()

    client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
    chat(client)
    assert totals()[2] == 1


def test_a_second_init_patches_nothing_new_and_never_double_counts():
    runbound.init()
    first = autowrap.patched()
    runbound.init()

    assert autowrap.patched() == first

    client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
    chat(client)
    assert totals()[2] == 1


# --- what it says about itself ----------------------------------------------


def test_the_first_init_says_what_it_instrumented_and_the_second_says_nothing(caplog):
    with caplog.at_level(logging.INFO, logger="runbound"):
        runbound.init()
        first = [record.getMessage() for record in caplog.records]
        caplog.clear()
        runbound.init()
        second = [record.getMessage() for record in caplog.records]

    assert (
        "runbound: auto-instrumented openai (chat, chat.async, responses, "
        "responses.async) and anthropic (messages, messages.async)" in first
    )
    assert not [line for line in second if "auto-instrumented" in line]


def test_no_supported_sdk_is_said_once_at_info(caplog):
    with blocked("openai"), blocked("anthropic"):
        with caplog.at_level(logging.INFO, logger="runbound"):
            runbound.init()

        assert autowrap.patched() == []

    assert [
        line
        for line in (record.getMessage() for record in caplog.records)
        if line == (
            "runbound: auto_wrap: no supported provider SDK found — call "
            "runbound.wrap(client) or use @runbound.llm"
        )
    ]


# --- fail-open --------------------------------------------------------------


def test_a_provider_that_cannot_be_imported_costs_the_other_provider_nothing():
    with blocked("anthropic"):
        runbound.init()

        labels = autowrap.patched()
        assert "openai:chat" in labels
        assert not [label for label in labels if label.startswith("anthropic")]

        client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
        chat(client)
        assert totals()[2] == 1


def test_a_target_that_refuses_to_be_patched_costs_only_itself(monkeypatch, caplog):
    module = types.ModuleType("runbound_fake_sdk")

    class Thing:
        def create(self, **kwargs):
            return "ok"

    module.Thing = Thing
    monkeypatch.setitem(sys.modules, "runbound_fake_sdk", module)

    def boom(*args, **kwargs):
        raise RuntimeError("this guard refuses to be built")

    bad = ClassTarget("fake:boom", "runbound_fake_sdk", "Thing", boom)
    monkeypatch.setattr(autowrap, "TARGETS", (bad,) + openai_wrapper.CLASS_TARGETS)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        runbound.init()

    assert "fake:boom" not in autowrap.patched()
    assert "openai:chat" in autowrap.patched()
    assert Thing().create() == "ok"  # left exactly as it was

    client = openai_at("http://mock.local/v1", json_transport(chat_completion()))
    chat(client)
    assert totals()[2] == 1


def test_a_target_in_a_module_that_does_not_exist_is_quiet(monkeypatch):
    bad = ClassTarget("nowhere:thing", "runbound_no_such_module", "Thing", lambda *a: None)
    monkeypatch.setattr(autowrap, "TARGETS", (bad,) + anthropic_wrapper.CLASS_TARGETS)

    runbound.init()

    assert "nowhere:thing" not in autowrap.patched()
    assert "anthropic:messages" in autowrap.patched()


def test_a_target_whose_class_has_moved_is_skipped(monkeypatch):
    bad = ClassTarget("openai:gone", "openai.resources.chat.completions", "Gone", lambda *a: None)
    monkeypatch.setattr(autowrap, "TARGETS", (bad,) + openai_wrapper.CLASS_TARGETS)

    runbound.init()

    assert "openai:gone" not in autowrap.patched()
    assert "openai:chat" in autowrap.patched()


def test_unpatch_never_raises_when_a_class_refuses_to_be_restored(caplog):
    """One stubborn class must not leave every other one patched forever."""
    runbound.init()
    autowrap._PATCHED["fake:frozen"] = (int, lambda: None, True)  # int rejects setattr

    with caplog.at_level(logging.WARNING, logger="runbound"):
        runbound.unpatch()

    assert autowrap.patched() == []
    assert autowrap._PATCHED == {}
    assert [line for line in (r.getMessage() for r in caplog.records) if "fake:frozen" in line]
