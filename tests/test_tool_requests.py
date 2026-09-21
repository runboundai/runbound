"""Tests for tool calls the *model* asked for, read off provider responses.

The loop a developer feels first is the one runbound cannot see through
``@tool``: the model keeps asking for ``search("weather")`` and the developer
dispatches those calls by hand. The wrappers read those requests off the
response and hand them to ``hooks.tool_request``, in their own ``"req:"`` hash
namespace, so executed tools and requested tools are never confused.

Nothing from openai/anthropic is imported here either — the fakes carry the
shapes the real SDKs return, including the dict-shaped responses that
OpenAI-compatible endpoints hand back.
"""

import asyncio

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped
from runbound.wrappers import (
    anthropic_wrapper,
    emit_tool_requests,
    openai_wrapper,
    request_hash,
)


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class RecordingHooks:
    """The wrapper-facing hooks interface, recorded instead of enforced."""

    def __init__(self, on_request=None) -> None:
        self.successes: list[str] = []
        self.errors: list[tuple] = []
        self.requests: list[tuple] = []
        self._on_request = on_request

    def before(self, provider):
        return None

    def success(self, provider):
        self.successes.append(provider)

    def error(self, model, exc, duration_s, provider):
        self.errors.append((model, exc, duration_s, provider))

    def tool_request(self, name, args_hash):
        self.requests.append((name, args_hash))
        if self._on_request is not None:
            self._on_request(name, args_hash)


class Obj:
    """An attribute bag; the shape every SDK response object has."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def chat_response(*tool_calls, model="gpt-4o"):
    """An OpenAI chat completion whose single choice asks for ``tool_calls``."""
    calls = [
        Obj(id=f"call_{i}", type="function", function=Obj(name=name, arguments=arguments))
        for i, (name, arguments) in enumerate(tool_calls)
    ]
    return Obj(
        model=model,
        usage=Obj(prompt_tokens=10, completion_tokens=5),
        choices=[Obj(index=0, message=Obj(content=None, tool_calls=calls))],
    )


def responses_response(*function_calls, model="gpt-4o"):
    """A Responses-API response whose output holds ``function_call`` items."""
    return Obj(
        model=model,
        usage=Obj(input_tokens=10, output_tokens=5),
        output=[Obj(type="message", content="thinking")]
        + [
            Obj(type="function_call", name=name, arguments=arguments)
            for name, arguments in function_calls
        ],
    )


def anthropic_response(*tool_uses, model="claude-sonnet-4-5"):
    """A messages response whose content holds ``tool_use`` blocks."""
    return Obj(
        model=model,
        usage=Obj(input_tokens=10, output_tokens=5),
        content=[Obj(type="text", text="thinking")]
        + [
            Obj(type="tool_use", id=f"tu_{i}", name=name, input=arguments)
            for i, (name, arguments) in enumerate(tool_uses)
        ],
    )


class FakeCreate:
    """A ``create`` that hands back canned responses, one per call."""

    def __init__(self, *responses, repeat=None):
        self.responses = list(responses)
        self.repeat = repeat
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if self.repeat is not None:
            return self.repeat
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class FakeStream:
    """Shaped like openai.Stream: iterator, context manager, closeable."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        return None


def openai_client(create, surface="chat"):
    """A duck-typed OpenAI client exposing one surface."""
    client = Obj()
    if surface == "chat":
        client.chat = Obj(completions=Obj(create=create))
    else:
        client.responses = Obj(create=create)
    return client


def anthropic_client(create):
    return Obj(messages=Obj(create=create))


def reports():
    """A ``report`` callback plus the list it appends to."""
    seen: list[tuple] = []

    def report(model, tokens_in, tokens_out, duration_s, tokens_reasoning):
        seen.append((model, tokens_in, tokens_out))

    return report, seen


# --- the hash ---------------------------------------------------------------


def test_request_hash_is_namespaced_and_deterministic():
    first = request_hash("search", '{"q": "weather"}')

    assert first.startswith("req:")
    assert first == request_hash("search", '{"q": "weather"}')
    assert len(first) == len("req:") + 64


