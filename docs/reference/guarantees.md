# Guarantees and limitations

[← Docs](../README.md)

**Fail-open is the promise.** A bug in runbound can never take down your agent.
Every failure inside detection, pricing, hashing, wrapping, or alerting is
caught, logged to the `"runbound"` logger, and swallowed — your call proceeds
as if runbound were not there. A detector that raises is skipped; a client
runbound cannot patch runs unguarded; a broken observer is logged past. The only
exception that escapes on purpose is `GuardrailTripped`, and only if you chose
`on_anomaly="raise"`.

The one deliberate exception to fail-open is `init()` itself: bad configuration
raises immediately, at startup, where you will see it.

**Overhead is measured, and the method is the promise — not the number.** On
one machine (Apple M2, 8 cores, macOS 26.0, CPython 3.13.14, `openai` 3.13.0
over an `httpx.MockTransport` with no network in the way), over six runs of
`examples/stress/bench.py`, a guarded LLM call cost **34–35 µs more than the
same call unguarded, at p50**; a `@runbound.tool` call **6.7–6.9 µs**; and
with **32 threads sharing one keyed session** — one `SessionState`, one
lock — **36–42 µs at p50 and 110–190 µs at p99**. Sharing the lock barely
moves the median and shows up in the tail, which is where a queue for a lock
should show up. On a single thread the p99 shift came out *below zero*: at the tail the
guard is smaller than the transport's own jitter, so there is nothing there to
measure.

Your CPU, your Python and your provider SDK are not those, so treat that as an
order of magnitude and not a service level. What we will stand behind is how it
was arrived at: `examples/stress/bench.py` times 1000 guarded calls
interleaved with 1000 unguarded ones, then 1000 decorated tool calls against
the same function undecorated, then 32 threads on one keyed session, and prints
the shift between the two samples at each quantile. It needs no network and no
API key — run it on your own hardware and quote your own number. For scale: a
real provider call is tens to hundreds of milliseconds, so 34 µs is under a
tenth of a percent of the call it is guarding.

Honest limitations today:

