"""What runbound costs per call, measured on a mock HTTP transport.

Three legs, no network and no API key:

1. **LLM calls.** 1000 `chat.completions.create` on a real `openai.OpenAI`
   whose HTTP layer is an `httpx.MockTransport` — the same arrangement
   `tests/test_real_sdk.py` uses, for the same reason: the provider SDK does
   its own parsing and validation, so what runbound sees is what it sees in
   production, only without the wire.
2. **Tool calls.** 1000 calls of a `@runbound.tool`-decorated function.
3. **Contention.** 32 threads sharing one *keyed* session — one
   `SessionState`, one lock, all 32 threads accounting through it. This is
   the path where runbound's own locking, if it costs anything, shows up.

Each leg times a guarded run and an unguarded twin of the same work, and
reports the difference. "Added latency" is a **shift between two
distributions at the same quantile** — `p50(guarded) - p50(unguarded)`,
`p99(guarded) - p99(unguarded)` — not a per-call subtraction, which would
need the same call to happen twice. It is the honest reading of what the
guard adds to a typical call and to a slow one.

The number this prints is a fact about the machine it ran on and nothing
else. Real calls take tens or hundreds of milliseconds over a network; the
whole point of running against a mock transport is that the overhead is not
buried under that. Run it on your own machine before quoting it.

    python -m examples.stress.bench             # 1000 / 1000 / 32x32
    python -m examples.stress.bench --calls 5000

Needs `openai` and `httpx` installed (`pip install openai httpx`). It is an
example, not a test: `pytest` never collects it.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import sys
import threading
import time
from typing import Callable, Sequence

try:
    import httpx
    import openai
except ImportError as exc:  # an example may ask for what the SDK never needs
    print(f"bench needs openai and httpx installed ({exc}). pip install openai httpx")
    raise SystemExit(2) from None

import runbound

MODEL = "gpt-4o"
BASE_URL = "http://mock.local/v1"
SESSION_KEY = "bench:one-key"

#: One ordinary chat completion, priced by runbound's table: 100 in, 50 out.
BODY = {
    "id": "chatcmpl-bench",
    "object": "chat.completion",
    "created": 1_756_000_000,
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
}


def _handler(request: "httpx.Request") -> "httpx.Response":
    """Answer every request with a fresh response object.

    Fresh rather than shared: leg 3 has 32 threads in this handler at once,
    and one `Response` whose stream is read by all of them is a race that
    would land in the measurement rather than in runbound.
    """
    return httpx.Response(200, json=BODY)


def _client(guarded: bool) -> "openai.OpenAI":
    """A real OpenAI client on the mock transport, guarded or not."""
    client = openai.OpenAI(
        api_key="bench",
        base_url=BASE_URL,
        max_retries=0,  # one create() must mean exactly one request
        http_client=httpx.Client(transport=httpx.MockTransport(_handler)),
    )
    return runbound.wrap(client) if guarded else client


def _chat(client: "openai.OpenAI", i: int) -> None:
    """One completion. The content varies so no two calls hash alike."""
    client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": f"hi {i}"}]
    )


# --- statistics -------------------------------------------------------------


def _pct(samples: Sequence[float], q: float) -> float:
    """The nearest-rank ``q`` quantile of ``samples``, in seconds."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _us(seconds: float) -> str:
    return f"{seconds * 1e6:9.1f} us"


class Leg:
    """One measured comparison: the same work, guarded and unguarded."""

    def __init__(self, title: str, note: str, plain: list[float], guarded: list[float]):
        self.title = title
        self.note = note
        self.plain = plain
        self.guarded = guarded

    def added(self, q: float) -> float:
        return _pct(self.guarded, q) - _pct(self.plain, q)

    def report(self) -> None:
        print(f"\n{self.title}")
        print(f"  {self.note}")
        for label, samples in (("unguarded", self.plain), ("guarded  ", self.guarded)):
            print(
                f"    {label}  n={len(samples):<6} "
                f"p50 {_us(_pct(samples, 0.50))}   p99 {_us(_pct(samples, 0.99))}"
            )
        print(
            f"    ADDED      per call  "
            f"p50 {_us(self.added(0.50))}   p99 {_us(self.added(0.99))}"
        )
        if self.added(0.99) <= 0:
            print("               (p99 shift <= 0: at the tail the guard is inside")
            print("                this transport's own jitter, not measurable)")


def _once(work: Callable[[int], None], i: int, into: list[float]) -> None:
    """Call ``work(i)`` once, appending how long it took to ``into``."""
    start = time.perf_counter()
    work(i)
    into.append(time.perf_counter() - start)


def _warm(work: Callable[[int], None], n: int = 50) -> None:
    for i in range(n):
        work(i)


# --- the legs ---------------------------------------------------------------