def test_request_hash_separates_names_and_arguments():
    assert request_hash("search", "{}") != request_hash("email", "{}")
    assert request_hash("search", '{"q": "a"}') != request_hash("search", '{"q": "b"}')


def test_request_hashes_never_collide_with_executed_tool_hashes():
    """The two namespaces are what keeps requests and executions apart."""
    executed = api._args_hash("search", ("weather",), {})

    assert executed not in request_hash("search", '["weather"]')


def test_emit_tool_requests_is_fail_open_but_lets_a_trip_through():
    def explode(name, args_hash):
        raise RuntimeError("hooks are broken")

    hooks = RecordingHooks(on_request=explode)
    emit_tool_requests(hooks, [("a", "{}"), ("b", "{}")])

    assert [name for name, _ in hooks.requests] == ["a", "b"]  # both attempted

    tripping = RecordingHooks(
        on_request=lambda name, args_hash: (_ for _ in ()).throw(
            GuardrailTripped(runbound.Anomaly("loop", "critical", "looping", {}))
        )
    )
    with pytest.raises(GuardrailTripped):
        emit_tool_requests(tripping, [("a", "{}")])


# --- reading a response -----------------------------------------------------


def test_openai_chat_tool_calls_are_emitted():
    hooks = RecordingHooks()
    report, seen = reports()
    create = FakeCreate(chat_response(("search", '{"q": "weather"}')))
    client = openai_client(create)
    openai_wrapper.install(client, report, hooks)

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]
    assert seen == [("gpt-4o", 10, 5)]  # usage is reported as it always was
    assert hooks.successes == ["openai@default"]


def test_openai_chat_requests_are_emitted_after_the_usage_report():
    """Usage first, then success, then the requests: the call did succeed."""
    order: list[str] = []
    hooks = RecordingHooks(on_request=lambda name, args_hash: order.append("request"))
    hooks.success = lambda provider: order.append("success")  # type: ignore[method-assign]
    create = FakeCreate(chat_response(("search", "{}")))
    client = openai_client(create)
    openai_wrapper.install(client, lambda *a: order.append("report"), hooks)

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert order == ["report", "success", "request"]


def test_openai_chat_dict_shaped_response_is_read():
    """OpenAI-compatible endpoints often hand back plain dicts."""
    hooks = RecordingHooks()
    report, _ = reports()
    response = {
        "model": "llama3",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "choices": [
            {
                "index": 0,
                "message": {
                    "tool_calls": [{"function": {"name": "search", "arguments": '{"q": "x"}'}}]
                },
            }
        ],
    }
    client = openai_client(FakeCreate(response))
    openai_wrapper.install(client, report, hooks)

    client.chat.completions.create(model="llama3", messages=[])

    assert hooks.requests == [("search", request_hash("search", '{"q": "x"}'))]


def test_openai_chat_dict_arguments_are_serialized_stably():
    """Some endpoints send arguments as an object, not a JSON string."""
    hooks = RecordingHooks()
    report, _ = reports()
    client = openai_client(FakeCreate(chat_response(("search", {"b": 2, "a": 1}))))
    openai_wrapper.install(client, report, hooks)

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert hooks.requests == [("search", request_hash("search", '{"a": 1, "b": 2}'))]


def test_openai_chat_without_tool_calls_emits_nothing():
    hooks = RecordingHooks()
    report, seen = reports()
    client = openai_client(FakeCreate(chat_response()))
    openai_wrapper.install(client, report, hooks)

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert hooks.requests == []
    assert seen == [("gpt-4o", 10, 5)]


def test_openai_responses_function_calls_are_emitted():
    hooks = RecordingHooks()
    report, seen = reports()
    client = openai_client(
        FakeCreate(responses_response(("search", '{"q": "weather"}'), ("email", "{}"))),
        surface="responses",
    )
    openai_wrapper.install(client, report, hooks)

    client.responses.create(model="gpt-4o", input="hi")

    assert [name for name, _ in hooks.requests] == ["search", "email"]
    assert seen == [("gpt-4o", 10, 5)]


