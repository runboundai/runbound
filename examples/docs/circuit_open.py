"""Failing fast instead of joining a provider's retry storm. Shown on: Production checklist, Self-hosted."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([
    (429, _offline.error_body("Rate limit reached for gpt-4o")),
])

import openai
from openai import OpenAI

fallback_calls: list = []


def other_provider(msgs):
    fallback_calls.append(msgs)
    return "served by the fallback"


# docs: circuit-open
import runbound
from runbound import CircuitOpen

runbound.init(on_provider_failure="open", circuit_failure_threshold=2)
client = runbound.wrap(OpenAI())


def ask(msgs):
    try:
        return client.chat.completions.create(model="gpt-4o", messages=msgs)
    except CircuitOpen:
        return other_provider(msgs)     # your fallback, your decision
# /docs

msgs = [{"role": "user", "content": "hi"}]

# While the circuit is closed, the provider's own exception is what the caller
# catches (README: "the provider's own exception is what your code catches").
for _ in range(2):
    try:
        ask(msgs)
    except openai.RateLimitError:
        pass
    else:
        raise AssertionError("a 429 must reach the caller while the circuit is still closed")

assert fallback_calls == [], "the fallback must not run before the circuit opens"

reply = ask(msgs)
assert reply == "served by the fallback", reply
assert transport.calls == 2, "the third request must never have left the process"
