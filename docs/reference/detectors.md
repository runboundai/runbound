# How we prevent uncontrolled execution — the detectors (implementation)

[← Docs](../README.md)

The headline is the promise above: unbounded or policy-violating execution
gets stopped deterministically. These eight detectors are how — the
implementation, not the pitch. Each is plain counting over in-memory state.
No detector calls a model, and none of them can be "wrong" in the way a
classifier can — they report a fact about your session. Ties among critical
anomalies that co-fire on the same event are resolved by a fixed precedence,
not by the order below — see [Which anomaly wins a
tie](#which-anomaly-wins-a-tie).

| Detector | What it catches | Config knob | Fires when |
|---|---|---|---|
| `loop` | The agent repeating the same tool call with the same arguments — whether your code ran it or [the model just asked for it](model-requested-tools.md#model-requested-tool-calls-loops-without-tool) | `loop_threshold` (default 3), `loop_window` (default 20) | The current `tool_call` or `tool_request` event's argument hash appears **at least** `loop_threshold` times in the last `loop_window` recorded actions. Requests are hashed into their own namespace, so three requests and three executions are two threes, not a six. Severity `critical`. |
| `budget` | A run spending more money or more tokens than allowed | `budget_usd`, `max_total_tokens` | `total_cost_usd > budget_usd`, or `total_tokens > max_total_tokens`. Strictly greater: exactly at the limit does not trip. The trip lands **after** the call that crossed the limit, since usage exists only once it returns. Cost is reported first if one event blows through both. Severity `critical`. |
| `velocity` | Burning tokens too fast, regardless of the total | `tokens_per_minute_limit` | Tokens recorded in the trailing 60 seconds (measured from the current event's timestamp) exceed the limit. An entry exactly 60s old still counts. Severity `warn`. |
| `steps` | An agent that will not stop taking model turns | `max_steps` | `turns > max_steps`, where a turn is one `llm_call` — a model call that makes three tool calls is one step, not four. Severity `critical`. See [max_steps and max_events](#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `events` | A session generating too much recorded activity, of any kind | `max_events` | `event_count > max_events` — every recorded event counts: model calls, tool calls, tool requests, failures. This is what `max_steps` counted before 0.3.0. Severity `critical`. |
| `spike` | A session whose model calls stop looking like themselves — thinking mode, a model update, a caller driving long generations | none (on by default); tune with `spike_*`, cap with `max_call_seconds` / `max_tokens_out_per_call` / `max_cost_per_call_usd` | A call exceeds `spike_factor` × this session's median duration or output work, after `spike_warmup_calls` of history. First one severity `warn` (never stops the agent); `spike_confirm` of the trailing 5 makes it `critical`. A breached hard cap — seconds, output tokens, or dollars on one call — is `critical` immediately, from call #1. See [Spike detection](../guides/spike-detection.md#spike-detection-zero-config). |
| `error_storm` | An agent retrying into a wall: a provider answering 429 while every layer above it retries | `error_storm_limit` (default **10**, `None` disables) | More than `error_storm_limit` failed calls — failed model calls *and* failed tools — in the trailing 60 seconds. Severity `critical`. The one detector that is on by default with a number. See [Retry storms](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). |
| `timeout` | A run that stopped being work an hour ago, with no single call looking wrong | `max_session_seconds`, `max_session_lifetime_seconds` | `event.ts - session.run_started_at > max_session_seconds` (`details["scope"] == "run"`), on any event kind, on the monotonic clock; or, if set, `event.ts - session.started_at > max_session_lifetime_seconds` (`details["scope"] == "lifetime"`). Each fires independently, once per session. Severity `critical`. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |

**`budget_usd` stops after the call that crossed it** — exact, and the
default. `budget_admission=True` (opt-in, off by default) also refuses a
call *before it goes out* whose **estimated** cost would cross `budget_usd`
— an estimate, stated as such, never the default: see [Admission: an
opt-in pre-call budget
check](#admission-an-opt-in-pre-call-budget-check-budget_admission).

## Which anomaly wins a tie

When more than one detector fires critical on the same event, exactly one
drives the reaction (`on_anomaly`, `on_trip`) — the rest are still alerted,
just not acted on. Which one wins is a **fixed, stated precedence**
(`runbound.events.PRIORITY`), not an accident of which detector happened to
run first:

`policy` > `budget` > `loop` > `error_storm` > `steps` > `events` > `timeout` > `spike` > `velocity`

Read as: a tool-policy violation outranks everything (you wrote that rule
yourself); then cost, then repetition, then failure, then shape (`steps`,
`events`), then time, then behavior — `velocity` last, since it is warn-only
and never stops anything anyway. `halt`, `circuit`, `inflight` and `plane`
are door refusals, raised before detection ever runs, so they are never in a
tie with anything. A detector name this table has never heard of — your own,
custom one — sorts after every named one and warns once; it never crashes
the selection. This order does not depend on `DEFAULT_DETECTORS`' list order,
which you are free to reorder or replace when injecting your own detectors.

Two behaviors worth knowing before you tune anything:

- **A detector fires once per session.** A sustained overrun does not re-alert
  on every following event. `runbound.reset()` starts a new session and arms
  them all again. (The loop detector under `on_loop="throttle"` or
  `"escalate"` is the exception — see [When a loop is
  detected](#when-a-loop-is-detected) — and `spike` reports twice, once when it
  starts watching and once when it confirms.)
- **A knob left at `None` disables its detector.** A session with no limits set
  observes and never trips — except `spike`, which needs no knob and is on
  unless you set `spike_detection=False`, and `error_storm`, which ships with a
  number (`error_storm_limit=10`) and is off only if you set it to `None`.

## `max_steps` and `max_events`: steps are turns, events are events

**One step is one model turn** (`llm_call` event) — not one agent iteration
in the older, looser sense, and not every recorded event either. An agent
that makes one model call and then three tool calls in the same turn has
taken **one** step, however many events that produced. `max_steps` is
measured against `SessionState.turns`; an agent alternating one model call
with one tool call reaches `max_steps=50` after 50 turns (100 events).

**`max_events`** is the raw count instead: every recorded event — model
calls, tool calls, tool requests, failures — counted once each, measured
against `SessionState.event_count`. This is what `max_steps` counted before
0.3.0. Use it when what you actually want bounded is total recorded
activity, not how many times the model itself ran.

The two are independent and both optional: three model calls plus seven tool
calls is `turns == 3` and `event_count == 10`; `max_steps=3` and
`max_events=10` each trip on their own turn, whichever you set.
`SessionState.step_count` is kept for one release as a read-only alias for
`event_count` — the pre-0.3.0 name for the pre-0.3.0 meaning — but new code
should read `turns` or `event_count` by name instead.

**Where in the call the trip happens** differs by detector, and it matters:

- `@runbound.tool` emits its event **before** the function body runs, so a
  loop is broken on the repeat that would have made it — the third identical
  call never executes.
- A wrapped LLM client emits its event **after** the call returns, because
  usage only exists then. The call that crossed your budget has already been
  paid for; the *next* one is the one prevented.
- A **model-requested** tool call is read off that same response, so a loop of
  requests trips out of `create()` after it returned. The provider call
  happened and was counted; what the raise prevents is your dispatch of the
  third identical call. See [Model-requested tool
  calls](#model-requested-tool-calls-loops-without-tool).
- A **fan-out limit** is enforced on `session()` entry, before the block's body
  runs at all.

## Admission: an opt-in pre-call budget check (`budget_admission`)

The `budget` detector above is exact and is the default for a reason: it
looks at a running total this process actually holds, and it never guesses.
That is also its one limitation — like every wrapped-LLM detector, it trips
**after** the call that crossed the limit returns, because usage only exists
then (see "Where in the call the trip happens," above). Most of the time
that is the right trade: precision over prediction.

Sometimes it is not — a single call can cost real money before its usage is
ever known, and you would rather refuse it than pay for it. `budget_admission`
is that choice, and it is **opt-in, off by default**: estimation is not
deterministic, and this product's identity is that it is. Set it and the
engine gains a fourth check, run before every wrapped call goes out (after
the circuit and `on_unpriced_model="refuse"`, alongside the in-flight cap —
see `Engine.admit`):

```python
runbound.init(budget_usd=5.0, budget_admission=True, on_anomaly="raise")
```

The estimate: characters across the request's `messages`, divided by four
(the same rough token estimator used everywhere else in runbound), priced at
the model's input rate; plus the request's own output-token cap —
`max_tokens`, `max_completion_tokens` or `max_output_tokens`, whichever it
set — or, if it set none, `admission_output_tokens` (default **1024**),
priced at the output rate. Same static price table (or `custom_prices`) the
post-call check uses — always at the model's plain input rate, even for a
model with a published cached-input rate: admission runs before the request
goes out, with no way to know yet how many of its tokens will be a cache
hit, so it never assumes the discount. The call is refused, with `GuardrailTripped` (detector
`budget`, `details["rule"] == "admission"`), when that estimate would push
`total_cost_usd + spend_offset_usd` past `budget_usd`.

Three things make this a door, not a second wall:

- **It never latches.** A refused estimate says nothing about the *next*
  call — a cheaper one, seconds later, may fit easily — so latching here
  would turn a guess into a permanent wall. The session is untouched:
  totals unchanged, nothing tripped, free to try again immediately.
- **An unpriced model skips it, not refuses it.** A model with no known
  price (`custom_prices` or the built-in table) cannot be estimated, and
  refusing a call for a cost runbound cannot compute would be the SDK
  inventing a limit you never set. It is warned once per model instead, and
  the post-call `budget` check still watches the call once it returns.
- **It is alerted once per session**, kept apart from an ordinary post-call
  `budget` trip in the same session by `details["rule"]`, so neither shadows
  the other.

With `budget_admission` left at its default (`False`), nothing here runs at
all — every call behaves exactly as it did before this setting existed.

---