def test_anthropic_tool_use_blocks_are_emitted():
    hooks = RecordingHooks()
    report, seen = reports()
    client = anthropic_client(FakeCreate(anthropic_response(("search", {"q": "weather"}))))
    anthropic_wrapper.install(client, report, hooks)

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]
    assert seen == [("claude-sonnet-4-5", 10, 5)]
    assert hooks.successes == ["anthropic@default"]


def test_anthropic_input_key_order_does_not_change_the_hash():
    hooks = RecordingHooks()
    report, _ = reports()
    client = anthropic_client(
        FakeCreate(
            anthropic_response(("search", {"a": 1, "b": 2})),
            anthropic_response(("search", {"b": 2, "a": 1})),
        )
    )
    anthropic_wrapper.install(client, report, hooks)

    client.messages.create(model="claude-sonnet-4-5", messages=[])
    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert hooks.requests[0] == hooks.requests[1]


# --- streams ----------------------------------------------------------------


def _chat_tool_chunks(name, fragments):
    """Chat chunks that dribble one tool call's arguments out in pieces."""
    chunks = [
        Obj(
            model="gpt-4o",
            usage=None,
            choices=[
                Obj(
                    index=0,
                    delta=Obj(tool_calls=[Obj(index=0, function=Obj(name=name, arguments=""))]),
                )
            ],
        )
    ]
    for fragment in fragments:
        chunks.append(
            Obj(
                model="gpt-4o",
                usage=None,
                choices=[
                    Obj(
                        index=0,
                        delta=Obj(
                            tool_calls=[Obj(index=0, function=Obj(name=None, arguments=fragment))]
                        ),
                    )
                ],
            )
        )
    chunks.append(
        Obj(model="gpt-4o", usage=Obj(prompt_tokens=10, completion_tokens=5), choices=[])
    )
    return chunks


def test_openai_chat_stream_emits_one_request_with_joined_arguments():
    hooks = RecordingHooks()
    report, seen = reports()
    chunks = _chat_tool_chunks("search", ['{"q":', ' "wea', 'ther"}'])
    client = openai_client(FakeCreate(FakeStream(chunks)))
    openai_wrapper.install(client, report, hooks)

    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    assert len(list(stream)) == len(chunks)  # chunks pass through untouched

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]
    assert seen == [("gpt-4o", 10, 5)]


def test_an_abandoned_stream_emits_no_requests():
    hooks = RecordingHooks()
    report, _ = reports()
    chunks = _chat_tool_chunks("search", ['{"q": "weather"}'])
    client = openai_client(FakeCreate(FakeStream(chunks)))
    openai_wrapper.install(client, report, hooks)

    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    next(iter(stream))

    assert hooks.requests == []


def test_openai_responses_stream_reads_the_completed_event():
    hooks = RecordingHooks()
    report, _ = reports()
    chunks = [
        Obj(type="response.output_item.added"),
        Obj(
            type="response.completed",
            response=responses_response(("search", '{"q": "weather"}')),
        ),
    ]
    client = openai_client(FakeCreate(FakeStream(chunks)), surface="responses")
    openai_wrapper.install(client, report, hooks)

    list(client.responses.create(model="gpt-4o", input="hi", stream=True))

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]


def test_openai_responses_stream_falls_back_to_arguments_done_events():
    hooks = RecordingHooks()
    report, _ = reports()
    chunks = [
        Obj(type="response.function_call_arguments.delta", delta="{"),
        Obj(
            type="response.function_call_arguments.done",
            item_id="fc_1",
            name="search",
            arguments='{"q": "weather"}',
        ),
    ]
    client = openai_client(FakeCreate(FakeStream(chunks)), surface="responses")
    openai_wrapper.install(client, report, hooks)

    list(client.responses.create(model="gpt-4o", input="hi", stream=True))

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]


