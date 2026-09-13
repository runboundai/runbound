"""runbound against the *real* openai and anthropic SDKs.

Every other test in this suite duck-types a client, which is the right way to
test the wrappers: runbound imports neither package and must not care. What
those tests cannot catch is the shape drifting underneath us — a usage field
renamed, a resource object that stops accepting a patched ``create``, an
exception class that no longer carries ``status_code``. So these tests build
genuine ``openai.OpenAI`` / ``anthropic.Anthropic`` clients and put a mock HTTP
transport underneath them: the SDK does its own parsing, validation, retrying
and error raising, and runbound sees exactly what it would see in production.

No network and no API key: every response below is JSON written by hand and
handed back by an ``httpx``/``httpx2`` ``MockTransport``. The handler counts its
own invocations, which is how "the circuit refused this call before it went
out" is asserted rather than assumed.

Skipped cleanly wherever the SDKs are not installed — runbound's own runtime
dependencies are still exactly none.
"""

import asyncio
import json

import pytest

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")

try:  # anthropic 1.x validates its http_client's type and ships on httpx2
    import httpx2 as anthropic_httpx
except ImportError:  # pragma: no cover - older anthropic releases used httpx
    anthropic_httpx = httpx

import runbound
from runbound import api
from runbound.exceptions import CircuitOpen, GuardrailTripped

OPENAI_BASE_URL = "http://mock.local/v1"
ANTHROPIC_BASE_URL = "http://mock.local"

CHAT_MODEL = "gpt-4o"  # $2.50 / $10.00 per 1M tokens in runbound's table
CLAUDE_MODEL = "claude-sonnet-4-5"  # $3.00 / $15.00 per 1M tokens


# --- responses the mock transport hands back --------------------------------


def chat_completion(tool_calls: list | None = None) -> dict:
    """A ``chat.completion`` body: 100 in, 50 out, 20 of them reasoning."""
    message: dict = {"role": "assistant", "content": None if tool_calls else "hello"}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1_756_000_000,
        "model": CHAT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
    }


def chat_completion_with_cache() -> dict:
    """A ``chat.completion`` body: 1200 in (1000 cached), 50 out (T139)."""
    body = chat_completion()
    body["usage"] = {
        "prompt_tokens": 1200,
        "completion_tokens": 50,
        "total_tokens": 1250,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }
    return body


def weather_tool_call() -> list:
    """The same tool call, with the same arguments, every time it is asked."""
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]


def responses_body() -> dict:
    """A Responses-API body: 200 in, 60 out, one function call in ``output``."""
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_756_000_000,
        "model": CHAT_MODEL,
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 200,
            "output_tokens": 60,
            "total_tokens": 260,
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }


def responses_body_with_cache() -> dict:
    """A Responses-API body: 1200 in (1000 cached), 60 out (T139)."""
    body = responses_body()
    body["usage"] = {
        "input_tokens": 1200,
        "output_tokens": 60,
        "total_tokens": 1260,
        "input_tokens_details": {"cached_tokens": 1000},
    }
    return body


def sse(chunks: list) -> str:
    """The wire format a streamed chat completion actually arrives in."""
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def stream_chunks() -> list:
    """Two content chunks, then the usage-only chunk include_usage adds."""
    def chunk(**fields):
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1_756_000_000,
            "model": CHAT_MODEL,
            **fields,
        }

    return [
        chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": "he"}}]),
        chunk(choices=[{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]),
        chunk(
            choices=[],
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        ),
    ]


def stream_chunks_with_cache() -> list:
    """Same shape as :func:`stream_chunks`, with the final usage chunk cached (T139)."""
    def chunk(**fields):
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1_756_000_000,
            "model": CHAT_MODEL,
            **fields,
        }

    return [
        chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": "he"}}]),
        chunk(choices=[{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]),
        chunk(
            choices=[],
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 50,
                "total_tokens": 1250,
                "prompt_tokens_details": {"cached_tokens": 1000},
            },
        ),
    ]