def leg_llm(n: int) -> Leg:
    """1000 wrapped LLM calls against the mock transport, one session.

    Interleaved — one unguarded call, one guarded call, all the way down — so
    that anything that drifts over the run (cache state, the OS, this laptop
    deciding to think about something else) drifts through both samples.
    """
    plain_client, guarded_client = _client(False), _client(True)
    plain: list[float] = []
    guarded: list[float] = []

    _warm(lambda i: _chat(plain_client, i))
    _warm(lambda i: _chat(guarded_client, i))
    for i in range(n):
        _once(lambda j: _chat(plain_client, j), i, plain)
        _once(lambda j: _chat(guarded_client, j), i, guarded)
    return Leg(
        "1. LLM call — openai.OpenAI over httpx.MockTransport",
        f"{n} guarded calls interleaved with {n} unguarded, one default session",
        plain,
        guarded,
    )


def leg_tool(n: int) -> Leg:
    """1000 ``@runbound.tool`` calls against the same function undecorated."""

    def lookup(order_id: str) -> str:
        return order_id[::-1]

    guarded_lookup = runbound.tool(lookup)
    plain: list[float] = []
    guarded: list[float] = []

    _warm(lambda i: lookup(f"order-{i}"))
    _warm(lambda i: guarded_lookup(f"warm-{i}"))
    for i in range(n):
        _once(lambda j: lookup(f"order-{j}"), i, plain)
        _once(lambda j: guarded_lookup(f"order-{j}"), i, guarded)
    return Leg(
        "2. Tool call — @runbound.tool vs the same function undecorated",
        f"{n} guarded calls interleaved with {n} unguarded, arguments vary per call",
        plain,
        guarded,
    )


def _threaded(
    work: Callable[[int], None], threads: int, per_thread: int, key: str | None
) -> list[float]:
    """Time ``per_thread`` calls of ``work`` on each of ``threads`` threads.

    With ``key``, every thread enters that one keyed session first — so all
    of them account through a single ``SessionState`` and a single lock.
    """
    samples: list[list[float]] = [[] for _ in range(threads)]

    def loop(into: list[float]) -> None:
        for i in range(per_thread):
            _once(work, i, into)

    def run(t: int) -> None:
        if key is None:
            loop(samples[t])
            return
        with runbound.session(key):
            loop(samples[t])

    workers = [threading.Thread(target=run, args=(t,)) for t in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return [duration for per_worker in samples for duration in per_worker]


def leg_threads(threads: int, per_thread: int) -> Leg:
    """``threads`` threads on ONE keyed session — the contended path.

    Whatever that single ``SessionState`` lock costs when every thread wants
    it at once is in this number. The unguarded phase runs the same threads
    over the same transport with no session at all.
    """
    plain_client, guarded_client = _client(False), _client(True)

    _warm(lambda i: _chat(plain_client, i))
    _warm(lambda i: _chat(guarded_client, i))

    plain = _threaded(lambda i: _chat(plain_client, i), threads, per_thread, None)
    guarded = _threaded(
        lambda i: _chat(guarded_client, i), threads, per_thread, SESSION_KEY
    )

    return Leg(
        f"3. Contention — {threads} threads, one keyed session, one lock",
        f"{threads} x {per_thread} guarded calls under session({SESSION_KEY!r}), "
        f"then the same shape unguarded",
        plain,
        guarded,
    )


# --- the run ----------------------------------------------------------------


def machine() -> str:
    """The line without which none of the numbers below mean anything."""
    cores = os.cpu_count() or 0
    return (
        f"{platform.platform()} ({platform.machine()}, {cores} cores)\n"
        f"  python    {platform.python_version()} ({platform.python_implementation()})\n"
        f"  runbound  {runbound.__version__}   "
        f"openai {openai.__version__}   httpx {httpx.__version__}"
    )


def _accounted() -> str:
    """What runbound actually recorded — the line that says it was guarded.

    A benchmark of a wrapper that quietly failed to wrap would report a very
    flattering overhead. These counts are the check: the guarded legs above
    are only worth reading if runbound saw every one of their calls.
    """
    default = runbound.current_session()
    with default.lock:
        steps = default.step_count
    tools = sum(runbound.tool_calls().values())
    with runbound.session(SESSION_KEY) as keyed:
        with keyed.lock:
            keyed_steps = keyed.step_count
    return (
        f"accounted: {steps} steps on the default session ({tools} of them tool "
        f"calls), {keyed_steps} on {SESSION_KEY!r}; warm-ups included."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.py", description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=1000, help="calls per single-threaded leg")
    parser.add_argument("--threads", type=int, default=32, help="threads in the contended leg")
    parser.add_argument("--per-thread", type=int, default=32, help="calls per thread")
    args = parser.parse_args(argv)

    # auto_wrap patches the provider *classes*, which would guard the
    # unguarded twin too and leave nothing to compare against. wrap() does
    # exactly what auto_wrap would have done to the one client we measure.
    runbound.init(auto_wrap=False)

    print("runbound overhead — added latency per call, on a mock transport")
    print(f"  machine   {machine()}")

    legs = [
        leg_llm(args.calls),
        leg_tool(args.calls),
        leg_threads(args.threads, args.per_thread),
    ]
    for leg in legs:
        leg.report()

    single, contended = legs[0].added(0.50), legs[2].added(0.50)
    print(
        f"\n  contention: added p50 is {_us(single).strip()} on one thread and "
        f"{_us(contended).strip()} on {args.threads} sharing a session."
    )
    print("  p50/p99 are quantile shifts between two samples, not paired differences.")
    print(f"  {_accounted()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