def test_anthropic_stream_assembles_tool_use_from_events():
    hooks = RecordingHooks()
    report, _ = reports()
    chunks = [
        Obj(
            type="message_start",
            message=Obj(model="claude-sonnet-4-5", usage=Obj(input_tokens=10, output_tokens=0)),
        ),
        Obj(type="content_block_start", index=0, content_block=Obj(type="text")),
        Obj(
            type="content_block_start",
            index=1,
            content_block=Obj(type="tool_use", name="search", input={}),
        ),
        Obj(
            type="content_block_delta",
            index=1,
            delta=Obj(type="input_json_delta", partial_json='{"q":'),
        ),
        Obj(
            type="content_block_delta",
            index=1,
            delta=Obj(type="input_json_delta", partial_json=' "weather"}'),
        ),
        Obj(type="message_delta", usage=Obj(output_tokens=5)),
    ]
    client = anthropic_client(FakeCreate(FakeStream(chunks)))
    anthropic_wrapper.install(client, report, hooks)

    list(client.messages.create(model="claude-sonnet-4-5", messages=[], stream=True))

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]


# --- fail-open --------------------------------------------------------------


class Hostile:
    """A response object that raises on every attribute access."""

    def __getattr__(self, name):
        raise RuntimeError(f"no {name} for you")


class Exploding:
    """Iterable in name only."""

    def __iter__(self):
        raise RuntimeError("boom")


class HostileChoices(Obj):
    """A well-formed response whose ``choices`` explode when iterated."""

    @property
    def choices(self):
        return Exploding()


def test_a_hostile_response_emits_nothing_and_still_reports_usage():
    hooks = RecordingHooks()
    report, seen = reports()
    response = HostileChoices(model="gpt-4o", usage=Obj(prompt_tokens=10, completion_tokens=5))
    client = openai_client(FakeCreate(response))
    openai_wrapper.install(client, report, hooks)

    assert client.chat.completions.create(model="gpt-4o", messages=[]) is response
    assert hooks.requests == []
    assert seen == [("gpt-4o", 10, 5)]
    assert hooks.successes == ["openai@default"]


def test_a_response_that_raises_on_everything_is_survivable():
    hooks = RecordingHooks()
    report, seen = reports()
    client = anthropic_client(FakeCreate(Hostile()))
    anthropic_wrapper.install(client, report, hooks)

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert hooks.requests == []
    assert seen == [("claude-sonnet-4-5", 0, 0)]


def test_a_hostile_stream_chunk_breaks_neither_usage_nor_the_consumer():
    hooks = RecordingHooks()
    report, seen = reports()
    chunks = [Hostile(), Obj(model="gpt-4o", usage=Obj(prompt_tokens=10, completion_tokens=5))]
    client = openai_client(FakeCreate(FakeStream(chunks)))
    openai_wrapper.install(client, report, hooks)

    assert len(list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))) == 2
    assert hooks.requests == []
    assert seen == [("gpt-4o", 10, 5)]


class OldHooks:
    """Hooks written before model-requested tool calls existed."""

    def __init__(self):
        self.successes = []

    def before(self, provider):
        return None

    def success(self, provider):
        self.successes.append(provider)

    def error(self, model, exc, duration_s, provider):
        return None


def test_hooks_that_predate_tool_requests_still_work():
    hooks = OldHooks()
    report, seen = reports()
    client = openai_client(FakeCreate(chat_response(("search", "{}"))))
    openai_wrapper.install(client, report, hooks)

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert hooks.successes == ["openai@default"]
    assert seen == [("gpt-4o", 10, 5)]


def test_a_wrapper_installed_without_hooks_is_silent():
    report, seen = reports()
    client = openai_client(FakeCreate(chat_response(("search", "{}"))))
    openai_wrapper.install(client, report)

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert seen == [("gpt-4o", 10, 5)]


# --- end to end -------------------------------------------------------------