- **runbound guards only what passes through its sensors.** An auto-wrapped
  or `wrap()`ped client, a `@runbound.tool`, `record_call()` / `@runbound.llm`,
  or the LangChain handler. Raw HTTP to a provider and SDKs that are not
  OpenAI- or Anthropic-shaped are invisible, and an invisible call looks exactly
  like a quiet one: every detector reads green.
  [What the SDK actually sees](../concepts/what-it-sees.md#what-the-sdk-actually-sees--and-what-it-never-sees)
  says which numbers go blind without each sensor, and `runbound.coverage()` /
  `runbound.assert_guarded()` are how you check instead of hoping.
- **An abandoned stream is recorded, but as an estimate, and only when
  garbage collected.** A guarded stream reports normally when it is exhausted,
  closed, or exited; one that is simply dropped is still reported — as one
  partial call, with the time actually streamed and usage-or-estimated
  output tokens (input tokens only from usage, or from the request under
  `estimate_tokens=True`) — but only once Python collects it, which is not necessarily
  promptly, and never at interpreter exit. See [Async and
  streaming](#async-and-streaming).
- **OpenAI streams need `stream_options={"include_usage": True}`** to be priced.
  Without it a stream is recorded as a step with zero tokens.
- **Sessions are per process.** `runbound.session(key)` gives each key its own
  session; work outside any block goes to one default session. Call
  `runbound.reset()` between agent runs in a long-lived worker so counters
  restart and detectors re-arm.
- **Without a control plane, everything is per process.** Budgets, latches,
  strikes, circuits and counters live in the worker that earned them, so N
  replicas mean N times the numbers you wrote.
  [Fleet mode](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane)
  shares the budget, the latch, the strike count, the org policy and the
  provider circuits; the three bullets below are what it does **not** fix.
- **Spike baselines are in-process and reset on restart.** A redeployed worker
  re-learns each session over its first `spike_warmup_calls` calls, and two
  workers serving the same user learn separately. Baselines are not among the
  things fleet mode shares.
- **Fan-out counters and the in-flight cap stay per worker, plane or no
  plane.** `max_active_sessions`, `max_session_depth`, `max_child_sessions` and
  `max_inflight_calls` are enforced against this process's own counts, so
  across N replicas they are really N times the numbers you wrote.
  `inflight_calls()` answers for this process only, and so does
  `circuit_state()` — a fleet circuit is *applied* to every worker, but what
  each one reports is its own state.
- **`max_calls` is still counted per session in one process.** An org policy
  from the plane brings the *rules* to every worker; the tally that
  `max_calls` is measured against is local, so N workers can each allow the
  same "once per session" call once.
- **`estimate_tokens` is an approximation, not a tokenizer.** `ceil(chars / 4)`
  over the text runbound can read: it exists so a server that reports no
  usage is counted as something rather than as free traffic. Real usage is
  always preferred, and estimated dollars are an order of magnitude, not a
  bill.
- **Only OpenAI- and Anthropic-shaped clients are wrapped.** Native SDKs with
  their own shapes — TGI's client, Bedrock, Vertex — and in-process inference
  are recorded through `runbound.record_call()` / `@runbound.llm` instead.
- **A model-requested loop trips *after* the response.** The wrapper reads the
  tool calls off a response that has already returned and been paid for; the
  raise stops your dispatch of the repeat, not the call that carried it. Only
  `@runbound.tool` catches a repeat before it executes.
- **Streamed tool requests are best-effort.** Fragments are assembled from the
  chunks as they arrive and reported at stream end; a provider or chunk shape
  runbound cannot read reports nothing rather than guessing. An abandoned
  stream's tool-call fragments are not recovered either — only the call's
  tokens and duration are recorded on abandonment, not a partial tool request.
- **Fleet mode costs a session entry up to `control_plane_timeout_s`.** 150 ms
  by default, on the request that opens a `session()` block and only on a cache
  miss — one key's answer is reused for `control_plane_cache_s` (5 s), and
  after 3 failures in a row the link stops calling at all. Nothing else on the
  request path talks to the plane.
- **A latch reaches the rest of the fleet within `control_plane_cache_s` plus
  one turn.** A key latched on one worker is refused on another as soon as that
  worker's cached entry answer for the key expires — 5 seconds by default —
  and not before. A worker mid-turn finishes that turn.
- **Budgets compare accumulated floats; don't predict the turn by division.**
  Six $0.02 turns accumulate to `0.12000000000000001`, not `0.12`, so a
  `budget_usd=0.12` is *over* on turn 6 rather than exactly at it. The
  comparison is against the running total the SDK actually holds, which is the
  honest thing to do with money in floats — but it means `budget / cost_per_turn`
  is not a reliable prediction of which turn trips.
- **A block refused at the door reports no exit.** A halt, a remote latch or a
  fan-out refusal raises before the body runs, so there is no delta to send and
  none is sent. The trip itself is reported; the (empty) exit is not.
- **A fleet halt fails open after 60 seconds, by default.** Under
  `stale_halt="release"` (the default) an enforced halt is enforced only
  while the worker is still hearing it from the plane, so a plane that dies
  releases the fleet rather than stopping it — a kill switch that outlives its
  operator is a worse failure than one that lapses. `stale_halt="hold"` is the
  other choice: the halt stays enforced past that window, on a dead link,
  until a heartbeat explicitly lifts it — pick it only if a stuck kill switch
  is the failure you can live with and a false "all clear" is not.
- **A new thread does not inherit the current session.** Context variables are
  per thread, so a thread spawned inside a `session()` block lands on the
  default session unless it enters the block itself. `asyncio` tasks do inherit.
- **Costs are estimates** from a static list-price table. Batch discounts and
  negotiated rates are not modeled.
- **Auto-patching covers two SDKs.** `auto_wrap` patches the OpenAI and
  Anthropic classes and nothing else; every other client still needs `wrap()`,
  and tools still need `@runbound.tool`. `runbound.unpatch()` undoes it.
- **A synchronous tool called directly on the event-loop thread is not
  throttled.** `on_loop="throttle"` stashes its delay in a `contextvar` for an
  `await`ing async wrapper to sleep; a synchronous call made directly on the
  event-loop thread, outside either async path, has nothing to await with, so
  its wrapper takes and discards the pending delay right away instead of
  blocking the loop — that one call is not throttled.
- Python 3.10+.

---
