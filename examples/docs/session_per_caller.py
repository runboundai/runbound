"""Wrap a request in a session keyed by the caller, and every control is per caller. Shown on: the landing page, How you use it."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion()])

import runbound
from openai import OpenAI

runbound.init()
client = OpenAI()
user_id = "8842"
messages = [{"role": "user", "content": "hi"}]

# docs: session-per-caller
with runbound.session(f"user:{user_id}"):
    reply = client.chat.completions.create(model="gpt-4o", messages=messages)
# /docs

with runbound.session(f"user:{user_id}") as caller:
    pass
with runbound.session("user:another") as other:
    pass

assert reply.choices[0].message.content, "the guarded call should return the model's reply untouched"
assert caller.turns == 1, "the caller's own session should have counted its one model turn"
assert other.turns == 0, "another caller's session should have seen nothing"