def test_three_identical_model_requests_trip_the_loop_after_the_response():
    """No ``@tool`` anywhere: the model asking three times is the loop."""
    runbound.init(on_anomaly="raise", loop_threshold=3)
    create = FakeCreate(repeat=chat_response(("search", '{"q": "weather"}')))
    client = runbound.wrap(openai_client(create))

    client.chat.completions.create(model="gpt-4o", messages=[])
    client.chat.completions.create(model="gpt-4o", messages=[])
    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "loop"
    assert "model requested tool" in excinfo.value.anomaly.message
    assert create.calls == 3  # the third response came back; the dispatch is what we stop


def test_model_requests_do_not_count_as_executed_tool_calls():
    runbound.init(loop_threshold=3)
    create = FakeCreate(repeat=chat_response(("search", '{"q": "weather"}')))
    client = runbound.wrap(openai_client(create))

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert runbound.tool_calls() == {}


def test_requests_and_executions_of_the_same_call_never_merge():
    """Two requests plus two executions of the same action are two twos."""
    runbound.init(on_anomaly="raise", loop_threshold=3)

    @runbound.tool
    def search(query):
        return "ok"

    create = FakeCreate(repeat=chat_response(("search", '{"query": "weather"}')))
    client = runbound.wrap(openai_client(create))

    for _ in range(2):
        client.chat.completions.create(model="gpt-4o", messages=[])
        search("weather")

    assert runbound.is_tripped() is None
    assert runbound.tool_calls() == {"search": 2}


def test_requests_with_different_arguments_are_not_a_loop():
    runbound.init(on_anomaly="raise", loop_threshold=3)
    create = FakeCreate(
        chat_response(("search", '{"q": "a"}')),
        chat_response(("search", '{"q": "b"}')),
        chat_response(("search", '{"q": "c"}')),
    )
    client = runbound.wrap(openai_client(create))

    for _ in range(3):
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert runbound.is_tripped() is None


def test_two_requests_in_one_response_both_count():
    runbound.init(on_anomaly="raise", loop_threshold=3)
    create = FakeCreate(
        repeat=chat_response(("search", '{"q": "weather"}'), ("search", '{"q": "weather"}'))
    )
    client = runbound.wrap(openai_client(create))

    client.chat.completions.create(model="gpt-4o", messages=[])
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert create.calls == 2


def test_anthropic_requests_trip_the_loop_end_to_end():
    runbound.init(on_anomaly="raise", loop_threshold=3)
    create = FakeCreate(repeat=anthropic_response(("search", {"q": "weather"})))
    client = runbound.wrap(anthropic_client(create))

    client.messages.create(model="claude-sonnet-4-5", messages=[])
    client.messages.create(model="claude-sonnet-4-5", messages=[])
    with pytest.raises(GuardrailTripped) as excinfo:
        client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert excinfo.value.anomaly.details["tool_name"] == "search"
    assert excinfo.value.anomaly.details["args_hash"].startswith("req:")


# --- async ------------------------------------------------------------------


class FakeAsyncCreate(FakeCreate):
    """The same canned responses, behind an ``async def``."""

    async def __call__(self, **kwargs):
        return FakeCreate.__call__(self, **kwargs)


class FakeAsyncStream:
    """Shaped like openai.AsyncStream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


def test_an_async_response_emits_its_requests():
    hooks = RecordingHooks()
    report, seen = reports()
    client = openai_client(FakeAsyncCreate(chat_response(("search", '{"q": "weather"}'))))
    openai_wrapper.install(client, report, hooks)

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]
    assert seen == [("gpt-4o", 10, 5)]


def test_an_async_stream_emits_its_requests_at_the_end():
    hooks = RecordingHooks()
    report, _ = reports()
    chunks = _chat_tool_chunks("search", ['{"q": ', '"weather"}'])
    client = openai_client(FakeAsyncCreate(FakeAsyncStream(chunks)))
    openai_wrapper.install(client, report, hooks)

    async def drain():
        stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        seen = []
        async for chunk in stream:
            seen.append(chunk)
            assert hooks.requests == []  # nothing is emitted mid-stream
        return seen

    assert len(asyncio.run(drain())) == len(chunks)
    assert hooks.requests == [("search", request_hash("search", '{"q": "weather"}'))]
