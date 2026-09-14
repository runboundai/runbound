"""A response reporting cached input tokens is priced at the discount. Shown on: Budgets and pricing."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([
    _offline.chat_completion(tokens_in=1200, tokens_out=50, cached_tokens=1000),
    _offline.chat_completion(tokens_in=1200, tokens_out=50, cached_tokens=0),
])

import runbound
from openai import OpenAI

runbound.init()
client = runbound.wrap(OpenAI())

# docs: cached-tokens
client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
# /docs

session = runbound.current_session()
with session.lock:
    cost_with_cache = session.total_cost_usd

assert cost_with_cache > 0.0

runbound.reset()
client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
session = runbound.current_session()
with session.lock:
    cost_without_cache = session.total_cost_usd

assert cost_with_cache < cost_without_cache, (cost_with_cache, cost_without_cache)