def messages_body(tool_use: bool = False) -> dict:
    """An Anthropic ``message``: 300 in, 80 out, optionally one ``tool_use``."""
    content: list = [{"type": "text", "text": "hello"}]
    if tool_use:
        content.append(
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_weather",
                "input": {"city": "Paris"},
            }
        )
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": CLAUDE_MODEL,
        "content": content,
        "stop_reason": "tool_use" if tool_use else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 300, "output_tokens": 80},
    }


def messages_body_with_cache(cache_creation: int = 0) -> dict:
    """An Anthropic ``message``: 200 fresh + 1000 cache-read input, 80 out (T139).

    ``cache_creation`` optionally adds ``cache_creation_input_tokens`` — a
    cache *write*, additive to ``input_tokens`` just like a read, but billed
    at a premium rather than a discount.
    """
    body = messages_body()
    body["usage"] = {
        "input_tokens": 200,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": cache_creation,
        "output_tokens": 80,
    }
    return body


RATE_LIMITED = {
    "error": {
        "message": "Rate limit reached for gpt-4o",
        "type": "rate_limit_error",
        "code": "rate_limit_exceeded",
    }
}

OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}


# --- the transport ----------------------------------------------------------


class Transport:
    """A mock HTTP handler that counts how often it was actually reached.

    The count is the whole point of several tests below: a circuit that refuses
    a call before it goes out is only proven by the request never arriving.
    """

    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0
        self.urls: list[str] = []

    def __call__(self, request):
        self.calls += 1
        self.urls.append(str(request.url))
        return self.response


def json_transport(body: dict, status: int = 200, headers: dict | None = None) -> Transport:
    """A transport answering every request with one JSON body."""
    return Transport(httpx.Response(status, json=body, headers=headers or {}))


def openai_client(transport: Transport, **kwargs):
    """A real ``openai.OpenAI`` whose HTTP layer is ``transport``."""
    return openai.OpenAI(
        api_key="test",
        base_url=OPENAI_BASE_URL,
        max_retries=0,  # one create() must mean exactly one request
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        **kwargs,
    )


def async_openai_client(transport: Transport, **kwargs):
    """A real ``openai.AsyncOpenAI`` whose HTTP layer is ``transport``."""
    return openai.AsyncOpenAI(
        api_key="test",
        base_url=OPENAI_BASE_URL,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        **kwargs,
    )


def anthropic_client(transport: Transport, **kwargs):
    """A real ``anthropic.Anthropic`` whose HTTP layer is ``transport``."""
    return anthropic.Anthropic(
        api_key="test",
        base_url=ANTHROPIC_BASE_URL,
        max_retries=0,
        http_client=anthropic_httpx.Client(
            transport=anthropic_httpx.MockTransport(transport)
        ),
        **kwargs,
    )


def anthropic_transport(body: dict, status: int = 200) -> Transport:
    """A transport answering with one JSON body, on anthropic's httpx."""
    return Transport(anthropic_httpx.Response(status, json=body))


def chat(client, **kwargs):
    """One ordinary chat completion request."""
    return client.chat.completions.create(
        model=CHAT_MODEL, messages=[{"role": "user", "content": "hi"}], **kwargs
    )


def message(client, **kwargs):
    """One ordinary Anthropic messages request."""
    return client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
        **kwargs,
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


def cached_tokens() -> int:
    """The session's running total of cached input tokens (T139)."""
    state = runbound.current_session()
    with state.lock:
        return state.tokens_cached_in


# --- openai: chat completions -----------------------------------------------


def test_a_real_chat_completion_is_counted_and_priced():
    transport = json_transport(chat_completion())
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    response = chat(client)

    assert response.usage.completion_tokens == 50  # the SDK object, untouched
    tokens, cost, events = totals()
    assert (tokens, events) == (150, 1)
    assert cost == pytest.approx(100 / 1e6 * 2.50 + 50 / 1e6 * 10.00)
    assert transport.calls == 1


