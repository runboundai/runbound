"""Streaming a chat completion without giving up on usage. Shown on: Production checklist, Streams and async."""

import json

import _offline

REQUIRES = ("openai", "httpx")


def openai_stream(request):
    """What OpenAI sends: a usage chunk only when the request asked for one."""
    asked = json.loads(request.content).get("stream_options", {}).get("include_usage")
    return _offline.sse(_offline.stream_chunks(tokens_in=11 if asked else None, tokens_out=7))


transport = _offline.patch_openai([openai_stream])

import runbound
from openai import OpenAI

runbound.init()
client = OpenAI()

# docs: openai-stream-usage
with client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hi"}],
    stream=True,
    stream_options={"include_usage": True},
) as stream:
    for _ in stream:
        pass
# /docs

session = runbound.current_session()
with session.lock:
    tokens_with_usage = session.total_tokens
    cost_with_usage = session.total_cost_usd

assert tokens_with_usage == 18, tokens_with_usage
assert cost_with_usage > 0.0

# The README's "free traffic" warning, proven: the exact same call, without
# stream_options, is served by a real endpoint that sends no usage at all —
# and is recorded as zero tokens rather than estimated.
runbound.reset()
for _ in client.chat.completions.create(
    model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
):
    pass

session = runbound.current_session()
with session.lock:
    tokens_without_usage = session.total_tokens

assert tokens_without_usage == 0, tokens_without_usage
