"""Shared offline scaffolding for the docs examples. Not a snippet itself —
skipped by both the site's generator (leading underscore) and the test
runner's discovery (same rule).

Every docs example runs with no network and no API key
(tests/test_docs_examples.py enforces both). A handful of the examples still
want to show a real ``openai.OpenAI()`` / ``openai.AsyncOpenAI()`` client
being wrapped by the real runbound, because that is what the README
documents and what a reader will actually paste into their own code. This
module is how: it patches the two client classes so that, from the moment
:func:`patch_openai` runs, every client built with no explicit ``api_key`` /
``base_url`` / ``http_client`` gets ones that talk to an in-process
``httpx.MockTransport`` instead of the network — the same technique
tests/test_real_sdk.py uses to drive genuine provider SDKs without a wire.

Response bodies below copy the real shape from tests/test_real_sdk.py
(``usage`` fields included) rather than inventing one.
"""

from __future__ import annotations

import json
from typing import Any

CHAT_MODEL = "gpt-4o"
BASE_URL = "http://mock.local/v1"


class Transport:
    """A mock HTTP handler that counts how often it was actually reached.

    Serves a fixed sequence of responses in order, one per request; once the
    sequence is exhausted, the last response is repeated. That is enough for
    every example here, each of which drives a small, known number of calls
    from one process. An entry is either a JSON-able ``dict`` (status 200), an
    ``(status, dict)`` pair, a pre-built SSE body (``str``, status 200), or an
    ``(status, str)`` pair for a non-200 SSE response, or a callable taking the
    ``httpx.Request`` and returning any of those — for a server whose answer
    depends on what the request asked for, the way a real one's does.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = [self._normalize(r) for r in responses]
        self.calls = 0

    @staticmethod
    def _normalize(entry: Any) -> tuple[int, Any]:
        if isinstance(entry, tuple):
            return entry
        return (200, entry)

    def __call__(self, request: Any):
        import httpx

        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        status, body = self._responses[index]
        if callable(body):
            status, body = self._normalize(body(request))
        if isinstance(body, str):
            return httpx.Response(
                status, content=body, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(status, json=body)


def patch_openai(responses: list[Any], base_url: str = BASE_URL) -> Transport:
    """Make every ``OpenAI()`` / ``AsyncOpenAI()`` built from here on use a
    mock transport serving ``responses`` in order.

    Returns the :class:`Transport`, so an example can assert on ``.calls`` —
    "the request never left the process" is only provable by counting.
    Idempotent to call more than once in a process (each call re-patches with
    a fresh transport); each docs example is its own subprocess, so that
    never actually happens in practice.
    """
    import httpx
    import openai

    transport = Transport(responses)

    original_sync_init = openai.OpenAI.__init__

    def sync_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("api_key", "sk-docs")
        kwargs.setdefault("base_url", base_url)
        kwargs.setdefault("max_retries", 0)
        kwargs.setdefault("http_client", httpx.Client(transport=httpx.MockTransport(transport)))
        original_sync_init(self, *args, **kwargs)

    openai.OpenAI.__init__ = sync_init  # type: ignore[method-assign]

    original_async_init = openai.AsyncOpenAI.__init__

    def async_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("api_key", "sk-docs")
        kwargs.setdefault("base_url", base_url)
        kwargs.setdefault("max_retries", 0)
        kwargs.setdefault(
            "http_client", httpx.AsyncClient(transport=httpx.MockTransport(transport))
        )
        original_async_init(self, *args, **kwargs)

    openai.AsyncOpenAI.__init__ = async_init  # type: ignore[method-assign]

    return transport


def chat_completion(
    *,
    tokens_in: int = 100,
    tokens_out: int = 50,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    tool_calls: list | None = None,
    model: str = CHAT_MODEL,
    content: str = "hello",
) -> dict:
    """One ``chat.completion`` body, the real OpenAI shape."""
    message: dict = {"role": "assistant", "content": None if tool_calls else content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    usage: dict = {
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }
    if reasoning_tokens:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {
        "id": "chatcmpl-docs",
        "object": "chat.completion",
        "created": 1_756_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": usage,
    }


def weather_tool_call() -> list:
    """The same tool call, with the same arguments, every time it is asked."""
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]


def error_body(message: str, type_: str = "rate_limit_error", code: str = "rate_limit_exceeded") -> dict:
    """An OpenAI-shaped error body, for a non-200 :class:`Transport` entry."""
    return {"error": {"message": message, "type": type_, "code": code}}


def sse(chunks: list[dict]) -> str:
    """The wire format a streamed chat completion actually arrives in."""
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def stream_chunks(
    *, tokens_in: int | None = 11, tokens_out: int | None = 7, model: str = CHAT_MODEL
) -> list[dict]:
    """Two content chunks, then a usage-only chunk if ``tokens_in`` is given.

    ``tokens_in=None`` omits the usage chunk entirely — what a real server
    sends when the request carried no ``stream_options={"include_usage":
    True}``.
    """

    def chunk(**fields: Any) -> dict:
        return {
            "id": "chatcmpl-docs",
            "object": "chat.completion.chunk",
            "created": 1_756_000_000,
            "model": model,
            **fields,
        }

    chunks = [
        chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": "he"}}]),
        chunk(choices=[{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]),
    ]
    if tokens_in is not None:
        chunks.append(
            chunk(
                choices=[],
                usage={
                    "prompt_tokens": tokens_in,
                    "completion_tokens": tokens_out,
                    "total_tokens": tokens_in + (tokens_out or 0),
                },
            )
        )
    return chunks