def test_a_cached_heavy_chat_completion_is_priced_at_the_discount():
    """T139: 1000 of 1200 input tokens are a cache hit, read off the real
    OpenAI SDK's ``usage.prompt_tokens_details.cached_tokens``."""
    transport = json_transport(chat_completion_with_cache())
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    response = chat(client)

    assert response.usage.prompt_tokens_details.cached_tokens == 1000
    tokens, cost, events = totals()
    assert (tokens, events) == (1250, 1)
    assert cached_tokens() == 1000
    price_in, price_out, price_cached_in = 2.50, 10.00, 1.25
    expected = (200 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (50 / 1e6) * price_out
    assert cost == pytest.approx(expected)
    assert cost < (1200 / 1e6) * price_in + (50 / 1e6) * price_out  # cheaper than no discount


def test_wrapping_a_real_client_returns_the_same_client():
    transport = json_transport(chat_completion())
    client = openai_client(transport)

    assert runbound.wrap(client) is client
    assert runbound.wrap(client) is client  # a second wrap is a no-op

    runbound.init()
    chat(client)
    assert totals()[2] == 1  # not double-counted


def test_a_response_the_sdk_returns_without_usage_is_handed_back_unharmed():
    body = chat_completion()
    del body["usage"]
    transport = json_transport(body)
    client = runbound.wrap(openai_client(transport))
    runbound.init(budget_usd=1.0)

    response = chat(client)

    assert response.choices[0].message.content == "hello"
    assert totals()[:2] == (0, 0.0)  # counted as nothing, never an error


def test_the_responses_api_is_guarded_too():
    transport = json_transport(responses_body())
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    response = client.responses.create(model=CHAT_MODEL, input="hi")

    assert response.status == "completed"
    tokens, cost, events = totals()
    assert (tokens, events) == (260, 1)
    assert cost == pytest.approx(200 / 1e6 * 2.50 + 60 / 1e6 * 10.00)


def test_the_responses_api_reads_its_own_cached_tokens_field():
    """T139: the Responses API names the details object differently
    (``input_tokens_details``, not ``prompt_tokens_details``)."""
    transport = json_transport(responses_body_with_cache())
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    response = client.responses.create(model=CHAT_MODEL, input="hi")

    assert response.usage.input_tokens_details.cached_tokens == 1000
    tokens, cost, events = totals()
    assert (tokens, events) == (1260, 1)
    assert cached_tokens() == 1000
    expected = (200 / 1e6) * 2.50 + (1000 / 1e6) * 1.25 + (60 / 1e6) * 10.00
    assert cost == pytest.approx(expected)


def test_a_streamed_call_records_its_usage_once_when_the_stream_ends():
    transport = Transport(
        httpx.Response(
            200, content=sse(stream_chunks()), headers={"content-type": "text/event-stream"}
        )
    )
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    stream = chat(client, stream=True, stream_options={"include_usage": True})
    assert totals()[2] == 0  # nothing is recorded while the stream is open

    chunks = list(stream)

    assert len(chunks) == 3
    assert totals() == (18, pytest.approx(11 / 1e6 * 2.50 + 7 / 1e6 * 10.00), 1)


def test_a_streamed_calls_cached_tokens_are_read_off_the_final_usage_chunk():
    transport = Transport(
        httpx.Response(
            200,
            content=sse(stream_chunks_with_cache()),
            headers={"content-type": "text/event-stream"},
        )
    )
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    list(chat(client, stream=True, stream_options={"include_usage": True}))

    tokens, cost, events = totals()
    assert (tokens, events) == (1250, 1)
    assert cached_tokens() == 1000
    expected = (200 / 1e6) * 2.50 + (1000 / 1e6) * 1.25 + (50 / 1e6) * 10.00
    assert cost == pytest.approx(expected)


# --- openai: failures and the circuit ---------------------------------------


def test_a_429_reaches_the_caller_as_the_sdks_own_rate_limit_error():
    transport = json_transport(RATE_LIMITED, status=429, headers={"retry-after": "1"})
    client = runbound.wrap(openai_client(transport))
    runbound.init()

    with pytest.raises(openai.RateLimitError) as excinfo:
        chat(client)

    assert excinfo.value.status_code == 429
    assert transport.calls == 1


def test_an_open_circuit_refuses_the_next_call_before_it_goes_out():
    transport = json_transport(RATE_LIMITED, status=429, headers={"retry-after": "1"})
    client = runbound.wrap(openai_client(transport))
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)

    for _ in range(2):
        with pytest.raises(openai.RateLimitError):
            chat(client)
    assert transport.calls == 2
    assert runbound.circuit_state("openai") == "open"

    with pytest.raises(CircuitOpen) as excinfo:
        chat(client)

    assert excinfo.value.provider == "openai@mock.local"
    assert transport.calls == 2  # the third request never left the process


