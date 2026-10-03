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
| `loop` | The same call repeated — free, always on. By default a loop is answered in three rungs: 3 is worth a line, 6 is worth a person, 9 is a runaway. See [the graded policy](reactions.md#when-a-loop-is-detected). Three more shapes (a rotation of tools cycling, one tool failing over and over, a run gone quiet) are also free and local, on by default except `stall` (opt-in). See [The four loop shapes](#the-four-loop-shapes). | `loop_threshold` (default 3), `loop_window` (default 20) | The shape-specific condition below holds for the current event. Requests are hashed into their own namespace, so three requests and three executions are two threes, not a six. Severity `warn` at the first rung and `critical` from the second under the graded policy; `critical` under the legacy `on_loop` values. |
| `budget` | A run spending more money or more tokens than allowed | `budget_usd`, `max_total_tokens` | `total_cost_usd > budget_usd`, or `total_tokens > max_total_tokens`. Strictly greater: exactly at the limit does not trip. The trip lands **after** the call that crossed the limit, since usage exists only once it returns. Cost is reported first if one event blows through both. Severity `critical`. |
| `velocity` | Burning tokens too fast, regardless of the total | `tokens_per_minute_limit` | Tokens recorded in the trailing 60 seconds (measured from the current event's timestamp) exceed the limit. An entry exactly 60s old still counts. Severity `warn`. |
| `steps` | An agent that will not stop taking model turns | `max_steps` | `turns > max_steps`, where a turn is one `llm_call` — a model call that makes three tool calls is one step, not four. Severity `critical`. See [max_steps and max_events](#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `events` | A session generating too much recorded activity, of any kind | `max_events` | `event_count > max_events` — every recorded event counts: model calls, tool calls, tool requests, failures. This is what `max_steps` counted before 0.3.0. Severity `critical`. |
| `spike` | A session whose model calls stop looking like themselves — thinking mode, a model update, a caller driving long generations | none (on by default); tune with `spike_*`, cap with `max_call_seconds` / `max_tokens_out_per_call` / `max_cost_per_call_usd` | A call exceeds `spike_factor` × this session's median duration or output work, after `spike_warmup_calls` of history. First one severity `warn` (never stops the agent); `spike_confirm` of the trailing 5 makes it `critical`. A breached hard cap — seconds, output tokens, or dollars on one call — is `critical` immediately, from call #1. See [Spike detection](../guides/spike-detection.md#spike-detection-zero-config). |
| `error_storm` | An agent retrying into a wall: a provider answering 429 while every layer above it retries | `error_storm_limit` (default **10**, `None` disables) | More than `error_storm_limit` failed calls — failed model calls *and* failed tools — in the trailing 60 seconds. Severity `critical`. The one detector that is on by default with a number. See [Retry storms](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). |
| `timeout` | A run that stopped being work an hour ago, with no single call looking wrong | `max_session_seconds`, `max_session_lifetime_seconds` | `event.ts - session.run_started_at > max_session_seconds` (`details["scope"] == "run"`), on any event kind, on the monotonic clock; or, if set, `event.ts - session.started_at > max_session_lifetime_seconds` (`details["scope"] == "lifetime"`). Each fires independently, once per session. Severity `critical`. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |

**`budget_usd` is not crossed by a call that states its output cap, and stops
after the call that crossed it otherwise.** Under the default
`budget_admission="capped"`, a request that names its own output cap is refused
*before it goes out* when that cap, priced at the model's output rate, plus its
input estimate would take the session past `budget_usd`. A request with no
stated cap is checked after it returns, exactly and as it always was. See
[Admission: a budget that is not crossed](#admission-a-budget-that-is-not-crossed-budget_admission).

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
  them all again. (A *refusal* is not a detector firing: every refusal is
  recorded, up to a cap per session, rule and tool. The loop detector under
  `on_loop="throttle"` or `"escalate"` is the exception — see [When a loop is
  detected](#when-a-loop-is-detected) — and `spike` reports twice, once when it
  starts watching and once when it confirms.)
- **A knob left at `None` disables its detector.** A session with no limits set
  observes and never trips — except `spike`, which defaults **on**
  (`spike_detection=True`; see
  [Progressive degradation](../guides/spike-detection.md)), and
  `error_storm`, which ships with a
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

## The four loop shapes

The `loop` detector implements four deterministic, content-blind shapes — the
loops agents fall into, not general graph inference over what a tool call
*means* (which would cross into behaviour understanding, and is refused).
All four are free and local, checked in the order `loop_shapes` states —
`("repeat", "sequence", "retry")` by default; `"stall"` is opt-in even
locally. An unknown shape name raises `ValueError` at `init()`. A
connected plane can only add a shape you have not opted into (`"stall"`,
typically), or narrow `loop_stall_turns` / widen `loop_max_period` further
— never drop a shape you already check or loosen either knob.

```
repeat      search("x")  search("x")  search("x")
            └──────────────── same hash, 3x ────────────────┘

sequence    edit(a)  test()  edit(b)  test()  edit(c)  test()
            └──edit,test──┘  └──edit,test──┘  └──edit,test──┘
            (period 2, repeated 3x -- different arguments each cycle)

retry       issue_refund(x) -> tool_error
            issue_refund(y) -> tool_error
            issue_refund(z) -> tool_error
            └──────── same tool, 3 failures ────────┘

stall       turn 1: search("x")   (new)
            turn 2: (nothing new)
            turn 3: (nothing new)
            turn 4: (nothing new)  <- 3 quiet turns, no new hash since turn 1
```

| Shape | Fires when | Config knob |
|---|---|---|
| `repeat` | The current event's `args_hash` appears at least `loop_threshold` times in the last `loop_window` recorded actions (unchanged since before 0.4.0). Free, always on. | `loop_threshold`, `loop_window` |
| `sequence` | A rotation of distinct **tool names** — not exact arguments, since the edit and the test each cycle usually differ — repeats `loop_threshold` times back to back. Compared by name rather than hash: an exact-hash rotation always also satisfies `repeat` on an earlier event, so `repeat` is the more specific read of that evidence. Free and on by default. | `loop_threshold`, `loop_max_period` (longest rotation period searched, default 6) |
| `retry` | One tool's `tool_error` events reach `loop_threshold` within the trailing `loop_window` failures of *that* tool. Distinct from `error_storm`, which counts every failure, any tool, any kind (`llm_error` too), in a trailing 60-second window rather than a trailing action count. Free and on by default. | `loop_threshold`, `loop_window` |
| `stall` | Some number of consecutive turns introduce no `args_hash` the session has not already seen at least once. Not restricted to tool events: a run of pure model turns with no tool calls at all stalls too. Free, but opt-in even locally — a quiet agent is not always a stuck one. | Add `"stall"` to `loop_shapes`; `loop_stall_turns` (default 5) sets its turn count |

Every shape's `details` carries a common backbone beside its own fields:
`"shape"` (one of the four names above), `"period_tools"` (the tool names in
the repeating unit — `[tool_name]` for `repeat`/`retry`, `[]` for `stall`),
`"repeats"`, `"started_turn"` (the `SessionState.turns` value the loop began
at) and `"usd_inside_loop"` — the model spend since `started_turn`. That last
one is exact when the loop began within the trailing `spike_window` calls
(default 50; the session's only per-call cost history,
`SessionState.recent_calls`, keeps no more), and an honestly documented lower
bound — never a wrong total — when it began longer ago than that. No shape's
`message` ever contains an `args_hash`: it is a salted digest and the one
thing here that could leak call arguments into a human-readable string.

`@runbound.tool` gains two more declarative keywords beside `polling=`:
`idempotent=True` states that calling the tool twice with the same arguments
does the same thing once as it does twice, and `retryable=True` states that
the tool is expected to be retried by the caller's own code. Both are
reported in the tool report (`coverage()`) and the control surface for a
human or the plane to read; `idempotent=True` is not enforced by anything yet, and
`retryable=True` is enforced only as a grace on the `retry` loop shape: a
retryable tool's failures need twice `loop_threshold` to trip it
(`tests/test_loop_shapes.py::test_retryable_tool_trips_at_double_the_threshold`).

## Admission: a budget that is not crossed (`budget_admission`)

**The promise.** With a stated cap, `budget_usd` is not crossed — under
concurrency, not just for one call in isolation. A request that names its own
output cap — `max_tokens`, `max_completion_tokens` or `max_output_tokens` —
cannot produce more output than that, so its worst case is known before it
goes out: the cap at the model's output rate, plus the request's input at the
input rate. The input is every field the provider bills as input, estimated at
four characters a token: chat `messages` (and the tool calls in them), the
Responses API's `instructions` and `input`, Anthropic's `system`, and the tool
definitions. The formula is public as `runbound.pricing.admission_worst_case`. When that worst case would take `total_cost_usd +
spend_offset_usd + reserved` past `budget_usd`, the call is refused right
there, before any socket opens, with `GuardrailTripped` (detector `budget`,
`details["reason"] == "reservation"`, plus `cap_tokens`, `worst_case_usd`,
`reserved_usd` and `remaining_usd`). The session's settled spend is
untouched.

**The reservation holds.** A call that is admitted does not just get compared
against the budget once — its worst case is *held* on the session's ledger for
the lifetime of the call, given back only when the call closes out, however it
ends: an ordinary return, a provider error, or — for a streamed response — the
stream being exhausted, closed early, failing mid-flight, or abandoned and
garbage-collected. Until then, `settled + reserved` is what every other
concurrent call is compared against, not settled alone — which is the whole
point: N calls racing the budget edge at once are not all admitted just
because none of them has *settled* yet. Against a `budget_usd` of `R`
remaining and a worst case of `W`, exactly `floor(R / W)` of N concurrent
calls are admitted; the rest are refused with `details["reserved_usd"]`
naming what the others are holding. `reserved` is **worker-local** — this
process's own in-flight money, never folded into the fleet's spend and never
sent to the control plane — readable at any moment as
`runbound.budget().reserved`.

```python
runbound.init(budget_usd=5.0, on_anomaly="raise")   # "capped" is the default
```

**Tokens ride the same reservation, but not the same reaction.** With a token limit in force
(`max_total_tokens`, which a control plane can only tighten by stating `budget_tokens`, and
`run_max_total_tokens`), the same stage also holds the call's worst case in tokens:
`ceil(characters / 4)` of the request plus its output cap
(`runbound.pricing.admission_worst_case_tokens`). It needs no price, so a model with none is
reserved too. Both holds are taken together or not at all, and a call whose worst case is
exactly what remains is admitted. A call that would cross the limit is **refused at the door
only under `on_anomaly="raise"`**: `GuardrailTripped` with `decision.boundary == "tokens"` and
`details["reason"] == "reservation"`, plus `worst_case_tokens`, `reserved_tokens` and
`remaining_tokens`. Under `"warn"` (the default) or `"callback"` it records the same anomaly
and decision, notifies your observers and lets the call go out; the post-call wall then stops
the next call. The two limits differ on purpose. The dollar door is a hard stop by design: you
set a money budget, and since 0.4.0 the call that would cross it is refused before it goes
out, whatever `on_anomaly` says. The token door follows `on_anomaly` because it was added in
front of a limit that could already just warn. It is gated by `budget_admission` as the dollar
reservation is, and the envelope's stated-cap check and the wall enforce the same limit
without it, so a limit is never unenforced, only unreserved. **Admission is per worker; the
fleet total is applied at entry.** The hold is this worker's own, so two workers can each admit
a call that together crosses the budget, and the wall then stops the next one.

**The trade-off.** A call whose cap-priced worst case exceeds what is left is refused even if it
would have used less: with $0.10 left, a request capped at 15,000 output
tokens on a $10-per-million model is refused although its answer might have
cost a cent. That is the price of "cannot be crossed". Two limits are stated
rather than hidden: the output side of the worst case is exact, and the input
side is the same chars/4 estimate used everywhere else in runbound, so a prompt
much denser than four characters a token can still take the session past the
line by the difference; and a call with no stated cap is not reserved at all.
`budget_admission=False` restores 0.3.0.

**The three modes.**

| `budget_admission` | A request that states its output cap | A request with no cap |
|---|---|---|
| `"capped"` (default) | reserved: refused before it goes out when its worst case would cross | not checked before it goes out; the post-call wall stops the session after the call that crossed it |
| `True` | reserved, as above | estimated with `admission_output_tokens` (default **1024**) standing in for the missing cap — a guess — then held and refused on it (`details["rule"] == "admission"`), so under `True` an open stream holds an *assumed* cap's worth for as long as it runs |
| `False` | not checked before it goes out | not checked before it goes out — 0.3.0 |

The price table is the one the post-call check uses (or `custom_prices`), always
at the model's plain input rate even for a model with a cached-input rate:
nothing can know before the call how much of it will be a cache hit, so it never
assumes the discount.

Three things make this a door in front of the wall, not a second wall:

- **It never latches.** A refusal says nothing about the *next* call — a
  cheaper one, seconds later, may fit — so the session is untouched: totals
  unchanged, nothing tripped, free to try again immediately.
- **An unpriced model skips it, not refuses it.** A model with no known price
  cannot be priced ahead of time, and refusing a call for a cost runbound cannot
  compute would be the SDK inventing a limit you never set. It is warned once
  per model, and the post-call `budget` check still watches the call.
- **Every refusal is recorded**, kept apart from an ordinary post-call
  `budget` trip by `details["rule"]` (`"reservation"` or `"admission"`), so
  neither shadows the other. Identical refusals are recorded one by one up to
  100 per session, rule and tool, then summarised (see the changelog).

**A soft line under the wall.** A fraction of `budget_usd` — say 80% — warns
once, at the first call that takes the session past it (strictly greater,
like the wall): a `warn` anomaly, detector `budget`, `details["limit_hit"] ==
"budget_soft"`. It never stops anything and never latches. Its reaction can
also put the session in [safe mode](../guides/policy.md#classify-by-capability)
— its tools stop acting, its model calls go on — until spend is back under
the line or the key is cleared. A call that crosses the soft line and the
budget at once trips the budget, and the soft line stays quiet.
`budget_soft` and `on_budget_soft` are free, local `init()` keywords; a
connected plane can only lower the fraction (fire sooner) or escalate the
reaction (`"notify"` -> `"safe_mode"`), or state a line from scratch where
you left `budget_soft` unset — never loosen or remove one you configured.
`runbound.budget()` reads what is left, soft line included:

```python
view = runbound.budget()          # or runbound.budget("user:8842")
view.limit, view.spent, view.remaining, view.soft_at, view.reserved
```

---

## The envelope: what is refused at the door

`envelope=True` (the default since 0.4.0) extends the reservation's own idea
— refuse before the call goes out, rather than discover the overrun after —
to `max_steps`, `max_session_seconds` and the `max_total_tokens` half of
`budget` a stated output cap can check exactly, plus a limit for tool
calls, an action cap (`max_actions_per_run`), free and local like every
other envelope field:

| Wall | Door condition | Honors `on_anomaly`? | Latches? |
|---|---|---|---|
| `max_steps` | the call about to be made would be step `max_steps + 1` | yes | yes, exactly as the wall would |
| `max_session_seconds` | the run has already run past the limit | yes | yes |
| `max_total_tokens` | a request with a stated output cap would project the total over the limit | yes | no — a projection says nothing about the next call, the same reasoning as the reservation |
| `max_actions_per_run` | the tool action about to run would be action `cap + 1` | **no — always refuses** | yes |

**"Honors `on_anomaly`"** matters because the first three rows are not new
controls — they are `max_steps`/`max_session_seconds`/`max_total_tokens`
themselves, checked one call earlier — so they react exactly as the wall
behind them always has: `"raise"` **refuses right here, at the door**;
`"warn"` and `"callback"` both **only log/alert and let the call through** —
neither latches and neither calls your handler at the door itself. The call
that was let through still gets its own event recorded, and the wall behind
the door detects the very same crossing on it, for real, and reacts exactly
once — under `"callback"`, that is where your handler is actually invoked
and the session is latched, "one call later" than the door, exactly as it
always has been. A deployment that set `max_steps`
with `on_anomaly="warn"` to be told, not stopped, keeps working exactly as
before. The action cap is a genuinely new control with no prior behavior to
preserve, so — like the money reservation — it always refuses, regardless of
`on_anomaly`. Set `max_actions_per_run` at `init()`, free and local; a
connected plane can only lower the cap, or state one from scratch where
you leave it unset, never raise it.

Each refusal reuses the wall's own detector name (`steps`, `timeout`,
`budget`, `fanout`) so a [refusal profile](reactions.md) set for that
detector applies unchanged — but carries `details["rule"] == "envelope"`
(the action cap keeps `"actions"`) so it never dedupes away a *later*
trip by the same wall on the same session; without that, an observer could
see the door's warning and never learn the wall itself was actually crossed
by an uncapped call afterward. Every refusal **at the door** — steps, run
time, tokens, actions, circuit, unpriced model, money, posture, capability,
policy — now also carries a `Decision`: `exc.decision`, or
`anomaly.details["decision"]` on the wire — the verdict, which boundary of
the execution envelope it hit (`"steps"`, `"time"`, `"tokens"`, `"money"`,
`"blast_radius"`, `"posture"`, `"capability"`, `"circuit"`, `"policy"`,
`"concurrency"`), the detector, and the numbers behind it (`limit`, `used`,
`estimate`). A **post-call** detector trip — the wall itself firing after a
call already went out — does not carry one; `exc.decision` is `None` there.

An uncapped model call is not checked for `max_total_tokens` at the door at
all — exactly like the money reservation, only a request that states its own
cap can be checked exactly, so the post-call wall is still the only check for
one that does not. Money admission (`budget_admission`) and
posture/capability enforcement are unaffected by `envelope` either way — they
are their own, older opt-ins, not new envelope controls.

`envelope=False` restores 0.3.0 exactly: none of the four door stages above
run, and only the post-call walls ever fire. `runbound.envelope(key=None)`
reads the whole picture — money, steps, time, actions and capabilities — as
one object; see [How it works](../concepts/how-it-works.md#admit-execute-reconcile-record).

---
