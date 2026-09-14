"""Guarding an async client the same way as a sync one. Shown on: Streams and async."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion()])

# docs: async-wrap
import asyncio

import runbound
from openai import AsyncOpenAI

runbound.init()
client = runbound.wrap(AsyncOpenAI())

async def main():
    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )

asyncio.run(main())
# /docs

assert transport.calls == 1
assert runbound.current_session().turns == 1