def test_under_the_default_an_open_circuit_still_lets_the_call_through():
    transport = json_transport(RATE_LIMITED, status=429, headers={"retry-after": "1"})
    client = runbound.wrap(openai_client(transport))
    runbound.init(circuit_failure_threshold=2)  # on_provider_failure="notify"

    for _ in range(3):
        with pytest.raises(openai.RateLimitError):
            chat(client)

    assert runbound.circuit_state("openai") == "open"  # counted and reported
    assert transport.calls == 3  # ...and nobody's traffic was refused


def test_a_400_from_the_sdk_never_opens_the_circuit():
    """A bad request is our bug, not the provider's."""
    transport = json_transport(
        {"error": {"message": "bad request", "type": "invalid_request_error"}}, status=400
    )
    client = runbound.wrap(openai_client(transport))
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)

    for _ in range(3):
        with pytest.raises(openai.BadRequestError):
            chat(client)

    assert runbound.circuit_state("openai") == "closed"
    assert transport.calls == 3


# --- openai: the model asking for the same tool over and over ---------------


def test_a_model_looping_on_one_tool_trips_without_any_tool_decorator():
    transport = json_transport(chat_completion(tool_calls=weather_tool_call()))
    client = runbound.wrap(openai_client(transport))
    runbound.init(on_anomaly="raise")

    chat(client)
    chat(client)
    with pytest.raises(GuardrailTripped) as excinfo:
        chat(client)

    anomaly = excinfo.value.anomaly
    assert anomaly.detector == "loop"
    assert "model requested tool 'get_weather'" in anomaly.message
    assert transport.calls == 3  # the trip lands on the response, not before it


def test_a_responses_api_function_call_loops_the_same_way():
    body = responses_body()
    body["output"].append(
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city": "Paris"}',
            "status": "completed",
        }
    )
    transport = json_transport(body)
    client = runbound.wrap(openai_client(transport))
    runbound.init(on_anomaly="raise")

    client.responses.create(model=CHAT_MODEL, input="hi")
    client.responses.create(model=CHAT_MODEL, input="hi")
    with pytest.raises(GuardrailTripped) as excinfo:
        client.responses.create(model=CHAT_MODEL, input="hi")

    assert excinfo.value.anomaly.detector == "loop"


# --- openai: async ----------------------------------------------------------


def test_a_real_async_client_is_guarded_the_same_way():
    transport = json_transport(chat_completion())
    client = runbound.wrap(async_openai_client(transport))
    runbound.init()

    async def call():
        response = await chat(client)
        assert response.model == CHAT_MODEL

    asyncio.run(call())

    assert totals()[:2] == (150, pytest.approx(0.00075))


def test_an_async_client_keeps_its_async_signature():
    transport = json_transport(chat_completion())
    client = runbound.wrap(async_openai_client(transport))
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)

    assert asyncio.iscoroutinefunction(client.chat.completions.create)


# --- anthropic --------------------------------------------------------------


def test_a_real_messages_call_is_counted_and_priced():
    transport = anthropic_transport(messages_body())
    client = runbound.wrap(anthropic_client(transport))
    runbound.init()

    response = message(client)

    assert response.content[0].text == "hello"
    tokens, cost, events = totals()
    assert (tokens, events) == (380, 1)
    assert cost == pytest.approx(300 / 1e6 * 3.00 + 80 / 1e6 * 15.00)


