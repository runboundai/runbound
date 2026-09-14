"""Async tasks inherit a session; a thread has to enter it itself. Shown on: Level 2, Streams and async."""

import runbound

runbound.init()

# docs: session-async-threads
import asyncio
import threading

import runbound


async def step():
    runbound.record_call("gpt-4o", tokens_in=10, tokens_out=5)


async def main():
    with runbound.session("run:async-demo"):
        await asyncio.gather(step(), step())


def worker():
    with runbound.session("run:async-demo"):
        runbound.record_call("gpt-4o", tokens_in=10, tokens_out=5)


asyncio.run(main())
t = threading.Thread(target=worker)
t.start()
t.join()
# /docs

with runbound.session("run:async-demo") as state:
    pass

assert state.event_count == 3, "two tasks plus one thread, all on the same key"