def test_a_cached_heavy_messages_call_is_priced_at_the_discount():
    """T139: Anthropic's ``input_tokens`` EXCLUDES cache fields, so the real
    SDK's ``usage.cache_read_input_tokens`` must be folded into the total,
    not just read for the discount."""
    transport = anthropic_transport(messages_body_with_cache())
    client = runbound.wrap(anthropic_client(transport))
    runbound.init()

    response = message(client)

    assert response.usage.cache_read_input_tokens == 1000
    tokens, cost, events = totals()
    assert (tokens, events) == (1280, 1)  # 200 fresh + 1000 cached + 80 out
    assert cached_tokens() == 1000
    price_in, price_out, price_cached_in = 3.00, 15.00, 0.30
    expected = (200 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (80 / 1e6) * price_out
    assert cost == pytest.approx(expected)
    assert cost < (1200 / 1e6) * price_in + (80 / 1e6) * price_out  # cheaper than no discount


def test_a_cache_write_is_priced_at_its_125_percent_premium():
    """T139 (revised): a cache write must never be under-priced.

    Under-counting a cache *read* discount trips a customer's wall early —
    annoying, but safe (they are stopped before their money is gone).
    Under-counting a *write* premium is the opposite: the wall fires late,
    after they have spent past a budget they set, which is the one direction
    this product cannot afford. So the write gets its own rate
    (`PRICES[model][3]`, 125% of the input rate) rather than being folded in
    at the plain input rate.

    This is also the coordinator's requested example: one call mixing a
    cache read and a cache write, so both rates are visible together —
    200 fresh + 1000 cache-read + 300 cache-write input tokens, 80 output.
    """
    transport = anthropic_transport(messages_body_with_cache(cache_creation=300))
    client = runbound.wrap(anthropic_client(transport))
    runbound.init()

    response = message(client)

    assert response.usage.cache_creation_input_tokens == 300
    tokens, cost, events = totals()
    assert (tokens, events) == (1580, 1)  # 200 fresh + 1000 read + 300 write + 80 out
    assert cached_tokens() == 1000  # the write never counts as a "cached" (discounted) token
    price_in, price_out, price_cached_in, price_write_in = 3.00, 15.00, 0.30, 3.75
    expected = (
        (200 / 1e6) * price_in  # fresh
        + (300 / 1e6) * price_write_in  # cache write: its own 125% premium
        + (1000 / 1e6) * price_cached_in  # cache read: the discount
        + (80 / 1e6) * price_out
    )
    assert cost == pytest.approx(expected)
    # The premium must actually bite: cheaper treatments would be a regression.
    priced_as_plain_input = (
        (500 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (80 / 1e6) * price_out
    )
    priced_as_a_cached_read = (
        (200 / 1e6) * price_in + (1300 / 1e6) * price_cached_in + (80 / 1e6) * price_out
    )
    assert cost > priced_as_plain_input
    assert cost > priced_as_a_cached_read


def test_repeated_tool_use_blocks_trip_the_loop():
    transport = anthropic_transport(messages_body(tool_use=True))
    client = runbound.wrap(anthropic_client(transport))
    runbound.init(on_anomaly="raise")

    message(client)
    message(client)
    with pytest.raises(GuardrailTripped) as excinfo:
        message(client)

    anomaly = excinfo.value.anomaly
    assert anomaly.detector == "loop"
    assert anomaly.details["tool_name"] == "get_weather"
    assert transport.calls == 3


def test_an_overloaded_anthropic_opens_its_own_circuit():
    transport = anthropic_transport(OVERLOADED, status=529)
    client = runbound.wrap(anthropic_client(transport))
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)

    for _ in range(2):
        with pytest.raises(anthropic.APIStatusError):
            message(client)

    assert runbound.circuit_state("anthropic") == "open"
    assert runbound.circuit_state("openai") == "closed"  # one provider, one circuit

    with pytest.raises(CircuitOpen) as excinfo:
        message(client)

    assert excinfo.value.provider == "anthropic@mock.local"
    assert transport.calls == 2


def test_a_budget_stops_the_second_real_messages_call():
    transport = anthropic_transport(messages_body())
    client = runbound.wrap(anthropic_client(transport))
    runbound.init(budget_usd=0.003, on_anomaly="raise")

    message(client)  # $0.0021 spent, under the cap

    with pytest.raises(GuardrailTripped) as excinfo:
        message(client)

    assert excinfo.value.anomaly.detector == "budget"
    assert runbound.is_tripped().detector == "budget"
