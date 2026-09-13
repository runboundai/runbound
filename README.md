# runbound

**Runtime controls for autonomous AI agents.** runbound runs inside your
agent's process and stops runaway cost, loops, tool abuse and provider
failures deterministically, by counting, timing and hashing, never by asking a
model whether something looks wrong. It reads no prompts and no replies, it
puts no gateway in your hot path, and it never speaks to your users: when it
stops something it hands control back to you and you answer in your own voice.

This is for the platform team that owns AI agents in production, the people
who get paged when one runs away, not the people who wrote its prompt.

## Three levels of protection

You choose how far in to go. Each level is a small, separate step, and most of
the value arrives at the first one.

**Level 1: guard the model calls.** Two lines. `runbound.init()` wraps the
OpenAI and Anthropic clients for you (or you call `runbound.wrap(client)` on
any client with that shape). From then on every model call is counted, timed
and priced, and you get: dollar and token budgets, per-call caps, velocity
limits, step and wall-clock limits, the spike ladder that learns what a session
normally looks like and reacts when it changes, error-storm detection with a
circuit per provider, and refusal profiles that carry your own status and
sentence. It also reads the tool calls the model *asks for* out of each answer,
so a loop of identical tool requests is caught before your code dispatches it.
All of that is what the free SDK does on its own, with no network call and no
account: every anomaly reaches your process the way you asked for it in
`on_anomaly` — a raised `GuardrailTripped` your own handler catches, your
callback, or a WARNING line in your logs — and `on_trip` decides whether the
session stays stopped afterwards. Forever, offline. Being *told* about it
without reading your own logs or writing your own handler — Slack, PagerDuty,
a signed webhook — is a paid feature: it starts with a token from
your runbound dashboard (see
[Fleet mode](#fleet-mode--one-truth-across-all-your-workers-control-plane)).
Nothing is decorated, no policy is written.

```python
import runbound
runbound.init(budget_usd=5.0, on_anomaly="raise")   # everything below is now guarded
```

**Level 2: one session per run.** One line per unit of work. Wrap each run in
`runbound.session(key)` — the key is any id you already have: a run id, a job,
a tenant, a customer — and every control above becomes per key instead of per
process: this run's budget, this run's ladder, this run stopped, every other
caller untouched. With the control plane the key's budget and latch hold across
every worker that serves it.

```python
with runbound.session(f"run:{run_id}"):
    answer = client.chat.completions.create(...)
```

**Level 3: refuse actions before they run.** Only for the tools that matter.
Agents have stopped only writing text: they send the email, issue the refund,
delete the record. Decorate the two or three functions that are irreversible
or reach the outside world with `@runbound.tool`, write a policy (deny this
tool, at most once per run, needs approval, or a predicate of your own over
the arguments, run in your process), and the call is refused *before the
function body runs*. Roll a policy out in dry run first and read what it would
have blocked. The rules are yours; runbound enforces and records them, it
never judges the action.

```python
@runbound.tool
def issue_refund(user: str, amount: float): ...

runbound.init(tool_policy={"max_calls": {"issue_refund": 1}, "on_violation": "block"})
```

Levels 1 and 2 protect against the failures that are about behavior: a loop
that never ends, a spend that never stops, a model that changes under you, a
provider that fails and takes your retries with it. Level 3 protects against
the failures that are about actions. Nothing here protects against a *bad
answer*: hallucinations, prompt injection and output quality are out of scope
by design. "The agent said something wrong" is a different product. "The agent
would not stop" is this one.

## Why this exists

One agent burned **$2,847 in four hours** on a refactoring loop while every
monitoring dashboard stayed green. A four-agent loop ran for **11 days and cost
$47,000**; a budget alert fired on day 9, two days too late. In both cases the
telemetry worked. Nobody was watching it at 3am, and nothing in the stack had
the authority to pull the plug. The same failure arrives at higher volume when
one service serves many callers: a chatbot free-rider works out that your
support assistant will answer anything and spends your API key on their
homework, or a provider update flips the model into thinking mode and a
two-second answer becomes a seventy-second one across every run you serve.
Nothing errors. Nothing pages.

runbound is the layer with the authority to pull the plug, and the proof
that it did. It raises on the agent's own thread, in the middle of the loop,
rather than reporting it in the morning. Every reaction is a choice you made in
plain configuration; every refusal is a row in a ledger that holds the tool
name and the rule, never the arguments.

Try it offline, no API key needed:

```
.venv/bin/python examples/runaway_demo.py         # 10 seconds, no API key needed
.venv/bin/python examples/chatbot_abuse_demo.py   # the free-rider story, offline
.venv/bin/python examples/policy_demo.py          # actions refused before they ran
```

---

## What it controls

Five areas, each a runtime control — it changes what the agent is allowed to
do, not just what you can see about it:

| Area | Controls |
|---|---|
| **Cost** | `budget_usd` / `max_total_tokens` budgets, `max_cost_per_call_usd` / `max_tokens_out_per_call` per-call caps, `tokens_per_minute_limit` velocity, `on_unpriced_model` unknown-model policy |
| **Execution** | `max_steps`, loop detection (`loop_threshold`, `loop_window`; `repeatable=True` / `loop_ignore_tools` for tools meant to repeat), `max_call_seconds` / `max_session_seconds` timeouts, `max_active_sessions` / `max_session_depth` / `max_child_sessions` fan-out, `max_inflight_calls` concurrency, spike detection (`on_spike`, notify by default) |
| **Authorization** | `tool_policy` deny/allow, `max_calls`, `constraints`, `require_approval` + `approval_callback`, org-wide action policy with `dry_run` rollout (fleet mode) |
| **Reliability** | per-endpoint provider circuits (`on_provider_failure`, `circuit_*`), fleet halt (`on_halt`), `on_plane_loss` and `stale_halt` policy, fail-open on every internal error |
| **Governance** | central policy versions (fleet mode), refused-actions ledger, estimated exposure prevented, `plane_status()` / `fleet_status()` fleet state, `refusals` refusal profiles |

[Configuration reference](#configuration-reference) has every knob;
[What happens when something
trips](#what-happens-when-something-trips--every-choice-in-one-place) has the
full reactions table.

---

## How it works

Your agent keeps calling its LLM client and its tools exactly as it does today.
`runbound.wrap(client)` is [the canonical mechanism](#wrap-is-canonical-auto_wrap-is-a-convenience):
it patches one client's `create` method in place, works on any OpenAI- or
Anthropic-shaped client, and is what everything else builds on.
`runbound.init()` with `auto_wrap` (the default) is the convenience: it
calls `wrap()` for you on the OpenAI and Anthropic SDK classes themselves, so
a client built after it is guarded already with no explicit call. `@runbound.tool`
decorates your functions. Every model call and tool call that passes through
one of those emits a small immutable event into an in-process session — one per process by
default, or one per session key inside a `runbound.session()` block. A model
call that *fails* emits one too (`llm_error`), and so does every tool call the
model **asks** for in its answer (`tool_request`), before your code dispatches
it. Seven detectors read that session after every event; the first anomaly
triggers your configured reaction (log, raise, or your own callback) and fires
any alerts you configured. A guarded tool call is also checked against your
[action policy](#action-policy--rules-for-what-your-agent-may-do), if you set
one, before the function body runs. Failed calls are counted a second time
against [the provider's own circuit](#retry-storms-and-the-provider-circuit-breaker),
which is process-wide rather than per session.

```
   your agent
       |
       |  client.chat.completions.create(...)      @runbound.tool
       v                                                  |
  wrapped client  --------> Event(step, tokens, cost, duration) <----+
       |                    llm_call | llm_error | tool_request
       |                    tool_call | tool_error
       |                              |
       |                              v
       |            SessionState (counters, sliding windows, call baseline)
       |                one per process, or one per session key
       |                              |
       |                              v
       |    loop | budget | velocity | steps | spike | error_storm | timeout
       |                    (pure functions, no LLM)
       |                              |
       |                       Anomaly detected
       |                         /          \
       |             Slack / PagerDuty     warn | raise GuardrailTripped | callback
       |             (background thread)         (stops the agent)  (your kill switch)
       |
       +--> failed call --> provider circuit (process-wide, per provider)
                                     |
                        open --> CircuitOpen before the next call
                                 (only under on_provider_failure="open")
```

---

## What the SDK actually sees — and what it never sees

`runbound.init()` stores your configuration, builds the engine, starts a
session, and — with `auto_wrap` left at its default `True` — patches the OpenAI
and Anthropic SDK classes so clients built afterwards are guarded. By itself it
computes nothing. Everything runbound knows arrives through three sensors:

1. **A guarded client** — auto-wrapped at `init()`, or handed to
   `runbound.wrap()` — which sees each model call go out and come back.
2. **`@runbound.tool`**, which sees an action before its body runs and again
   when it returns or raises.
3. **`runbound.session(key)`**, which says whose work this is.

If none of them is in the path, nothing is guarded, and every detector reports
green because it has nothing to read. `runbound.coverage()` and
`runbound.assert_guarded()` exist so you never have to take that on faith.

### Where each number comes from

| Data point | Where it comes from | Blind if you skip… |
|---|---|---|
| Tokens in / out / reasoning / cached | `response.usage` on a guarded call — `prompt_tokens`/`input_tokens`, `completion_tokens`/`output_tokens`, `*_tokens_details.reasoning_tokens`, and the cached-input count (OpenAI's `prompt_tokens_details.cached_tokens`/`input_tokens_details.cached_tokens`, Anthropic's `cache_read_input_tokens` — see [Cached input tokens](#cached-input-tokens)). `estimate_tokens=True` only fills in when the server sends no usage at all, and never guesses a cache hit. | a guarded client. Use `record_call()` or `@runbound.llm` for calls runbound did not make — neither reports cached tokens, so those calls price every input token at the full rate. |
| Estimated cost | Those tokens × the static price table, or your `custom_prices` — cached tokens at the table's cached rate where it has one, else the full input rate. | anything that makes tokens blind — and it reads `$0.00` for a model with no price. |
| Call duration | A stopwatch around the guarded call. | a guarded client. Also blind under the LangChain handler, which reports no timing. |
| Provider errors → `error_storm`, circuits | The exception raised inside the guarded call. | any call runbound did not make. `record_call(..., error=exc)` reports one by hand. |
| Tool calls the **model asked for** → loops | Parsed off the response: `tool_calls`, Responses `function_call` items, Anthropic `tool_use` blocks. | a guarded client. |
| Tool name + argument hash **you executed** → loops, action policy, `max_calls` | `@runbound.tool`, or the LangChain handler. | any tool that is not decorated. |
| Which run this is → per-key limits, spike baselines, the ladder | `runbound.session(key)`. | without it every call lands in the one process-wide default session, so one key's spike is measured against everybody's traffic. |
| In-process inference (no HTTP call to read) | `@runbound.llm` or `runbound.record_call()`. | nothing else can see it. |

### What it never reads

Prompts and model replies are never read, stored, or sent — except the one
narrow case of `estimate_tokens=True`, which counts characters when the server
reported no usage. Tool arguments are hashed with sha256 and the digest is what
is kept; an [action policy](#action-policy--rules-for-what-your-agent-may-do)
hands your own callbacks the real arguments for the duration of that call and
stores none of them. Failures are recorded as the exception's class name.
Details are in [Privacy](#privacy).

### Paths that are not guarded today

- Raw HTTP to a provider — `requests`, `httpx`, or your own transport.
- The Google Gemini / Vertex SDK, Bedrock via `boto3`, and the Mistral and
  Cohere SDKs.
- Any framework that reaches the provider through its own transport rather than
  through an OpenAI- or Anthropic-shaped client. (LangChain is covered by
  [its own handler](#langchain--langgraph).)

For all of these, `runbound.record_call()` and `@runbound.llm` are the way
in: they take the same path a guarded client does, so budgets, spikes, limits
and circuits all see them.

### `wrap()` is canonical; `auto_wrap` is a convenience

`runbound.wrap(client)` — patching one client object you hand it — is the
mechanism the rest of the SDK is built on: it always works, on any client
whose shape it recognizes, whenever you call it. `auto_wrap` (on by default)
is a convenience layered on top: it patches the OpenAI and Anthropic SDK
*classes themselves* at `init()` time, so a client built afterwards is guarded
without a `wrap()` call anywhere in your code. Convenient, but class patching
is a guess about how your code constructs clients, and a few real situations
guess wrong:

- **Multiple SDK versions or code paths** — a class patched in one imported
  copy of `openai`/`anthropic` does nothing for a client built from a
  different copy (a vendored dependency, a plugin with its own pinned
  version).
- **Framework wrappers around the client** — some frameworks construct their
  own client internally and expose only their own call surface, so there is
  no client object left in your code for a class-level patch to reach through.
- **Test mocks** — a `unittest.mock` or a hand-rolled fake stands in for the
  real class in tests, so patching the real class guards nothing there (which
  is usually what you want in a test, but worth knowing rather than assuming).
- **Unusual init order** — a client built *before* `runbound.init()` runs
  (module-level construction, an import-time singleton) was built before the
  patch existed, so `auto_wrap` never touches it, ever.

`wrap()` on the actual client object sidesteps all four — there is no
guessing about which class or which import path, only the object in front of
you. When you are not certain `auto_wrap` reached everything, `wrap()` the
client by hand. `assert_guarded()`, next, only catches the *total* miss — a
provider SDK imported with zero guarded calls recorded; a client that is
guarded for some call sites and not others still passes it. The honest check
for a partial miss is `runbound.coverage()["guarded_calls"]` against the
call volume you actually expect, plus `wrap()` on the object you are unsure
about.

### How to be sure

```python
import runbound
from openai import OpenAI

runbound.init(budget_usd=5.0)

client = OpenAI()          # built after init(): auto_wrap already guards it

runbound.coverage()
# {'auto_wrapped': ['openai'], 'wrapped_clients': 0, 'decorated_tools': 3,
#  'guarded_calls': 12, 'tool_calls_seen': 7, 'keyed_sessions_seen': 4,
#  'providers_imported': ['openai'], 'providers_unguarded': [],
#  'last_guarded_call_age_s': 0.8, 'warnings': []}

runbound.assert_guarded()   # raises RuntimeError if a provider SDK is
                              # imported and no guarded call has been recorded
```

`coverage()` is the honest answer to "is this actually on?" — put
`assert_guarded()` in your startup path or your CI smoke test and a forgotten
`wrap()` fails loudly instead of quietly. You also get told without asking: if
a provider SDK is imported and no guarded call has landed 60 seconds after
`init()`, runbound logs a warning once. `coverage_check_seconds` moves that
deadline; `None` turns the check off.

---

## Quick start (under 5 minutes)

**1. Install.**

```bash
pip install runbound
```

To build from source instead:

```bash
git clone https://github.com/runboundai/runbound && cd runbound
python -m venv .venv
.venv/bin/pip install -e .
```

There are no runtime dependencies — stdlib only. LangChain support is the one
extra: `pip install "runbound[langchain]"`.

**2. Wire the three sensors** around one agent run — only the first is
required:

```python
import runbound
from openai import OpenAI

runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")
client = runbound.wrap(OpenAI())

@runbound.tool
def search(query: str) -> str:
    ...

with runbound.session(f"run:{run_id}", tags={"service": "research-agent"}):
    agent.run(task)   # every model call and every @tool inside it is bounded
```

That run may spend $5 and take **50 model turns**. `max_steps` counts turns:
one `create()` call is one step however many tools the model then asks for and
your code then dispatches. The knob that counts everything instead — model
calls, tool calls, tool requests, failures — is `max_events`, and a tool-using
run records several of those per turn. They are two different walls, not one
wall at two thresholds; set either, or both. See [`max_steps` and
`max_events`](#max_steps-and-max_events-steps-are-turns-events-are-events).

`wrap()` returns the same client object it was given, so nothing downstream
changes — and with `auto_wrap` at its default you can drop it entirely for an
OpenAI or Anthropic client built after `init()`. Keep it for the unusual cases:
a gateway or proxy object, or a client constructed before `init()` ran. What
`init()` cannot do for you is the other two sensors: a tool is only guarded if
it carries `@runbound.tool`, and limits are only per key inside
`runbound.session(key)`. [What the SDK actually sees](#what-the-sdk-actually-sees--and-what-it-never-sees)
lists exactly which numbers go blind without each one, and
`runbound.coverage()` tells you what is instrumented right now.

**3. Check what is actually guarded.** An unguarded path looks exactly like a
quiet one; no incidents is not the same as no risk. Put this in your startup
path, or your CI smoke test:

```python
runbound.coverage()          # what's actually instrumented right now
runbound.assert_guarded()    # raises loudly if a provider SDK is imported
                                # but no guarded call has been recorded
```

`coverage()` reports `auto_wrapped` labels, `wrapped_clients`,
`decorated_tools`, `guarded_calls`, `tool_calls_seen`, `keyed_sessions_seen`,
`providers_imported`, `providers_unguarded` (imported but never seen guarded),
`last_guarded_call_age_s`, and any `warnings` — read it to know the guard is
on rather than assume it. See [How to be sure](#how-to-be-sure) for the full
picture, including the once-per-process warning that fires on its own if a
provider is imported and nothing guarded has happened.

**4. Catch the trip** wherever you want the agent to stop:

```python
from runbound import GuardrailTripped

try:
    run_agent()
except GuardrailTripped as tripped:
    print(tripped.anomaly.detector, tripped.anomaly.message)
    shut_down_cleanly()
```

Two subclasses let you narrow that catch when you want to tell the cases apart:
[`PolicyViolation`](#action-policy--rules-for-what-your-agent-may-do) (the agent
tried something your rules forbid) and
[`CircuitOpen`](#retry-storms-and-the-provider-circuit-breaker) (the provider is
down and we failed the call fast). Both are `GuardrailTripped`, so the broad
catch above keeps working unchanged.

See it work, offline, with no API key and no `openai` package installed:

```bash
.venv/bin/python examples/runaway_demo.py
.venv/bin/python examples/policy_demo.py
```

The first runs a deliberately looping agent, stops it on the third identical
tool call before that call executes, then runs a spend spiral and stops it on
the budget cap. The second is a support agent that tries four things the
business wrote down that it may not do — a deny list, a per-session call cap,
an argument constraint and an approval gate — and each one is refused before
the function body runs.
`examples/openai_agent.py` is the same wiring against a real OpenAI key.

The second worked example — many people behind one service, each on their own
key — is in [Runs keyed by any id](#runs-keyed-by-any-id) below.

---

## Runs keyed by any id

One process usually does more than one thing at a time, and "the process spent
too much" is rarely the useful sentence. `runbound.session(key)` scopes every
control above to whatever id you pass it — an agent run, a background job, a
tenant, a customer — so counters, baselines and trips are per key:

```python
with runbound.session(f"run:{run_id}", tags={"service": "refunds-agent"}):
    response = client.chat.completions.create(model="gpt-4o", messages=messages)
```

- **The key is yours and opaque to us.** runbound stores it, prints it in
  alerts, and never interprets it. Send a run id, a job id, a tenant id, a
  customer id, a hash — identity stays the business's. `tags` is a small dict
  of labels that rides along with it.
- **The same key is the same session.** Re-entering a key in a later request
  returns the state it already had, so budgets and learned baselines survive
  from request to request — a retried job, or a second turn of the same
  conversation. Tags given on a later entry are merged in.
- **Everything is per key**: step count, tokens, dollars, the loop window, and
  the spike baseline.
- **A trip stops that session only.** The key that spikes gets a
  `GuardrailTripped` on the call that crossed the line; every other run carries
  on, because detectors fire per session and the other sessions never tripped.
- **A tripped session stays tripped.** The wall is a latch, not a speed bump:
  once a session trips critically, every later call re-raises (or re-invokes
  your callback), and under `on_anomaly="raise"` even *entering*
  `session(key)` raises — so a blocked key costs you **zero** model calls
  from its next request on. Check without tripping via
  `runbound.is_tripped(key)`; forgive explicitly with
  `runbound.clear(key)` (fresh budget, fresh baseline, detectors re-armed).
- **The latch is permanent by default — and that is a choice you can change.**
  Out of the box a tripped session stays tripped until you `clear()` it or the
  process restarts. If a false-positive trip should heal on its own, **opt
  in** with `latch_ttl_seconds=3600`: the latch lifts that long after it was
  set, every detector is re-armed, and the session's next event is judged
  fresh — on the same cumulative counters. Re-admits, does not reset: a
  session still over budget re-trips immediately, with the same detector,
  which is the wall the setting promises. It is **not** a windowed budget
  that zeroes on a schedule — that is a different, unbuilt feature — and
  nothing expires unless you set this. `runbound.clear(key)` is the one call
  that actually zeroes the counters.
- **Key and tags reach your alerts.** Slack messages carry a `key: ... | tags:
  ...` line, PagerDuty puts `session_key` and `session_tags` in
  `custom_details`, and a spike anomaly carries them in
  `anomaly.details["key"]` / `["tags"]`.
- Blocks nest, and the enclosing session is restored on exit. Outside any
  block, work is accounted to the default session `init()` created — nothing
  changes for a single-agent process.
- At most `max_sessions` keys are kept (default 10,000, least-recently-used
  evicted); re-entering an evicted key simply starts it over.
- **Inert before `init()`.** The block yields `None` and observes nothing,
  exactly like the rest of the SDK.

**The second example: a chatbot.** The same call, keyed by the person rather
than by the run — one process serving many people, each with their own budget,
baseline and latch, so the one who spikes is stopped and everybody else keeps
chatting. runbound never writes the reply: it stops the session and hands
control straight back to you.

```python
from runbound import GuardrailTripped

def handle(user_id: str, message: str) -> str:
    with runbound.session(f"user:{user_id}", tags={"plan": "free"}):
        try:
            return call_model(message)
        except GuardrailTripped as tripped:
            log.warning("runbound: %s", tripped.anomaly.message)
            return "You've reached today's assistant limit — a human will follow up."
```

`examples/chatbot_abuse_demo.py` runs that story offline: three people on their
own keyed sessions, one of whom pushes the model into thinking mode, with
`on_anomaly="raise"` as the only configuration — a quiet notice on the first
odd call, a stop on the second, and the other two left alone. For a dress
rehearsal over HTTP — a FastAPI service, a normal caller, a repeat-offender key
that gets latched and refused pre-spend, a support `clear()`, and a heavy but
genuine caller who is watched and never blocked — see
[`examples/stress/`](examples/stress/README.md). It runs with no API key and
also against a real one, and shows the multi-worker limitation honestly.

**Async tasks inherit the session; new threads do not.** The binding is a
`contextvars.ContextVar`, so an `asyncio` task copies the context it was created
in, while a thread starts with an empty one. Enter the block inside the worker:

```python
# asyncio: tasks created inside the block see it
with runbound.session(key):
    await asyncio.gather(step_one(), step_two())

# threads: the block goes inside the thread's own function
def worker(key: str) -> None:
    with runbound.session(key):
        do_work()
```

---

## Fleet mode — one truth across all your workers (control plane)

Everything above happens inside one process. That is exactly right until you
run more than one of them — and then every number quietly becomes a number
*per replica*:

- **The budget multiplies.** `budget_usd=5.0` across eight workers is a $40
  budget. The key you meant to cut off at $5 gets $5 from each of them.
- **A blocked key comes back on another worker.** A latch lives in the process
  that set it. The repeat-offender key you stopped hits the load balancer
  again, lands on a replica that has never heard of it, and carries on.
- **Nothing learned survives.** Strikes and cooldowns, `max_calls` tallies,
  provider circuits and fan-out counters are each rediscovered N times over,
  and forgotten at the next deploy.

**Fleet mode gives the fleet one memory.** Point the SDK at a control plane and
every worker asks the same question at the same two moments — a session opens,
a session closes — so the eight replicas serving `user:42` share one budget,
one latch, one strike count, one org policy and one set of provider circuits.
Detection itself does not move: the detectors still run in your process, on
your thread, with no model calls, exactly as they do now. The plane only tells
each worker what the *other* workers already know.

Those five are the whole list, and the list is honest: spike baselines, the
`max_calls` tally and the fan-out / in-flight counters are still per worker —
see [the limitations](#guarantees-and-limitations).

```python
import os
runbound.init(token=os.environ["RUNBOUND_TOKEN"],
                service="support-bot", budget_usd=5.0, on_anomaly="raise")
```

That is the whole change for the hosted case: a non-empty `token`, with no
`control_plane_url` anywhere — not passed, and not in `RUNBOUND_PLANE_URL`
— is `plane_mode == "hosted"`, and you never have to name `control_plane_url`
yourself. Set `control_plane_url` too, from either source, and that is
`"self_hosted"` instead, not hosted — it always needs a token, and
`token=""` is how you state a self-hosted plane with no auth, since an empty
token never invents a url. `service` names the fleet this process belongs
to; `worker_id` names this process inside it and defaults to
`hostname:pid:xxxxxx` (six random hex characters, so two replicas on one
host never share an id).

### When the plane is contacted — and when it is not

| Moment | What goes out | What it costs your request |
|---|---|---|
| `init()`, then every `control_plane_poll_s` (default **5 s**) | a heartbeat: service, worker id, SDK version, the policy version this worker has, its circuit states, its open session count | nothing — a daemon thread |
| **entering** a `runbound.session()` block | one entry question, on your thread | at most `control_plane_timeout_s` (default **150 ms**), and only on a cache miss: one key's answer is reused for `control_plane_cache_s` (default **5 s**), so a retry loop of blocks costs one request rather than a hundred |
| **leaving** a `runbound.session()` block | this block's spend / token / step delta, queued | nothing — a dict copy; a background thread posts it in batches |
| a **critical trip** | the trip, on your thread, so the other workers refuse this key on *their* next request | at most `control_plane_timeout_s`; a trip that does not get through falls back to the batch queue, ahead of ordinary events |
| a provider **circuit** opening or closing | queued on the priority lane | nothing |

**Never per model call, and never per tool call.** An auto-wrapped client, a
`wrap()`ped client, `@runbound.tool`, `@runbound.llm` and `record_call()`
open no sockets at all — at most they append to an in-memory queue. A run that
makes a thousand model calls inside one `session()` block talks to the plane
exactly once, and there is a test that asserts it with a spy on the client.

**A plane that is down changes nothing about your guarding.** Every call to it
is bounded, caught, and answered locally on failure:

- one warning a minute, not one per session;
- after **3 failed calls in a row** the link is *degraded* and stops calling
  altogether, spending one probe every **30 s** to find out when the plane is
  back;
- `runbound.plane_status().mode` reads `"degraded"`, so a health endpoint can
  say so out loud;
- a token the plane rejects (401/403) is terminal until the process is
  reconfigured — a bad token costs one request, not one per session;
- every detector, latch, cap and policy you configured locally keeps working —
  the single-process behavior the rest of this README describes.

Even the heaviest instruction a plane can give fails open: a fleet-wide halt is
enforced only while we are still hearing it, and stops being enforced **60
seconds** after the last successful contact. A control plane that dies must not
take the fleet down with it.

```python
# an unreachable plane, on purpose
runbound.init(control_plane_url="http://127.0.0.1:9", token="ag_live_x",
                budget_usd=0.01, on_anomaly="raise")

with runbound.session("user:1"):
    runbound.record_call("gpt-4o", tokens_in=5000, tokens_out=5000)
# GuardrailTripped: Budget exceeded: $0.0625 spent, limit $0.0100

runbound.plane_status()
# PlaneStatus(mode='degraded', last_contact_age_s=None, consecutive_failures=3, notice=None)
```

### What fleet mode adds

- **A bounded fleet budget, not a hard one.** The entry answer carries what
  the rest of the fleet has already spent under this key; the SDK folds it in
  as an offset, and the `budget` detector counts local spend **plus** that
  offset. In a single process the cap is exact — the check is a plain
  comparison against a running total. Across a fleet it is a *bound*: a
  **turn** is one `runbound.session()` block on one worker, entry to exit.
  A worker learns the fleet's spend only at block entry and reuses that
  answer for `control_plane_cache_s` (5 s default); other workers' spend
  reaches the plane through exit deltas posted in batches (about one
  second), so a worker can be stale for up to the cache window plus that
  batch latency. Worst-case overspend for one key is the sum over workers of
  (the spend of that worker's one in-flight block, plus anything it admitted
  while stale) — the full statement and the tests that assert it live in
  [INVARIANTS.md](INVARIANTS.md#budget). Keep blocks short (one request per
  block) and lower `control_plane_cache_s` to tighten the bound, at the cost
  of more entry requests. The worker that crosses the line trips
  on the same turn a single worker with those numbers would; the anomaly
  says where the money went: `details["fleet_spend_offset_usd"]` and
  `["fleet_tokens_offset"]`. `max_total_tokens` works the same way.
- **A shared latch and shared strikes.** A trip is reported synchronously, and
  the next worker to open that key adopts the latch as if it had set it itself —
  **within `control_plane_cache_s` (5 s) plus one turn**: a worker holding a
  cached answer for that key finishes the turn it is on, and the entry after
  the cache expires is the one that is refused. That window is the knob; lower
  it to tighten propagation at the cost of more entry requests.
  The latch arrives with what is left of its ttl, so the key is let back in
  when the *fleet's* cooldown runs out rather than when one worker happened to
  hear about it. Under `on_anomaly="raise"` that means the block is
  refused before it runs, which is what makes a blocked key cost zero model
  calls fleet-wide. The abuse ladder's strike count travels with it, so a
  rollover earned on worker 3 tightens the allowance on worker 7.
- **Org-wide action policy.** The plane states one rule set per `service`, and
  the SDK merges it with your local `tool_policy` **most-restrictive-wins**:
  bans unioned, allow-lists intersected, `max_calls` the lower of the two.
  Callables never come off the wire, so your `constraints` and
  `approval_callback` stay exactly as you wrote them. A violation of an org rule
  reports `details["origin"] == "org"`. A new version is picked up on the
  heartbeat, on the poller's thread, with no redeploy.
- **Dry-run rollout.** An org policy the plane marks `dry_run` is logged and
  alerted and **never blocks** — how a platform team rolls a rule out across a
  fleet before turning it on. Your local rules are untouched by it and keep
  blocking.
- **Fleet circuits.** The heartbeat can open or close a provider circuit on
  every worker at once, so one provider outage is discovered once for the fleet
  instead of N times. What an open circuit *does* is still your
  `on_provider_failure`: `"notify"` alerts and lets calls through, `"open"`
  raises `CircuitOpen` before the call.
- **A fleet kill switch.** The plane can halt everything. `on_halt="raise"` (the
  default) refuses every guarded `session()` block with a `GuardrailTripped`
  carrying detector `halt`; `on_halt="warn"` lets the business run and says so
  at most once a minute — which is what you want while you are still proving the
  switch reaches your workers.

### What we send — hashes and counts, never content

Fleet mode is the first thing in runbound that opens a socket we own, so here
is all of it. Every outbound record is built from one module,
`runbound/plane_types.py`, which is what makes this table checkable by reading
a single file.

| Endpoint | When | Fields |
|---|---|---|
| `POST /v1/hello` | every `control_plane_poll_s` | `service`, `worker_id`, `sdk_version`, `policy_version_seen`, `circuits` (`{label: "open"\|"half_open"\|"closed"}`), `active` (open `session()` blocks), `coverage` (the counts `runbound.coverage()` shows), `tools_hash`, and `tools` — the tool report — only when that hash changed |
| `POST /v1/enter` | a `session()` block opens, on a cache miss | `key_hash`, `tags`, `service`, `worker_id`, `budget_usd`, `local_spend_usd`, `local_total_tokens` |
| `POST /v1/trip` | a critical trip latches a session, and every block refused at the door because a key is latched | `key_hash`, the anomaly (`detector`, `severity`, `message`, scrubbed `details`, `reacted`), `latch_ttl_s`, `strikes`, `generation`, `refused_at_door` |
| `POST /v1/events` | batched in the background; the `exits` and `circuits` lanes always, the `events` and `anomalies` lanes while `export_events` is on | `service`, `worker_id`, `sent_at`, `dropped`, and four lanes — `events` (`kind`, `step`, `tokens_in` / `tokens_out` / `tokens_reasoning`, `cost_usd`, `model`, `tool_name`, `args_hash`, `duration_s`, `error_class`), `anomalies`, `exits` (`key_hash`, `seq`, `spend_delta_usd`, `tokens_delta`, `steps_delta`, `tool_calls`, `events_delta`, `errors_delta`, `tokens_cached_delta`, `last_detector`, `trigger_message`, `trigger_age_s`), `circuits` (`label`, `state`, `failures`, `cooldown_s`) |
| `GET /v1/policy?service=…` | the heartbeat announced a new policy version | nothing but the service name |
| `POST /v1/clear` | `runbound.clear(key)` | `key_hash` |

The **tool report** is the one record built from your code rather than from
your traffic: for every `@runbound.tool` in the process, its name, its
parameters' names, each annotation rendered as a string, whether each parameter
has a default (never *what* the default is), the first line of its docstring,
its defining module and the `rules` its decorator stated — plus a
`decorated: false` entry for every tool name a model asked for that no
decorator declared, which is exactly the coverage gap worth seeing. Read it
yourself with `runbound.tools()`; it is the same list the heartbeat carries.

```python
[{"name": "issue_refund",
  "decorated": True,
  "params": [{"name": "user", "annotation": "str", "required": True},
             {"name": "amount", "annotation": "float", "required": True}],
  "doc": "Refund a customer.",
  "module": "acme.tools",
  "rules": {"max_calls": 1, "constraint": "acme.tools:under_500"}}]
```

A tool that states no rule reports `"rules": {}`. A `constraint` or
`require_approval` travels as `"module:qualname"` and **never as the function
itself**: your predicates run in your process, are never shipped, and are never
called anywhere else.

`tools_hash` — 16 hex characters of a sha256 over that list — rides every
heartbeat; the list itself rides only when the hash changed, or when the plane
answers that it has none for this worker. A deploy costs one payload; the five
seconds after it cost a hash. At most 500 tools are reported, sorted by name.

An `exits` entry is a delta — what one `session()` block added since this
worker's last report for that key — and its numbers keep the meaning stated
elsewhere in this README: `steps_delta` is model turns, `events_delta` the
raw count of everything recorded (a tool call included), `errors_delta` how
many `llm_error`/`tool_error` events, `tokens_cached_delta` how many of
`tokens_delta` were served from a provider's cache. `last_detector`,
`trigger_message` and `trigger_age_s` describe the anomaly that last stopped
the session — all three `None` for a session that never tripped, which is
every session under `on_anomaly="warn"`. `trigger_age_s` is this worker's own
monotonic clock, in seconds, because its clock and the plane's are not the
same one; the plane converts it to a timestamp on arrival, on its own clock.

What is **never** on that wire, because there is no field for it to travel in:

- **prompts and replies** — not read, not stored, not sent;
- **defaults, return values and the rest of a docstring** — the tool report says
  a parameter *has* a default, never its value, and carries a docstring's first
  line and nothing after it: the lines after the summary are where hostnames,
  credentials and customer examples live;
- **tool arguments** — only `args_hash`, the same salted sha256 digest the loop
  detector compares. The salt is a random value generated once per process, so
  the digest is an *equality token* good for spotting a repeat inside this
  process — not a fingerprint: the same call hashes differently in every
  process, on purpose, so two workers (or a leaked ledger) cannot correlate
  "who called what" by matching hashes across the fleet;
- **error messages** — only `error_class`, and only when the message *names* a
  class (`"RateLimitError: …"` → `RateLimitError`). A message with no such
  prefix sends `None` rather than a guess, because a guess would be a piece of
  the message, and the message may hold anything;
- **your session keys** — a key travels as `sha256(key)` and nothing else,
  unless you opt in with `send_session_keys=True`, which adds the raw key
  alongside the hash. Off by default. Detectors *do* name the key in their own
  `message` and `details["key"]`, because a local log with the key in it is the
  useful one — so the last thing done to an outbound anomaly, on the plane
  wire, is to replace every occurrence of the key with the first 12 characters
  of its hash and an ellipsis: `"3f2a9c1b04d7…"`. Two records about the same
  key still line up; neither carries the key itself — as long as
  `send_session_keys` is off. An `exits` entry's `trigger_message` is the same
  kind of sentence (it is, verbatim, the `message` of the anomaly that
  latched the session) and gets the same treatment before it leaves. Turn
  `send_session_keys` on and that redaction is skipped too: a detector's
  `message` and `details` — and an exit's `trigger_message` — now travel
  exactly as it was written, and the plane stores the raw key next to its
  hash. From there the plane's own alert adapters (Slack, PagerDuty, a
  webhook) do what you told them to: read the raw key into your own
  `link_template` wherever you wrote `{key}`, and pass a detector's
  unredacted `message`/`details` straight through like everything else in an
  alert. That is your choice about your own Slack, your own PagerDuty and
  your own endpoint — the hash is what ships unless you make it otherwise.

Your `tags` are the exception, and deliberately so: they are labels you chose,
so they travel exactly as you wrote them and are never redacted. Don't put a
raw key in a tag.

`tags` **do** travel, so a dashboard can group by them: at most 32 entries, keys
and values stringified and cut to 64 characters — label sessions with something
you are willing to see in a console. A detector's `details` dict is scrubbed to
JSON scalars: strings truncated to 256 characters, and anything that is not a
string, number, bool, `None`, list or dict is *dropped* rather than
stringified, because stringifying it is how content escapes. `link_template`
— the "open this session" link an alert carries — is not a setting here at
all any more (0.3.0): it is a per-service field on your runbound
dashboard, rendered by the plane when it builds a delivery, not by the SDK.

Telemetry is lossy on purpose: each lane is a bounded queue, the oldest record
is dropped when it is full, and `dropped` rides along on every batch as a
cumulative counter, so a plane that is down for an hour costs a fixed amount of
memory rather than an OOM. At interpreter exit the queue is drained once.
`export_events=False` turns the telemetry lanes off — no events and no
anomalies leave the process. **The fleet state still flows**: the exit deltas,
the circuit transitions and the trips travel on the same batches, because they
are how this worker's spend reaches the fleet's total, not telemetry about it.
A shared budget works the same with telemetry off.

### Reading the link

```python
runbound.plane_status()
# PlaneStatus(mode='connected', last_contact_age_s=1.2, consecutive_failures=0,
#             notice=None, entitlements={'plan': 'team', 'limits': {...}, 'denied': []})

runbound.fleet_status("user:42")
# {'fleet_spend_usd': 4.9, 'fleet_tokens': 900000, 'strikes': 1, 'generation': 3,
#  'halt': False, 'latched': True, 'policy_version': 4, 'age_s': 0.4,
#  'door_refusals': 12}

runbound.key_hash("user:42")
# 'ea3fd43be1e57d62e163dae19fc740bd6d660eec497235fd0ef859e2bd9fa328'
```

`plane_status()` reads `"local"` when no plane is configured (and before
`init()`), `"connected"` while it is answering, `"degraded"` when it is not,
and `"limited"` when the plane is answering but your plan is having entry
decisions made locally. `entitlements` is the plan the plane last stated —
`{"plan", "limits", "denied", "notice"}` — and `notice` is what it wants you to
read. **A plan limit never stops your guarding**: every detector, latch, cap
and policy keeps running exactly as it does with no plane at all, the
heartbeat keeps going, and the trips and exit deltas keep flowing so the
fleet's totals stay right. What a denial can take away is the plane's own
additions — telemetry (`events_denied`, `events_over_cap`) and the shared entry
answer (`workers_synced_exceeded`). The notice is logged at most once an hour.
`fleet_status(key)` is the other half of `session_status(key)`: that one is what
*this* worker knows about one key, this one what the whole fleet does — and
it answers `None` for a key this worker has not opened a block for in the last
few seconds. `door_refusals` is the one local number in it: how many blocks
*this* worker has turned away at the door while the key was latched — what the
latch is saving, which the plane also counts fleet-wide. Both read cached state
and never open a socket, so a health endpoint may poll them. `key_hash(key)` is the digest the plane knows a key by:
the join key between your own logs and anything the plane shows you.

The control plane is Runbound AI's hosted product, currently in early access
(see [pricing](https://runbound.co/pricing)); a self-hosted deployment
of the plane is available as an Enterprise option, licensed separately
rather than built from this repository. The SDK half above is the free,
MIT-licensed part of fleet mode, and it works against any server that
speaks those six endpoints — a real property, not a sales pitch: nothing in
the SDK cares whether the plane behind them is ours.

A human can run and watch the fleet too, wherever the plane is deployed: it
serves an admin dashboard at `/` — fleet overview, sessions, the
refused-actions ledger, policies, kill switch and more, on every plan. A
self-hosted deployment boots from `/setup` using a bootstrap key printed on
first run; the hosted product adds `/signup` once it is live. See
[the control plane docs](https://runbound.co/docs/control-plane) for
roles, CSRF, and the rest of the admin API the dashboard is built on.

---

## Spike detection (zero-config)

Every other detector needs a number from you. This one learns one, so it needs
**no configuration at all**: `runbound.init()` with no arguments already
watches every model call that reaches it through a guarded client.

It keeps a sliding window of that session's last `spike_window` (50) model
calls. Once there are `spike_warmup_calls` (4) of them, the **median** duration
and the median output work become "this session's normal" — a median, so one bad
call in the history cannot drag the baseline up with it. Output work is the
provider's **completion-token count, which already includes reasoning
(thinking) tokens**, so a model that quietly starts thinking is measured on
what it actually generated. The reasoning share is carried alongside in the
event so you can see the split; it is never added on top, which would make a
cap fire at half the number you set.

A call is abnormal when it exceeds `spike_factor` (10) times that median **and**
rises above it by an absolute floor — `spike_min_duration_s` (2s) for duration,
`spike_min_output_tokens` (500) for output work — so ordinary answer-length
variance on a fast bot (0.3s lookups, one 1.5s answer) never counts as a spike:

- **First abnormal call → a `warn` anomaly.** A quiet notice, logged and
  alerted, that **never stops the agent, whatever `on_anomaly` says**. Models
  are noisy; one slow call is not an incident.
- **`spike_confirm` (2) of the trailing 5 calls abnormal → a `critical`
  anomaly.** By default (`on_spike="notify"`) this is **still only logged and
  alerted** — a model legitimately thinking hard about a hard question is not
  an incident either, and a wrong stop on a real user is worse than the
  spend. Set `on_spike="trip"` if you want a confirmed spike to follow your
  `on_anomaly` (raise, callback) and stop that session, or
  [`on_spike="limit"`](#many-callers-behind-one-service-on_spikelimit) for the
  middle setting — sustained spiking earns a session limit before anything
  closes.
  Each phase is reported once per session, and a window that returns to normal
  simply goes quiet.

**The held baseline: a repeat offender's spikes never become their "normal".**
The baseline is the median of the session's own recent calls, and a spike lands
in that history like any other call — so a sustained run of them would drag the
median up until the abuse read as ordinary. It does not: while the trailing
window still contains an abnormal call, the baseline is **frozen** at the
snapshot taken on the last call before the abnormal run began. Live tracking
resumes once five ordinary calls in a row have pushed the abnormal ones out of
the window, so a session whose traffic genuinely changes still re-learns. This
holds in every `on_spike` mode — it is a correctness property, not a policy.

Spikes are the early-warning system; the hard walls are the deterministic
detectors — a session that keeps burning money still hits `budget_usd`, and
an explicit `max_call_seconds` / `max_tokens_out_per_call` cap always reacts
through `on_anomaly` (you set that number on purpose). Your observers — and,
once a plane is connected, the plane itself — hear about both phases, each
exactly once per session: the watching notice and the confirmation.

The anomaly names the session and the numbers behind it:

```
Spike confirmed for session 'user:5310': call took 74.0s, this session's normal is 2.0s
```

```python
anomaly.details
# {'session_id': '91dd75886877', 'key': 'user:5310',
#  'tags': {'service': 'support-bot'}, 'metric': 'duration',
#  'value': 74.0, 'median': 2.0, 'factor': 10.0, 'confirmed': True}
```

`metric` is `"duration"` or `"output_tokens"` — whichever moved further from
that session's normal.

**Optional hard caps.** If you do know a number, there are three per-call
ceilings: `max_call_seconds` (wall time), `max_tokens_out_per_call` (completion
tokens, reasoning included) and `max_cost_per_call_usd` (estimated dollars for that one
call). All three are off by default and they override the learned path
entirely: breaching one is `critical` from the first call, with no warm-up and
no confirmation, and it reacts through `on_anomaly` whatever `on_spike` says —
a number you stated is not a behavioral spike. The anomaly comes from the
`spike` detector with `details["cap"]` set and `details["metric"]` naming which
ceiling went:

```
Per-call cap exceeded for session 'user:5310': call cost $1.2000, cap $0.5000
```

```python
runbound.init(on_anomaly="raise")                        # learned, zero config
runbound.init(on_anomaly="raise", max_call_seconds=120)  # plus a hard ceiling
runbound.init(on_anomaly="raise", max_cost_per_call_usd=0.50)  # or a dollar one
runbound.init(spike_detection=False)                     # off
```

The dollar cap is priced from the same static table `budget_usd` uses, so
under the default `on_unpriced_model="zero"` it reads `$0.00` for a model
runbound has no price for — on Ollama or a local vLLM, cap the tokens
instead, or set `on_unpriced_model="estimate"` / `"refuse"` (see the
[configuration reference](#configuration-reference)).

Honest about the limits: **baselines live in this process and reset when it
restarts.** A fresh worker re-learns each session over its first 4 calls, and
two workers serving the same key do not share what they have learned.
Cross-instance baselines need a backend, which is on the roadmap and not in the
SDK.

### Many callers behind one service (`on_spike="limit"`)

When one service is shared — a hosted agent, an internal API, an assistant —
some keys behave and some do not, and a moderate spike should not slam the
door. A caller asking one genuinely hard question looks, for a moment, exactly
like a caller farming your service for free tokens, and cutting the first one
off is worse than paying for the second. `on_spike="limit"` is the middle
setting: **graduated pressure for behavior**, applied to one key's session, one
rung at a time. Money keeps its own hard wall underneath — `budget_usd` is
arithmetic and stops the session outright, ladder or no ladder.

| Level | What it means | What runbound does | What your app sees |
|---|---|---|---|
| **0** | Quiet | Watching, saying nothing | Nothing |
| **1** | Watching | First abnormal call: a `warn` anomaly, logged and alerted once | Nothing — the call is served |
| **2** | Limited | `spike_confirm` of the trailing 5 calls abnormal: the session gets an **allowance** of `spike_limit_calls` (5) further abnormal calls. Each one spends a unit, silently. Still a `warn` — nothing stops. If the window normalizes before the allowance runs out the session **heals** back to level 1, allowance forgotten (a later re-confirmation limits it again, and alerts again) | Nothing — every call is served |
| **3** | Closed | The allowance hits zero: a `critical` anomaly with `action="rollover"`. The session is closed, the key takes a **strike**, and its next session starts after a `spike_cooldown_seconds` (300s) cooldown with **half** the allowance (halved again per strike, floor 1) | `GuardrailTripped` on the closing call, then a `GuardrailTripped` at the door of each message during the cooldown — then served again |
| **4** | Blocked | After `spike_max_strikes` (3) rollovers the key is latched permanently, `action="blocked"`. No cooldown expires it | `GuardrailTripped` at the door, every time, until `runbound.clear(key)` |

**The rollover is invisible to your code.** There is no new exception, no new
call to make, and no state for you to hold: it is the same
`with runbound.session(key):` block, and the refusal during a cooldown is an
ordinary `GuardrailTripped` — the same one you already catch. The key quietly
moves to a fresh session between requests; `anomaly.details["action"]` is
`"rollover"` (or `"blocked"`) if you want to tell it from a budget trip.

```python
runbound.init(
    on_anomaly="raise",
    on_spike="limit",           # requires on_trip="latch" — the default
    spike_limit_calls=5,        # abnormal calls a limited session may still make
    spike_cooldown_seconds=300, # how long a closed session is refused
    spike_max_strikes=3,        # rollovers before the key is blocked outright
)
```

**Telling the caller how long.** `runbound.session_status(key)` reads the
ladder without touching it (never creates a session, never counts as using
one), which is how you render "back in 5 minutes" instead of a bare error:

```python
runbound.session_status("user:5310")
# limited, four of five abnormal calls left:
# {'level': 2, 'strikes': 0, 'allowance_left': 4,
#  'cooldown_remaining_s': 0.0, 'tripped_by': None, 'generation': 0}

# rolled over, serving a cooldown:
# {'level': 0, 'strikes': 1, 'allowance_left': None,
#  'cooldown_remaining_s': 287.4, 'tripped_by': 'spike', 'generation': 1}
```

`runbound.clear(key)` is total forgiveness: the strikes go too, so a cleared
key starts again at the full allowance rather than one rollover from a block.

**The ladder explains itself.** `session_status(key)` also answers "why was this
key limited?" without anyone reading our code:

```python
runbound.session_status("user:5310")["why"]
# {'trigger': {'metric': 'duration', 'value': 41.2, 'median': 2.0,
#              'factor': 10.0, 'at_s_ago': 12.4},   # or None
#  'limited_at_s_ago': 12.4,      # 0.0 if it has never been limited
#  'allowance_start': 5,          # the limit's starting allowance, or None
#  'healed_times': 0,             # how many times level 2 healed back to 1
#  'closed_at_s_ago': 0.0,        # 0.0 if it has never been closed
#  'baseline': {'duration_s': 2.1, 'output_tokens': 118.0}}   # or None

runbound.session_status("user:5310")["history"]
# [(0, 1, 40.2, 'first_abnormal'), (1, 2, 12.4, 'confirmed'), ...]
```

`trigger` is the abnormal call that last moved the level up — a call that only
spends allowance or heals does not replace it — and `baseline` is the held or
live median the detector is judging calls against. `history` is the last 10
level transitions, oldest first, as `(level_from, level_to, at_s_ago, reason)`
with `reason` one of `first_abnormal`, `confirmed`, `healed`,
`allowance_spent`, `rollover` or `blocked`. Both survive a rollover:
the fresh session's entry is appended to what the closed one had earned, so the
story reads as one continuous climb rather than restarting at every strike.
`clear()` stamps a `cleared` transition onto the session it is forgetting, but
the key is gone from the registry by then, so it is visible only to a caller
holding its own reference to that session's state — never through
`session_status()`.

Three honest boundaries:

- **The ladder is for keyed sessions.** It ends in rolling a *key* over to a
  fresh session, and the unkeyed default session has no key to roll over — so
  outside a `session()` block, `"limit"` behaves exactly like `"trip"`.
- **A latch is what serves the cooldown, so nothing latches under
  `on_anomaly="warn"`** — a warn-mode session climbs to level 3, logs the
  closure, and keeps going. `init()` already rejects `on_spike="limit"` with
  `on_trip="once"` for the same reason. Use `"raise"` or `"callback"` if you
  want the ladder to actually hold a door shut.
- **Strikes and cooldowns live in this process**, like every other counter
  here. Two workers count strikes separately; a restart forgets them.

The whole climb, end to end and offline:
[`examples/ladder_demo.py`](examples/ladder_demo.py).

---

## How we prevent uncontrolled execution — the detectors (implementation)

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
| `loop` | The agent repeating the same tool call with the same arguments — whether your code ran it or [the model just asked for it](#model-requested-tool-calls-loops-without-tool) | `loop_threshold` (default 3), `loop_window` (default 20) | The current `tool_call` or `tool_request` event's argument hash appears **at least** `loop_threshold` times in the last `loop_window` recorded actions. Requests are hashed into their own namespace, so three requests and three executions are two threes, not a six. Severity `critical`. |
| `budget` | A run spending more money or more tokens than allowed | `budget_usd`, `max_total_tokens` | `total_cost_usd > budget_usd`, or `total_tokens > max_total_tokens`. Strictly greater: exactly at the limit does not trip. Cost is reported first if one event blows through both. Severity `critical`. |
| `velocity` | Burning tokens too fast, regardless of the total | `tokens_per_minute_limit` | Tokens recorded in the trailing 60 seconds (measured from the current event's timestamp) exceed the limit. An entry exactly 60s old still counts. Severity `warn`. |
| `steps` | An agent that will not stop taking model turns | `max_steps` | `turns > max_steps`, where a turn is one `llm_call` — a model call that makes three tool calls is one step, not four. Severity `critical`. See [max_steps and max_events](#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `events` | A session generating too much recorded activity, of any kind | `max_events` | `event_count > max_events` — every recorded event counts: model calls, tool calls, tool requests, failures. This is what `max_steps` counted before 0.3.0. Severity `critical`. |
| `spike` | A session whose model calls stop looking like themselves — thinking mode, a model update, a caller driving long generations | none (on by default); tune with `spike_*`, cap with `max_call_seconds` / `max_tokens_out_per_call` / `max_cost_per_call_usd` | A call exceeds `spike_factor` × this session's median duration or output work, after `spike_warmup_calls` of history. First one severity `warn` (never stops the agent); `spike_confirm` of the trailing 5 makes it `critical`. A breached hard cap — seconds, output tokens, or dollars on one call — is `critical` immediately, from call #1. See [Spike detection](#spike-detection-zero-config). |
| `error_storm` | An agent retrying into a wall: a provider answering 429 while every layer above it retries | `error_storm_limit` (default **10**, `None` disables) | More than `error_storm_limit` failed calls — failed model calls *and* failed tools — in the trailing 60 seconds. Severity `critical`. The one detector that is on by default with a number. See [Retry storms](#retry-storms-and-the-provider-circuit-breaker). |
| `timeout` | A run that stopped being work an hour ago, with no single call looking wrong | `max_session_seconds`, `max_session_lifetime_seconds` | `event.ts - session.run_started_at > max_session_seconds` (`details["scope"] == "run"`), on any event kind, on the monotonic clock; or, if set, `event.ts - session.started_at > max_session_lifetime_seconds` (`details["scope"] == "lifetime"`). Each fires independently, once per session. Severity `critical`. See [Time and fan-out limits](#time-and-fan-out-limits). |

**`budget_usd` stops after the call that crossed it** — exact, and the
default. `budget_admission=True` (opt-in, off by default) also refuses a
call *before it goes out* whose **estimated** cost would cross `budget_usd`
— an estimate, stated as such, never the default: see [Admission: an
opt-in pre-call budget
check](#admission-an-opt-in-pre-call-budget-check-budget_admission).

### Which anomaly wins a tie

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

### `max_steps` and `max_events`: steps are turns, events are events

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

### Admission: an opt-in pre-call budget check (`budget_admission`)

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

## What happens when something trips — every choice, in one place

Nothing here is decided for you silently. These settings cover every behavior,
each with a documented default, and this table is the whole menu. One thing
holds for every "alerted" below, so it is said once here instead of forty
times: the anomaly reaches your own process first, by the route `on_anomaly`
names — `"raise"` gives your handler a `GuardrailTripped`, `"callback"` calls
your function, `"warn"` writes a WARNING line. Those are alternatives, not a
set: `raise` and `callback` hand the anomaly to your code *instead of* logging
it; only `warn` logs. None of the three ever posted anywhere — that is not
what "alerted" means here. Once you are connected to a control plane, hosted
or self-hosted (see [Fleet mode](#fleet-mode--one-truth-across-all-your-workers-control-plane)),
the same anomaly also reaches it as telemetry, and "alerted" is what happens
next: whether that becomes a Slack message, a page, or a webhook POST is an
[alert route](#alerting) you configure on your runbound dashboard, not a
keyword on this call.

| Setting | Options | Default | What each option does |
|---|---|---|---|
| `on_anomaly` | `"warn"` / `"raise"` / `"callback"` | `"warn"` | The reaction to a critical anomaly. **warn**: log a warning, the agent keeps running. **raise**: raise `GuardrailTripped` on the agent's thread (`exc.anomaly` is the full `Anomaly`; `try/finally` still runs). **callback**: call your `callback(anomaly)` — your own kill switch; if it raises, that is logged and swallowed. |
| `on_trip` | `"latch"` / `"once"` | `"latch"` | What a critical trip does to the session **afterwards**. **latch**: the session stays stopped — every later call is refused (raise / callback again, no re-alert), and under `"raise"` even entering `session(key)` raises, so a blocked key costs zero model calls until `clear()` or `latch_ttl_seconds`. **once**: stop that one call only; the next call is evaluated afresh (a caught exception lets the caller continue — pick this only if you handle blocking yourself). |
| `latch_ttl_seconds` | `None` / seconds | `None` | Only matters with `on_trip="latch"`. **None**: the latch is permanent until `clear()`. **A number**: the latch expires that many seconds after it was set — every detector is re-armed and the session's next event is judged fresh, on the same cumulative counters. This **re-admits, it does not reset**: a session still over budget re-trips immediately, with the same detector; only `clear()` zeroes the counters themselves. Opt-in — nothing expires unless you set it. |
| `on_spike` | `"notify"` / `"trip"` / `"limit"` | `"notify"` | What a *confirmed* spike does (a first spike is always notify-only). **notify**: log and alert, never stop — thinking mode alone is not an incident. **trip**: treat it as critical and follow `on_anomaly` / `on_trip`. **limit**: climb [the abuse ladder](#many-callers-behind-one-service-on_spikelimit) instead of slamming the door — a confirmed spike costs the session an allowance of `spike_limit_calls` abnormal calls, and only an exhausted allowance closes it, with a cooldown and a strike (requires `on_trip="latch"`, which enforces the cooldown). Explicit hard caps (`max_call_seconds`, `max_tokens_out_per_call`) always trip regardless — you set that number on purpose. |
| `on_loop` | `None` / `"break"` / `"throttle"` / `"escalate"` | `None` | The reaction to a loop only (details [below](#when-a-loop-is-detected)). **None**: follow `on_anomaly`. **break**: raise immediately, whatever `on_anomaly` says. **throttle**: sleep before each repeat, never raise — a blocking `time.sleep()` on a sync call, and under a running event loop the engine hands the delay to the async wrapper instead, which `await asyncio.sleep()`s it, so the loop is never blocked either way. **escalate**: warn first, raise at `loop_hard_threshold`. |
| `on_provider_failure` | `"notify"` / `"open"` | `"notify"` | What a provider that keeps failing does to your calls. **notify**: count the failures and alert once when `circuit_failure_threshold` of them land inside `circuit_window_seconds` — nothing is ever blocked. **open**: also refuse calls — the wrapped client raises `CircuitOpen` (a `GuardrailTripped`, with `.provider`) **before** touching the provider for `circuit_cooldown_seconds`, then lets exactly one probe through; a successful probe closes the circuit. Your app catches it and picks its own fallback — [we never route](#retry-storms-and-the-provider-circuit-breaker). The circuit is per provider and process-wide, so it stops nobody's session and latches nothing. |
| `max_active_sessions`, `max_session_depth`, `max_child_sessions` | `None` / a number | `None` | The fan-out limits. A `session()` block that would take the run past one of them raises `GuardrailTripped` (detector `fanout`) **at the door, before its body runs**. **This row ignores `on_anomaly`** — like the per-call caps, these are numbers you stated — and it **latches nothing**: what was wrong is the shape of the run, not this key, so the next block is judged on its own. See [Time and fan-out limits](#time-and-fan-out-limits). |
| `max_inflight_calls` | `None` / a number | `None` | How many calls to one endpoint may be in flight at once. The call that would take a provider label past it raises `GuardrailTripped` (detector `inflight`) **before the request goes out**. **This row ignores `on_anomaly`** — the number is one you stated — and **latches nothing**: the moment a slot frees up the next call goes through. Alerted once per endpoint. See [Self-hosted models](#self-hosted-models). |
| `budget_admission`, `admission_output_tokens` | `bool`, `int` | `False`, `1024` | **Opt-in.** When `True`, `Engine.admit` also refuses a call **before it goes out** whose *estimated* cost would push `budget_usd` past its limit — `GuardrailTripped` (detector `budget`, `details["rule"] == "admission"`). **This row ignores `on_anomaly`** and **never latches**: an estimate is not a wall, and a cheaper call may still fit. An unknown model skips the estimate (warned once per model) rather than refuse a cost nobody stated. Alerted once per session. `admission_output_tokens` is the assumed output size when a request states no `max_tokens` / `max_completion_tokens` / `max_output_tokens` cap. With `budget_admission` left off, nothing here runs. See [Admission](#admission-an-opt-in-pre-call-budget-check-budget_admission). |
| `max_steps`, `max_events` | `None` / a number | `None` | Unlike the two rows above, both are ordinary detectors (`steps`, `events`): `critical`, follow `on_anomaly` and `on_trip` like `budget` does. **They count different things, not the same thing at two thresholds**: `max_steps` counts model turns (`llm_call` events) only, `max_events` counts every recorded event. Set both if you want an independent wall on each. See [max_steps and max_events](#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `max_session_seconds`, `max_session_lifetime_seconds` | `None` / seconds | `None` | The two wall clocks. Also ordinary detectors (`timeout`), like the row above: `critical`, follows `on_anomaly` and `on_trip` like `budget` does. `max_session_seconds` measures the *run* — reset on every `session(key)` entry — and fires with `details["scope"] == "run"`; `max_session_lifetime_seconds` measures since the session's first-ever creation and fires with `details["scope"] == "lifetime"`. Each fires once per session, independently of the other. See [Time and fan-out limits](#time-and-fan-out-limits). |
| `on_halt` | `"raise"` / `"warn"` | `"raise"` | What an org-wide halt from [the control plane](#fleet-mode--one-truth-across-all-your-workers-control-plane) does to this worker. **raise**: every guarded `session()` block is refused **at the door** with `GuardrailTripped` (detector `halt`) — that is what a kill switch is for. **warn**: nothing is refused; the halt is logged at most once a minute, so you can prove the switch reaches your workers before you let it stop them. **This row ignores `on_anomaly`** and **latches nothing**: by default the halt lifts by itself 60 s after the last contact with the plane — see `stale_halt` below for the other choice. |
| `stale_halt` | `"release"` / `"hold"` | `"release"` | What an **enforced** halt does while the plane link itself goes degraded (not the same question as whether to enforce a halt at all — that is `on_halt`). **release**: the halt stops being enforced 60 s after the last successful contact with the plane, so a dead plane cannot keep a fleet stopped forever. **hold**: the halt stays enforced past that window, until a heartbeat explicitly says otherwise — pick this when a false "all clear" costs you more than a stuck kill switch. `plane_status().halt_stale_s` reports how long a currently-enforced halt has been stale. |
| `on_plane_loss` | `"guard_locally"` / `"refuse"` | `"guard_locally"` | What entering a `session()` block does when the plane could not answer the entry question at all (timeout, error, a degraded link with no fresh cached decision) — a different moment from an *answered* refusal, which is always honored regardless of this setting. **guard_locally**: fall back to local detection alone, today's behavior. **refuse**: refuse the entry itself (detector `plane`, `GuardrailTripped`) rather than guess — latches nothing, costs no strike, and the very next entry asks the plane again. An invalid token is a configuration error, not plane loss, and guards locally under **both** settings (logged at most once a minute) — this option is only about a plane that could not be reached or answer, not one that rejected your credentials. |
| Fleet budget (`budget_usd` with a control plane) | — | as `budget_usd` | Nothing extra to configure. In fleet mode the entry answer carries what the rest of the fleet has already spent under this key, and the `budget` detector counts local spend **plus** that offset — so the reaction is exactly the `budget` reaction (`on_anomaly`, then `on_trip`), and the worker trips on the same turn a single worker with those numbers would. The anomaly says where the money went: `details["fleet_spend_offset_usd"]`. Same for `max_total_tokens` and `details["fleet_tokens_offset"]`. |
| Remote latch | — | always on with a plane | A latch **another worker** set is adopted here as if this worker had set it, with whatever is left of the fleet's ttl as this session's expiry and `details["origin"] == "fleet"` on the anomaly. From there it behaves like any local latch: under `on_anomaly="raise"` even *entering* `session(key)` raises, and under `"warn"` / `"callback"` the block runs and the reaction is re-applied on its first event. `is_tripped(key)` reports the real reason — the other worker's — and `runbound.clear(key)` clears it everywhere, not just here. |
| Org policy `dry_run` | set by the plane, not by you | off | An org [action policy](#action-policy--rules-for-what-your-agent-may-do) the plane marks `dry_run` is **logged and alerted and never blocks**: the anomaly is `warn`-severity, reads "Policy dry-run: would block tool …", and carries `details["dry_run"] is True`. It is how a platform team rolls a rule out across a fleet. **Your local `tool_policy` rules are untouched by it** and keep blocking exactly as they did. |
| Fleet circuit | follows `on_provider_failure` | `"notify"` | The plane can open or close a provider circuit on **every** worker at once, so one outage is discovered once for the fleet. What an open circuit *does* here is unchanged and still yours: **notify** alerts and lets every call through, **open** raises `CircuitOpen` before the call. Like a local circuit, it stops no session and latches nothing. |
| `tool_policy.on_violation` | `"block"` / `"block_and_latch"` / `"dry_run"` | `"block"` | What a tool call that breaks [your action policy](#action-policy--rules-for-what-your-agent-may-do) does. **block**: refuse that one call — `PolicyViolation` (a `GuardrailTripped`) is raised on the agent's thread, the tool body never runs, and the session keeps going. **block_and_latch**: refuse it *and* stop the session, honoring `on_trip` (under `"once"`, only that call). **dry_run**: let the call run, and log and alert what would have been refused — how you roll a policy out. **This row ignores `on_anomaly`**: the rule is one you stated about your own agent, so it is enforced whether or not detectors are set to stop anything. |

Which detectors can latch a session: `budget`, `steps`, `events`, `error_storm`,
`timeout`, a `loop` under `"break"` / escalate-critical, a hard cap, a `spike`
only under `on_spike="trip"` or the ladder's `on_spike="limit"` (where the latch
is what serves the cooldown), and `policy` under
`on_violation="block_and_latch"`. `velocity` is warn-severity and never stops
anything, and a `fanout` refusal, an `inflight` refusal, a fleet `halt` and an
open `circuit` stop the block and the call in front of them without latching the
session at all. A **remote** latch is the one exception that arrives from
outside: it is a latch another worker made and this one adopts, ttl included.

**The plane can also refuse a session in its own words, with no local anomaly
behind it at all** — an org daily budget already spent, or an entry refused
under `on_plane_loss="refuse"`. Until Wave 24 the SDK only read the *facts* a
plane decision carried (spend offsets, strikes, a latch) and never its
`allow` field, so a plane that said "no" was silently overruled by the worker
and the call went out anyway. It no longer is: a refused decision is honored
at the door, before any provider call. The two cases carry different
`detector`s, both with `details["origin"] == "plane"`: an entry refused
because the plane could not be reached at all (`on_plane_loss="refuse"`) is
detector `plane`, and resolves against the `"plane"` key in a
[refusals profile](#what-the-caller-sees--your-words-your-status) (built-in
fallback: HTTP 503, "The assistant is temporarily unavailable."); an org daily
budget already spent is detector `budget` with `details["rule"] ==
"org_budget"` — it is a budget like any other, just decided by the plane —
and resolves against the `"budget"` key (built-in fallback: the `"default"`
profile, HTTP 429, unless you set one). Either way — like a remote latch — it
latches nothing extra and costs no local strike; if the decision also carries
a latch (a real fleet fact, `origin="fleet"`), that is adopted first and
behaves exactly like the remote-latch row above.

`on_anomaly="callback"` requires a `callback`, and setting a `callback` without
it is rejected at `init()` — a kill switch that would never be called is a
configuration bug, not a warning.

Only the most severe anomaly drives the reaction (`critical` over `warn`), but
**every** anomaly is alerted before any reaction runs, so an escalation is never
lost to the exception that stops the agent. Alerts go out once per session per
detector per severity, never on a latched session's re-refusals — and a policy
refusal keys on the rule and the tool too, so a second forbidden tool is news
while the same refusal on every retry is not.

### When a loop is detected

A loop is the one failure mode where stopping the run is not always what you
want — sometimes the agent is repeating itself but is still making progress, and
sometimes you would rather slow it down than kill it. `on_loop` sets the
reaction for the `loop` detector only; every other detector keeps following
`on_anomaly`.

| `on_loop` | What happens on a repeat |
|---|---|
| `None` (default) | Nothing changes: the loop follows `on_anomaly`, and fires once per session. |
| `"break"` | Raises `GuardrailTripped` as soon as the loop threshold is reached, even if `on_anomaly` is `"warn"`. Use it when a loop is always a bug. |
| `"throttle"` | Sleeps before the repeated tool runs, doubling from `throttle_base_seconds` on each further repeat, up to `throttle_max_seconds`. Never raises. On a sync call this is a blocking `time.sleep()` on the agent's thread. Under a running event loop the sleep is instead `await`ed by the async tool wrapper (and, for a model-requested loop, by the async client wrapper) before the body runs — the delay travels through a `contextvar` from wherever the engine decided it, so the event loop is never blocked either way — except a synchronous tool called directly on the event-loop thread, which has nothing to await with and is not throttled. Use it to stop a burn while the run finishes. |
| `"escalate"` | Logs a warning on each repeat, then raises `GuardrailTripped` once the count reaches `loop_hard_threshold` (default `2 * loop_threshold`). Use it when a few repeats are normal and many are not. |

```python
runbound.init(loop_threshold=3, on_loop="escalate",
                loop_hard_threshold=8,
                on_anomaly="warn")   # still applies to budget, velocity, steps
```

### Tools that are supposed to repeat

Some tools are *meant* to run identically, over and over — polling a job until
it finishes, checking a status, refreshing a token. That is not a loop; it is
the tool doing its job, and without an exemption it would trip the loop
detector on schedule.

```python
@runbound.tool(repeatable=True)
def poll_job_status(job_id: str) -> str:
    ...
```

`repeatable=True` (equivalently, listing the tool's name in
`loop_ignore_tools` at `init()`, for a tool you cannot decorate) marks the
call `loop_exempt` for both the executed (`tool_call`) and the
model-requested (`tool_request`) event, so it never feeds the loop window —
whoever runs it. It still counts everywhere else: `runbound.tool_calls()`
tallies it and any `max_calls` in your [action policy](#action-policy--rules-for-what-your-agent-may-do)
still enforces its cap. Use it to mark polling, not to tune a real loop away —
a tool that is actually stuck still needs `max_calls` or a wall clock to stop
it.

Under `"throttle"` and `"escalate"` the loop detector reports on every repeat
rather than once, because the reaction has to be applied every time. Your
observers — and, once connected, the plane — are still told only once per
session per detector, so a throttled loop does not turn into a notification
storm.

### Alerting

Slack, PagerDuty, Opsgenie and a signed webhook are not SDK code (0.3.0):
`runbound/alerts.py` sends none of them. What it keeps is the receiver-side
signature check (below) and the bookkeeping that lets outbound telemetry
threads drain at process exit — nothing here builds an alert body or opens a
socket to Slack, PagerDuty or anyone else any more. The division of labour:
*the SDK detects, stops, refuses and reports; the plane routes and delivers.*
`on_anomaly="callback"` is the free, forever answer for anyone who wants to
notify themselves without a token or a plane at all — it hands the anomaly to
your own function, in your own process.

Once a plane is connected — hosted (a bare `token`) or self-hosted
(`control_plane_url`, `token=""` if it needs no auth) — configure where
anomalies go as an alert route on your runbound dashboard: routing by
severity, detector or service, fleet-wide dedup, delivery history and a Test
button against the real adapter, none of which a truthiness check running
inside your own process could ever provide. Which adapters a route may use
is gated by plan and enforced with a 403, not a suggestion — see
[the control plane docs](https://runbound.co/docs/control-plane) for
the endpoints, the adapter list, and the webhook body your receiver gets.

The plane's webhook adapter matches what used to be sent from here in
everything that signs and verifies a delivery: same `version`, same
envelope, same `X-Runbound-Timestamp` / `X-Runbound-Signature` headers,
same signing string. Two fields in the body did change: `session.id` used to
be this process's own session id and is now the sha256 key hash — the plane
has no notion of a per-process id, since any worker can serve the same
session — and `session.key` (what `send_session_keys=True` used to add
straight to the body) does not exist here at all; a raw key now reaches you,
if you opt in, only through your own `link_template`. If your receiver keyed
on `session.id` as an opaque per-process value or read `session.key`, update
it; the signature check itself is unchanged — verify a delivery the same way
you always did:

```python
from runbound import verify_webhook_signature

@app.post("/hooks/runbound")
def hook():
    body = request.get_data()          # the raw bytes, unparsed
    if not verify_webhook_signature(SECRET,
                                    request.headers["X-Runbound-Timestamp"],
                                    body,
                                    request.headers["X-Runbound-Signature"]):
        return "bad signature", 400
    ...
```

It compares in constant time and rejects a timestamp more than five minutes from
your clock in either direction, so a captured delivery cannot be replayed later.
It returns `False` rather than raising for anything malformed — a timestamp that
is not a number, a missing header, a body that has been touched. Verify the
*raw* body: re-serializing the parsed JSON will not reproduce the bytes that
were signed.

### What the caller sees — your words, your status

runbound raises `GuardrailTripped` — it never writes the sentence your caller
reads. Without this, every app ends up inventing its own HTTP status and
copy for a refusal, one `except` block at a time, and a customer who wants to
change either has to ship code. `exc.refusal` fixes that: it carries the
status and the message *you* set, resolved fresh on every access.

```python
runbound.init(refusals={
    "default": {"status": 429, "message": "This assistant can't continue this conversation right now. Please try again later."},
    "budget":  {"status": 402, "message": "This conversation is over today's spending limit."},
    "policy":  {"status": 403, "message": "That action isn't allowed."},
})
```

A profile is a dict of `{key: {"status": int, "message": str}}`. Keys are
`"default"` or a detector name exactly as the engine emits it — grepping
`detector=` across the package turns up `budget`, `loop`, `spike`,
`velocity`, `steps`, `error_storm`, `timeout`, `fanout` and `inflight`, plus
`policy` (`PolicyViolation`), `circuit` (`CircuitOpen`), `halt` (an org-wide
halt) and `fleet` (a latch relayed from another worker whose own detector
could not be read). **Per-call hard caps** (`max_call_seconds`,
`max_tokens_out_per_call`, `max_cost_per_call_usd`) report through `spike` —
there is no `cap` key. Either field of an entry may be omitted; a missing
`status` or `message` falls through to `"default"`, then to `BUILTIN` below.
`message` may contain `{retry_after_s}` and `{detector}`; formatting is safe
by construction — an unknown placeholder is left verbatim and a malformed
template is returned unformatted, never raised.

Precedence, highest first, checked **field by field** — a plane profile that
only overrides `status` does not blank out a local `message`:

1. the plane's profile (org merged under service), this detector's entry
2. the plane's profile, its `"default"` entry
3. the local profile (`GuardrailConfig.refusals`), this detector's entry
4. the local profile, its `"default"` entry
5. `BUILTIN`

`BUILTIN` — the fallback with no configuration at all:

| Key | Status | Message |
|---|---|---|
| `default` | 429 | This assistant can't continue this conversation right now. Please try again later. |
| `policy` | 403 | That action isn't allowed. |
| `circuit` | 503 | The assistant is temporarily unavailable. |
| `halt` | 503 | The assistant is paused for maintenance. |

`exc.refusal.headers` is `{"Retry-After": "<seconds>"}`, rounded up, whenever
the anomaly's session is under a latch or cooldown with a known remaining
time, else `{}`. `exc.refusal.body()` is `{"refused": True, "detector": ...,
"message": ..., "retry_after_s": ...}` — everything a handler needs to answer
with directly:

```python
# FastAPI
except GuardrailTripped as exc:
    r = exc.refusal
    return JSONResponse(status_code=r.status, headers=r.headers,
                        content={**r.body(), "reply": r.message})
```

```python
# Flask
except GuardrailTripped as exc:
    r = exc.refusal
    resp = jsonify({**r.body(), "reply": r.message})
    resp.status_code = r.status
    resp.headers.update(r.headers)
    return resp
```

**Fleet mode:** set a profile once, on the plane, through the admin API
(`PUT /v1/admin/refusals` org-wide, `PUT /v1/admin/services/{service}/refusals`
per service — see [the control plane docs](https://runbound.co/docs/control-plane))
and the change reaches every worker within `control_plane_poll_s`, no
redeploy. The last profile a worker saw survives a plane outage, exactly
like a policy rollout. `coverage()["refusals"]` reports which tier is
currently answering — `"plane"`, `"local"`, or `"default"`.

---

## Action policy — rules for what your agent may do

Detectors watch how *much* an agent is doing. A policy states what it is
*allowed* to do — and once an agent can send email, issue refunds and move
money, that is where the liability sits. Monitoring can tell you afterwards
which refund went out; only something sitting on the tool call can refuse it.
`@runbound.tool` already fires before the function body runs, so a policy is
evaluated there: **we enforce the rules you state, we never judge the action,
and no model is involved in the decision.**

### The rule lives on the tool

No policy file, no CLI, no second place to look. A tool's rule is a keyword on
its decorator, so it sits in the same line — and lands in the same diff, and
gets the same review — as the function it governs:

```python
import runbound

runbound.init(on_anomaly="raise")             # governs detectors; policy is separate

@runbound.tool(max_calls=1, constraint=under_500)
def issue_refund(user: str, amount: float): ...

@runbound.tool(blocked=True)                  # the model may ask for it; it never runs
def send_email(to: str): ...

@runbound.tool(require_approval=ask_a_human)
def wire_money(account: str, amount: float): ...

@runbound.tool
def lookup_order(order_id: str): ...          # known, allowed, no rule
```

| Keyword | You give | A call is refused when |
|---|---|---|
| `blocked` | `True` | ever. The blunt one: this agent may never do this. Reported as the `deny` rule. |
| `max_calls` | `int >= 1` | the tool has been attempted more times than the limit **in this session**. The count includes the attempt being judged, so `1` means the first call runs and the second is refused. |
| `constraint` | `predicate(call) -> bool` | your predicate returns `False`. It receives a `ToolCall` — `name`, `args`, `kwargs`, `session_key`, `tags` — so it can decide on the real arguments, or on who is calling ("free-tier users may not do this"). |
| `require_approval` | `predicate(call) -> bool` | your callback returns `False`. Same `ToolCall`. Each tool gets **its own** callback: two tools can ask two different people. |
| `allow` | `True` | never. It states that this tool was reviewed and is deliberately unrestricted, so it satisfies `require_rules` below. It is **not** an allow-list and it restricts nothing. |

The rules take effect the moment the decorator runs, which is normally *after*
`init()` — your module configures runbound at the top and defines its tools
below. A keyword that could never be enforced (`max_calls=0`, a `constraint`
that is not callable) raises `ValueError` right there at decoration, the same
way a bad `init()` argument does.

### The CI gate: `require_rules=True`

```python
runbound.init(require_rules=True)
```

Now a `@runbound.tool` that states no rule at all is a `ValueError` — named at
`init()` for every tool already imported, and raised at decoration for every
one declared afterwards. Any CI step that imports your app fails with it, so a
tool cannot reach production without a stated rule. A tool that genuinely needs
none says so out loud with `reviewed=True`.

### `tool_policy=` on `init()`: the fleet allow-list, and tools with no decorator

Two things a decorator cannot say, so this stays:

- **An allow-list across all tools.** `ToolPolicy.allow` is the one rule that
  *inverts*: set it and the listed tools are the only ones permitted, which is
  a statement about the whole process rather than about any one tool.
- **A rule for a tool you did not write.** A framework's tools (LangChain's,
  say) carry no decorator of yours; name them here.

```python
from runbound import ToolPolicy

runbound.init(
    tool_policy=ToolPolicy(
        allow=["lookup_order", "issue_refund"],   # ONLY these tools may run
        deny=["wire_money"],
        max_calls={"framework_search": 5},
        require_approval=["delete_account"],
        approval_callback=lambda call: ask_a_human(call.name),
        on_violation="block",                     # "block_and_latch" | "dry_run"
    ),
)
```

A plain dict of the same fields is identical — useful when the policy comes out
of your own config file. It is coerced and validated at `init()`, and an unknown
key raises there rather than silently dropping a rule you believed was enforced:

```python
runbound.init(tool_policy={"deny": ["wire_money"], "max_calls": {"framework_search": 5}})
```

`on_violation` belongs here and only here: it is what a *broken rule* does, a
property of the policy rather than of any one tool.

**Where both name the same tool, the decorator wins** — the rule that ships in
the same diff as the function is the one that was reviewed with it — and a
warning says so once, naming the tool. A tool the decorator blocks is dropped
from an `allow` list here, exactly the way an org policy's deny resolves the
same disagreement.

The five rules are evaluated in a fixed order — `deny`, `allow`, `max_calls`,
`constraint`, `approval` — and the first one broken is the one reported, so an
explicitly blocked tool reads as "denied" however many other rules also cover
it.

**Catch a refusal apart from a runaway.** `PolicyViolation` is a subclass of
`GuardrailTripped`, so code that already catches trips keeps working; catch it
first when you want to tell "the agent tried something it may not do" from "the
agent ran away":

```python
try:
    run_agent()
except PolicyViolation as exc:
    # exc.violation.rule is "deny" | "allow" | "max_calls" | "constraint" | "approval"
    log.warning("refused %s (%s): %s", exc.violation.tool, exc.violation.rule, exc)
    ask_the_user_what_to_do()
except GuardrailTripped as exc:
    shut_down_cleanly(exc.anomaly)
```

`ToolPolicy`, `ToolCall`, `Violation` and `PolicyViolation` are all exported from
the package root. `runbound.tool_calls(key=None)` reads the tally `max_calls`
is measured against, without provoking a violation:

```python
runbound.tool_calls("user:8842")
# {'send_email': 2, 'issue_refund': 2, 'wire_money': 1}
```

Those are **attempts, not successes** — a call the policy refused is counted,
because it was made. The attempt is recorded before the policy judges it, so a
refused call still counts towards the loop window and the step count too, and an
agent that is both running away and misbehaving can be stopped by a detector
before the policy ever sees the call. That is the right order: the session is
already over.

**Roll it out with `dry_run`.** Nobody's first draft of a policy is right, and a
wrong rule in `block` mode breaks a working agent. Ship with
`on_violation="dry_run"` for a week: every violation is logged
(`[runbound] Policy dry-run: would block tool 'send_email' ...`) and alerted,
and every tool still runs. Read what it would have refused, fix the rules that
were wrong, then change one word to `"block"`.

Four honest notes:

- **An approval nobody can answer is a refusal.** Each tool's own
  `require_approval` callback is asked first; a tool that has none falls
  through to the `approval_callback` you configured on `init()`; if there is
  neither, the call is refused, not permitted. Fail-**closed**, like every
  other gate here.
- **A gate that errors refuses the call.** Everywhere else in runbound a bug
  is swallowed so your call proceeds. A constraint or approval callback is the
  one exception: it is a permission gate you opted into, and a gate that cannot
  answer must not wave the call through. A predicate that raises therefore
  counts as a violation — fail-**closed** — exactly as if it had returned
  `False`. Only the exception's *type* reaches the anomaly (`error: 'KeyError'`),
  never its message, which usually quotes the argument it choked on.
- **The real arguments go to your predicate and nowhere else.** `ToolCall.args`
  and `.kwargs` live for the duration of your call and are never stored, hashed,
  logged, or put in an anomaly or an alert. A violation names the tool, the rule
  and the numbers behind it — never a refund amount or an email address.
- **Approval callbacks are synchronous by contract.** runbound calls yours on
  the agent's own thread and waits, which is what makes the refusal arrive
  before the tool runs. In an async app a callback that blocks on a human blocks
  the event loop: return a decision you already have (a cached approval, a
  queue, a flag) rather than waiting inside it.

Two boundaries worth stating plainly:

- **`max_calls` is per session.** Inside `runbound.session(key)` each key gets
  its own tally; outside one, everything counts against the default session
  `init()` created. Both are per process.
- **Under LangChain, constraints see the input string.** The handler enforces
  the policy in `on_tool_start`, where LangChain hands us the tool's name and a
  single `input_str` — so `call.args == (input_str,)` and `call.kwargs == {}`.
  Deny, allow, `max_calls` and approval work exactly as they do elsewhere;
  a constraint that wants typed arguments needs the `@runbound.tool`
  decorator on the function itself.

The whole thing, offline, in three acts —
dry-run, block, and a denied tool that latches the session:
[`examples/policy_demo.py`](examples/policy_demo.py).

---

## Retry storms and the provider circuit breaker

The failures themselves are free. The retry loop around them is not: a provider
answering 429 while your SDK retries, your framework retries, and your own
`for attempt in range(5)` retries — every attempt costing latency and quota,
and every one that half-succeeds costing real dollars, with nothing that could
possibly work happening. runbound counts failed calls in two places, because
they are two different facts.

**Per session — the `error_storm` detector.** A wrapped client that raises
records an `llm_error` event, and a failing `@runbound.tool` already records
a `tool_error`. More than `error_storm_limit` (default **10**) of them in the
trailing 60 seconds is a `critical` anomaly that follows `on_anomaly` and
`on_trip` like any other detector — this is your *agent's* behavior, and the
session it belongs to is the thing to stop. Set `error_storm_limit=None` to
turn it off.

**Per provider — the circuit.** The circuit is process-wide and keyed by
provider, because a provider is down for everyone, not for one unlucky caller.
`circuit_failure_threshold` (5) failures inside `circuit_window_seconds` (60)
opens it for `circuit_cooldown_seconds` (30).

The key is the client's shape **and the endpoint it points at** —
`"openai@api.openai.com"`, `"openai@localhost:11434"`,
`"anthropic@api.anthropic.com"`, `"openai@default"` for a client that names no
`base_url`. Two OpenAI-compatible endpoints in one process are therefore two
circuits: a dead vLLM box opens its own and never refuses a call to OpenAI
proper. `circuit_state()` takes either form — a full label for that endpoint,
or a bare shape (`"openai"`) for the **worst** state among its endpoints, so a
health check written against `circuit_state("openai")` keeps meaning what it
meant.

Which failures count is a four-way classification, and only two of the four
open a circuit:

| Class | What it is | Counts? |
| --- | --- | --- |
| `provider` | 408, 425, 429, any 5xx, and every flavour of timeout | **yes** |
| `transport` | connection reset or refused, DNS, TLS — the request never arrived | **yes** |
| `application` | `TypeError`, `ValueError`, `KeyError`, pydantic `ValidationError`, and any other 4xx (400, 401, 404) | no |
| `cancel` | your code cancelled the call | no |

`provider` and `transport` both count because both say the *next* call cannot
succeed either, which is the only question a breaker asks. The other two never
do: a bad request or a bad key is a bug in your code, a `TypeError` in your own
callback is not the provider's fault, and opening a circuit over either would
stop your calls to a provider that is answering perfectly. The alert an open
circuit raises names the class in `details["fault"]`, because "the provider is
answering 503" and "we cannot reach the provider" are different pages in a
runbook.

The classifier reads `exc.status_code` when the exception carries one — the
provider answered, and its own verdict beats any guess of ours — and otherwise
the exception's class name, walking its base classes. Provider SDK types
(`openai.APIConnectionError`, `anthropic.APITimeoutError`) are matched **by
name**: runbound imports neither package, so the answer is the same whether or
not they are installed. An exception runbound cannot place is `application` —
it will not stop your calls over something it does not understand.

What an open circuit *does* is your explicit choice:

```python
import runbound

runbound.init(on_provider_failure="notify")   # the default: count and alert
runbound.init(on_provider_failure="open")     # also refuse calls while it is open
```

Under `"notify"` nothing is ever blocked. Failures are counted, and the moment
the threshold is crossed you get one alert per outage — not one per session
that ran into it — and every call still goes out.

Under `"open"` the wrapped client raises `CircuitOpen` **before** the request
leaves your process, so a refusal costs a microsecond instead of a timeout.
After the cooldown, exactly one probe is allowed through; if it works the
circuit closes, if it fails the cooldown starts again.

```python
from runbound import CircuitOpen

try:
    reply = client.chat.completions.create(model="gpt-4o", messages=msgs)
except CircuitOpen as outage:
    reply = other_provider(msgs)     # your fallback, your decision
    print(outage.provider)           # 'openai@api.openai.com'
```

`CircuitOpen` is a subclass of `GuardrailTripped`, so a host that already
catches that keeps working unchanged — the call simply fails fast instead of
joining the storm.

**We open the circuit and hand you the signal; we never route.** Picking a
fallback provider is an application decision with your keys, your prices and
your quality bar in it — the same boundary as never writing your bot's replies.

Ask at any time:

```python
runbound.circuit_state("openai")                  # worst of the openai@* endpoints
runbound.circuit_state("openai@localhost:11434")  # that one box
```

The state is counted under **both** modes, so you can run on `"notify"`,
watch `circuit_state()` on your health endpoint, and switch to `"open"` when
you trust the numbers. It reads `"closed"` before `init()` and whenever the
answer cannot be read — a health check must never be the thing that breaks.

Whatever the mode, when a provider call fails **the provider's own exception is
what your code catches.** The one deliberate exception: if recording that
failure is what makes a retry storm, the `GuardrailTripped` replaces it — you
have been retrying a wall and being told again what you already know is worth
less than being told to stop.

---

## Model-requested tool calls (loops without `@tool`)

`@runbound.tool` sees the calls your code runs. It does not see a model
asking for the same tool forever when you dispatch those calls by hand, through
a framework, or into a queue — and that loop costs exactly the same money.

So the wrappers read the tool calls a response **asks for**, on every path:

- **OpenAI chat** — `choices[].message.tool_calls[].function.{name, arguments}`
- **OpenAI Responses** — `output[]` items of type `function_call`
- **Anthropic** — `content[]` blocks of type `tool_use`
- **Streams** — assembled from the fragments as they arrive and reported once
  at stream end, best-effort; a shape runbound cannot read reports nothing.

Each one becomes a `tool_request` event, hashed from the tool name and its
**canonicalized** arguments — JSON re-encoded with sorted keys, so the same
call formatted two ways still hashes the same, and a streamed call hashes like
the non-streamed one it is a copy of. The arguments themselves are never
stored, logged or alerted; only the digest is.

Those hashes live in their own `"req:"` namespace, separate from executed
`@tool` calls: **three requests and three executions are two threes, not a
six.** The `loop` detector treats them exactly like executed calls otherwise,
so `loop_threshold` and `loop_window` mean the same thing for both.

**The trip comes out of `create()`, after the response returned.** That call
happened and was counted — we cannot un-send it. What the raise prevents is
your dispatch of the third identical call:

```python
try:
    response = client.chat.completions.create(model="gpt-4o", messages=msgs,
                                              tools=tools)
except GuardrailTripped as looping:
    print(looping.anomaly.message)
    # Loop detected: model requested tool 'search' repeated 3x in last 20 actions
    return give_up_gracefully()

for call in response.choices[0].message.tool_calls:   # never reached
    dispatch(call)
```

Requests are counted for loops only. They never feed an action policy's
`max_calls`, and `runbound.tool_calls()` still counts executions alone: what
the model *asked* for is not what your agent *did*.

---

## Time and fan-out limits

Some runs are wrong in a way no single call shows. An agent stuck waiting on
something that will never arrive; a run that spawns sub-agents that spawn
sub-agents. Every individual number looks reasonable, and the bill does not.
These are the numbers you state about the *shape* of a run — all `None`, all
off, until you set them.

```python
runbound.init(
    max_session_seconds=900,     # a run may last 15 minutes
    max_active_sessions=50,      # 50 session() blocks open at once, process-wide
    max_session_depth=2,         # a top-level block plus two levels under it
    max_child_sessions=10,       # one session may open ten distinct children
    on_anomaly="raise",
)
```

**`max_session_seconds` and `max_session_lifetime_seconds` are both the
`timeout` detector**, reading two different clocks. Any event kind trips
either — a session is running whether it is calling a model, running a tool,
or failing — measured on the monotonic clock, never the system one, so a
clock change cannot fake either. Each fires once per session, `critical`,
and follows `on_anomaly` and `on_trip` exactly like `budget` does.

| Clock | Measures | Keyed session (`session(key)`) | Unkeyed (default) session |
|---|---|---|---|
| `max_session_seconds` (`details["scope"] == "run"`) | The current *run* | Reset on **every entry** of `session(key)` — a returning key's next request starts a fresh clock, not a continuation of its first one ever. | Never reset: the default session guards the whole process, so it *is* the run, from `init()` onward. |
| `max_session_lifetime_seconds` (`details["scope"] == "lifetime"`) | The session's whole existence | Set once, at the key's first-ever entry, and never reset — the old (pre-0.3.0) meaning of `max_session_seconds`, for a customer who wants it back. `None` (off) by default. | Identical to `max_session_seconds` here, since the default session is never re-entered — this knob exists for keyed sessions. |

A keyed session entered three times over 70 minutes, each block short, never
trips `max_session_seconds=3600` — the run clock resets each time. One block
that itself runs 3601 seconds does. `max_session_lifetime_seconds` trips on the
*sum* of the key's whole history instead, whenever you set it — independently
of whether the run clock ever trips.

**The three fan-out limits are enforced at the door.** Entering
`runbound.session(key)` records where the block sits — its depth, its parent,
and one more child on that parent — and then checks the three numbers *before
the body runs*:

| Rule | Counts | Refused when |
|---|---|---|
| `active` | `session()` blocks open right now, process-wide | entering would make more than `max_active_sessions` |
| `depth` | how many blocks enclose this one — a top-level block is depth **0** | this block's depth exceeds `max_session_depth`, so `2` permits a top-level block and two levels under it |
| `children` | distinct children one session has opened | the parent's child count exceeds `max_child_sessions` |

A refusal is a `GuardrailTripped` whose anomaly carries
`details["rule"]` (`"active"` / `"depth"` / `"children"`), `count`, `limit` and
the session `key`. Two properties are deliberate, and both are in
[the reactions table](#what-happens-when-something-trips--every-choice-in-one-place):
it is enforced **regardless of `on_anomaly`** — like a per-call cap, this is a
number you stated, not something inferred — and it **latches nothing**, because
the thing that went wrong is the shape of the run, not this key. The next
block for the same key is judged on its own shape. Each rule pages once per
session, however often the agent walks back into it.

Lineage is recorded where a session is *born*: a key first entered under one
parent keeps that depth and that parent forever, so a shared helper session used
from everywhere cannot inflate anybody's child count, and re-entering the same
child counts one child however many times it is used.

```python
runbound.active_sessions()   # keyed session() blocks open right now
```

It counts blocks, not keys — the same key entered twice in two threads is two
pieces of work in flight — and the default session is never counted, so this
reads `0` in a program that uses no keys, and before `init()`.

Fail-open holds here too: anything that goes wrong deciding is logged and the
block is entered, exactly as it would have been with no limits configured.

---

## Configuration reference

Every field below is a keyword argument to `runbound.init()`. Bad values raise
`ValueError` at `init()` time, and unknown options raise `TypeError` — a
misconfigured guard fails loudly at startup instead of quietly guarding
nothing in production. Five names 0.3.0 retired — `slack_webhook`,
`pagerduty_routing_key`, `webhook_url`, `webhook_secret`, `link_template` —
are the one exception: they still raise, but with a `ValueError` naming where
that setting actually lives now (an [alert route](#alerting) or a per-service
field on your runbound dashboard), not a bare `TypeError: unexpected
keyword`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `budget_usd` | `float \| None` | `None` | Dollar cap for the session. Trips when total estimated cost exceeds it. |
| `max_total_tokens` | `int \| None` | `None` | Token cap for the session (input + output). Works for unpriced and local models. |
| `max_steps` | `int \| None` | `None` | Maximum model turns (`llm_call` events) in the session. An agent step is a model turn, not every recorded event — see [max_steps and max_events](#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `max_events` | `int \| None` | `None` | Maximum recorded events in the session — every kind counted once each. What `max_steps` counted before 0.3.0. |
| `tokens_per_minute_limit` | `int \| None` | `None` | Ceiling on tokens in any trailing 60-second window. |
| `loop_threshold` | `int` | `3` | Identical tool-call hashes within the window that count as a loop. Must be >= 2. |
| `loop_window` | `int` | `20` | How many recent tool calls are remembered. Must be >= `loop_threshold`. |
| `on_loop` | `str \| None` | `None` | What to do about a loop specifically: `None` (use `on_anomaly`), `"break"`, `"throttle"`, or `"escalate"`. |
| `loop_hard_threshold` | `int \| None` | `None` | Repeat count at which `on_loop="escalate"` stops the agent. Defaults to `2 * loop_threshold`. Must be greater than `loop_threshold`. |
| `throttle_base_seconds` | `float` | `2.0` | First sleep applied by `on_loop="throttle"`; it doubles on each further repeat. Must be positive. |
| `throttle_max_seconds` | `float` | `30.0` | Ceiling on the throttle sleep. Must be positive and >= `throttle_base_seconds`. |
| `spike_detection` | `bool` | `True` | The per-session behavior watch. Set `False` to turn it off. |
| `on_spike` | `str` | `"notify"` | What a *confirmed* spike does: `"notify"` logs and alerts only; `"trip"` follows `on_anomaly` and stops that session; `"limit"` climbs [the abuse ladder](#many-callers-behind-one-service-on_spikelimit) — allowance, then rollover, then a block (requires `on_trip="latch"`). Hard caps always react regardless. |
| `spike_limit_calls` | `int` | `5` | Ladder only. Abnormal calls a *limited* session may still make before it is closed and rolled over. Halved for each strike the key has already earned, floor 1. Must be >= 1. |
| `spike_cooldown_seconds` | `float` | `300.0` | Ladder only. How long a rolled-over key is refused at the door before its fresh session starts serving. Must be positive. |
| `spike_max_strikes` | `int` | `3` | Ladder only. Rollovers a key may earn before it is blocked permanently, until `clear()`. Must be >= 1. |
| `spike_warmup_calls` | `int` | `4` | Model calls of history a session needs before its baseline is trusted. Must be >= 2. |
| `spike_min_duration_s` | `float` | `2.0` | Absolute rise over the median a duration spike must also clear. Must be positive. |
| `spike_min_output_tokens` | `int` | `500` | Absolute rise over the median an output-work spike must also clear. Must be positive. |
| `spike_window` | `int` | `50` | Model calls kept per session for the baseline. Must be greater than `spike_warmup_calls`. |
| `spike_factor` | `float` | `10.0` | Multiple of the session's median duration or output work that counts as abnormal. Must be > 1. |
| `spike_confirm` | `int` | `2` | Abnormal calls out of the trailing 5 that turn a watch into a `critical` anomaly. Must be 1–5. |
| `max_call_seconds` | `float \| None` | `None` | Hard per-call duration ceiling. Breaching it is `critical` on the first call, no warm-up. |
| `max_tokens_out_per_call` | `int \| None` | `None` | Hard per-call ceiling on output work — the provider's completion-token count, which already includes reasoning tokens. Same immediate `critical`. |
| `max_cost_per_call_usd` | `float \| None` | `None` | Hard per-call ceiling on estimated dollars for one model call. Same immediate `critical`, from call #1; reported by `spike` with `details["metric"] == "cost_usd"`. `$0.00` for unpriced models — cap tokens there instead. |
| `max_session_seconds` | `float \| None` | `None` | Wall-clock ceiling on one *run*, on the monotonic clock. Reset on every `session(key)` entry (unchanged for the default session, which never re-enters). Any event kind trips it; `critical`, once per session, follows `on_anomaly`, `details["scope"] == "run"`. See [Time and fan-out limits](#time-and-fan-out-limits). |
| `max_session_lifetime_seconds` | `float \| None` | `None` | Wall-clock ceiling on a keyed session's whole existence, since its first-ever entry — never reset. The pre-0.3.0 meaning of `max_session_seconds`, for a customer who wants it. `critical`, once per session, follows `on_anomaly`, `details["scope"] == "lifetime"`. See [Time and fan-out limits](#time-and-fan-out-limits). |
| `max_active_sessions` | `int \| None` | `None` | How many `session()` blocks may be open at once, process-wide. Enforced at the door, whatever `on_anomaly` says; latches nothing. |
| `max_session_depth` | `int \| None` | `None` | How deep `session()` blocks may nest. A top-level block is depth `0`, so `2` permits it plus two levels under it. Same door, same rules. |
| `max_child_sessions` | `int \| None` | `None` | How many distinct child sessions one session may open. Same door, same rules. |
| `error_storm_limit` | `int \| None` | `10` | Failed calls (model **and** tool) in the trailing 60 seconds that a session may reach and stay quiet at. The one past it is `critical`. `None` disables it. See [Retry storms](#retry-storms-and-the-provider-circuit-breaker). |
| `on_provider_failure` | `str` | `"notify"` | What a failing provider's circuit does: `"notify"` counts and alerts once per outage and never blocks; `"open"` also raises `CircuitOpen` before each call until the provider recovers. |
| `circuit_failure_threshold` | `int` | `5` | Provider failures inside the window that open its circuit. Always on (the circuit is counted under both modes). Must be positive. |
| `circuit_window_seconds` | `float` | `60.0` | The trailing window those failures are counted in. Must be positive. |
| `circuit_cooldown_seconds` | `float` | `30.0` | How long an open circuit stays open before one probe is let through. Must be positive. |
| `on_trip` | `str` | `"latch"` | After a critical trip: `"latch"` keeps the session stopped until `clear()`/ttl; `"once"` stops that one call only. See [the reactions table](#what-happens-when-something-trips--every-choice-in-one-place). |
| `latch_ttl_seconds` | `float \| None` | `None` | **Opt-in.** `None` = a tripped session stays tripped until `clear()`. A number = the latch expires that many seconds after it was set: every detector is re-armed and the session's next event is judged fresh, on the same cumulative counters — a session still over budget re-trips immediately, with the same detector. Re-admits; does not reset. |
| `tool_policy` | `ToolPolicy \| dict \| None` | `None` | The rules for what your agent may do, enforced at every guarded tool call: `deny`, `allow`, `max_calls`, `constraints`, `require_approval` + `approval_callback`, and its own `on_violation`. A dict of those fields is coerced to a `ToolPolicy` and validated at `init()`. Per-tool rules belong on `@runbound.tool` instead; this is for the fleet-wide `allow` list, for tools that carry no decorator of yours, and for `on_violation`. Where both name a tool, the decorator wins and one warning says so. See [Action policy](#action-policy--rules-for-what-your-agent-may-do). |
| `require_rules` | `bool` | `False` | The CI gate. `True`: a `@runbound.tool` that states no rule at all raises `ValueError` — at `init()` for every such tool already imported, and at decoration for every one declared afterwards. `reviewed=True` on the decorator says a tool was reviewed and needs none. |
| `max_inflight_calls` | `int \| None` | `None` | **Opt-in.** How many guarded calls to one provider label may be in flight at once, process-wide. The call that would exceed it raises `GuardrailTripped` (detector `inflight`) before the request goes out, whatever `on_anomaly` says, and latches nothing. `None` counts nothing at all. See [Self-hosted models](#self-hosted-models). |
| `estimate_tokens` | `bool` | `False` | **Opt-in.** When a response carries no usage at all, estimate tokens as `ceil(chars / 4)` over the request text and the answer, streams included. Never used when the endpoint reported usage. Logged once per process. For self-hosted servers that omit `usage`. |
| `auto_wrap` | `bool` | `True` | Patches the OpenAI and Anthropic SDK classes at `init()`, so every client built afterwards is guarded without a `wrap()` call. The provider label is still read per call from that client's `base_url`, so per-endpoint circuits keep working. `False` leaves the classes untouched. Reversible with `runbound.unpatch()`. See [What the SDK actually sees](#what-the-sdk-actually-sees--and-what-it-never-sees). |
| `coverage_check_seconds` | `float \| None` | `60.0` | How long after `init()` runbound waits before warning, once, that a provider SDK is imported but no guarded call has been recorded. `None` turns the check off. |
| `max_sessions` | `int` | `10_000` | How many keyed sessions are kept; the least recently used is evicted. Must be >= 1. |
| `on_anomaly` | `str` | `"warn"` | `"warn"`, `"raise"`, or `"callback"`. |
| `callback` | `Callable[[Anomaly], None] \| None` | `None` | Your handler. Required by, and only valid with, `on_anomaly="callback"`. |
| `token` | `str \| None` | `None` | The credential from your runbound dashboard, sent as `Authorization: Bearer …`. This is the one way to connect, and it names one of two kinds of customer (`GuardrailConfig.plane_mode`): **alone, with no `control_plane_url` set anywhere** (not passed, and not in `RUNBOUND_PLANE_URL`), it is **hosted** — you are on our server, we resolve the endpoint at `HOSTED_PLANE_URL`, which is `None` until that plane exists, so a bare token today logs one WARNING and the process stays local. Set `control_plane_url` too, from either source, and that is **self-hosted** instead — your own cluster — and `token=""` is how you state that plane has no auth (see `control_plane_url` below). Read from the `RUNBOUND_TOKEN` environment variable when unset (blank or whitespace counts as unset, from either source). **`token=""` pins a process local whatever the environment says** — it never reads `RUNBOUND_TOKEN`, the documented way for a library, or a test harness, to say "never connect, no matter what the caller's shell exports." What a token does *not* do any more is deliver anything: Slack, PagerDuty and a signed webhook are the control plane's job (0.3.0), configured as an [alert route](#alerting) on your dashboard, not a keyword here. Kept out of `repr()`. |
| `api_key` | `str \| None` | `None` | **Deprecated**, the old name for `token`. Still works for one release: it folds into `token` at `validate()`, with a one-time `WARNING` whether or not `token` was also set, and is cleared to `None` afterwards so nothing downstream reads it. Prefer `token`. Kept out of `repr()`. |
| `control_plane_url` | `str \| None` | `None` | Present, this is the **self-hosted** mode: your own cluster, at this url, and it requires a `token` (`token=""` if that plane needs no auth — a url is never enough by itself) — **when you passed it**. A url that came from `RUNBOUND_PLANE_URL` with no token anywhere is a deployment fact rather than a typo, so it warns once and guards locally instead of failing your `init()`: a base image that exports the variable must not be able to take down a service that has no token yet. Absent with a `token` set, a bare token points here for you instead (**hosted** mode, above). Absent with no token at all, this is **off** — the single-process SDK, which opens no sockets of its own. Blank or whitespace, from the call or from `RUNBOUND_PLANE_URL`, counts as unset either way: there is no such thing as a plane at the empty url. `GuardrailConfig.plane_mode` (`"off"` \| `"hosted"` \| `"self_hosted"`) is the one property that answers which of the three a configuration is, and every internal reader (`shared.build` first) asks it instead of re-deriving its own truthiness test. |
| `service` | `str` | `"default"` | Which fleet this process belongs to. The plane keys org policy and dashboards on it. |
| `worker_id` | `str \| None` | `None` | This process's name inside the fleet. Defaults to `hostname:pid:xxxxxx` — six random hex characters appended so two replicas that share a hostname (containers, a restarted process reusing a pid) never collide on an id. |
| `control_plane_timeout_s` | `float` | `0.15` | The longest a plane call may block a session's entry. Must be `> 0` and `<= 2.0`: the plane is an optimization on top of local detection, never a dependency of it. |
| `control_plane_poll_s` | `float` | `5.0` | How often the heartbeat runs, on a daemon thread. The plane can slow a chatty fleet down without a redeploy. Must be positive. |
| `control_plane_cache_s` | `float` | `5.0` | How long one key's entry answer is reused before the plane is asked again. This is the fleet's convergence window: a key latched on one worker is refused on another within `control_plane_cache_s` **plus one turn**. Lower it for tighter propagation and more requests; raise it for fewer. Must be positive. |
| `export_events` | `bool` | `True` | Ship the event stream (counts and hashes, never content) to the plane in background batches. Telemetry only: with it off the session exit deltas, circuit transitions and trips still ship, so this worker keeps contributing to the fleet's shared spend. |
| `send_session_keys` | `bool` | `False` | **Opt-in.** Send the raw session key alongside its hash, to the plane. Off by default: a key travels as `sha256(key)` and nothing else, and every occurrence of it inside an anomaly's `message` and `details` is replaced by the first 12 characters of that hash and an ellipsis (`"3f2a9c1b04d7…"`). By default the plane's own alert adapters see only that hash too. Turn this on and the raw key can reach a delivery — not as a field the plane adds, but wherever you put it yourself: your own `link_template`'s `{key}`, or a detector's `message`/`details`, now unredacted, passed through to your own Slack, PagerDuty or webhook. |
| `on_halt` | `str` | `"raise"` | What an org-wide halt does here: `"raise"` refuses every guarded `session()` block at the door (detector `halt`); `"warn"` keeps serving and logs it once a minute. What happens to an *enforced* halt once the plane link itself goes stale is `stale_halt`, below. |
| `stale_halt` | `"release"` \| `"hold"` | `"release"` | **release**: a halt stops being enforced 60 s after the last contact with the plane. **hold**: it stays enforced until a heartbeat says otherwise. See [the reactions table](#what-happens-when-something-trips--every-choice-in-one-place). |
| `on_plane_loss` | `"guard_locally"` \| `"refuse"` | `"guard_locally"` | What a `session()` entry does when the plane could not answer at all (timeout, error, a degraded link with no fresh cached decision): **guard_locally** falls back to local detection; **refuse** refuses the entry itself (detector `plane`), latching nothing. An invalid token always guards locally, in both modes — it is a configuration error, not plane loss. |
| `custom_prices` | `dict[str, tuple[float, float] \| tuple[float, float, float] \| tuple[float, float, float, float]]` | `{}` | `model -> (usd per 1M input tokens, usd per 1M output tokens)`, or add a third `usd per 1M cached-input (read) tokens` and, further, a fourth `usd per 1M cache-write tokens` to state your own cache rates. Overrides the built-in table, and is consulted before it — the fix for a price `runbound.pricing.as_of()` says is stale. |
| `on_unpriced_model` | `"zero"` \| `"estimate"` \| `"refuse"` | `"zero"` | What a model with **no** price — not in the built-in table, not in `custom_prices` — costs a dollar budget. **zero**: counted as $0.00, same as always, with a once-per-model warning **on by default** so the blind spot is not a silent one. **estimate**: priced from `unpriced_price_per_1m_usd` instead; the event/anomaly carries `priced="estimated"`. **refuse**: the call is refused at the door (detector `budget`, `details={"reason": "unpriced_model", "model": ...}`), before it goes out, whatever `on_anomaly` says — a choice you stated on purpose, so it is not negotiable per anomaly. A model only discovered unpriced after the fact (`record_call()`, or a model name known only from the response) is priced like `"estimate"` when a fallback pair was given, else like `"zero"`, and logged once either way. |
| `unpriced_price_per_1m_usd` | `tuple[float, float] \| None` | `None` | The `(usd per 1M input, usd per 1M output)` fallback pair `on_unpriced_model="estimate"` prices from. Required when that mode is set; a 2-tuple of non-negative numbers otherwise it is rejected at `init()`. |
| `budget_admission` | `bool` | `False` | **Opt-in.** Refuse a call *before it goes out* whose estimated cost would cross `budget_usd` — see [Admission](#admission-an-opt-in-pre-call-budget-check-budget_admission). Off leaves every call path exactly as it was before this setting existed. |
| `admission_output_tokens` | `int` | `1024` | The assumed output size `budget_admission`'s estimate uses when a request states no `max_tokens` / `max_completion_tokens` / `max_output_tokens` cap. Must be a positive int. Ignored when a request names its own cap. |
| `loop_ignore_tools` | `tuple[str, ...]` | `()` | Tool names exempt from the loop window by policy rather than by decorator — equivalent to `@runbound.tool(repeatable=True)` for every call to that name, whoever runs it (executed or model-requested). They still count toward `tool_calls()` and any `max_calls` in your action policy. For marking a tool that is *meant* to repeat (polling a job, checking a status) — not for tuning around a real loop. |
| `refusals` | `dict \| None` | `None` | **Opt-in.** Your own HTTP status and sentence for a refusal, by detector (including `"plane"`) or `"default"`. Validated at `init()` — a bad status or an over-length message raises `ValueError` naming the key. A control-plane profile overrides this field by field; unset, `BUILTIN` answers. See [What the caller sees](#what-the-caller-sees--your-words-your-status). |

Every limit knob — `budget_usd`, `max_total_tokens`, `max_steps`, `max_events`,
`tokens_per_minute_limit`, `max_call_seconds`, `max_tokens_out_per_call`,
`max_cost_per_call_usd`, `max_session_seconds`, `max_session_lifetime_seconds`,
`max_active_sessions`, `max_session_depth`, `max_child_sessions`, `max_inflight_calls`,
`error_storm_limit`, `latch_ttl_seconds` — must be positive or `None`. The three `circuit_*` knobs
are always on and must be positive.

The rest of the public API:

| Call | Does |
|---|---|
| `runbound.init(**kwargs)` | Validates config, builds the engine, starts a session. Calling it again reconfigures and starts fresh. |
| `runbound.wrap(client)` | Guards a client's calls in place and returns the same object. Wrapping twice is a no-op; an unrecognized client raises `ValueError`. |
| `runbound.tool` / `runbound.tool(name="...", repeatable=True)` | Records every call to a function. Usable bare or with an explicit name. `repeatable=True` marks a tool that is *supposed* to run with the same arguments over and over — polling a job, checking a status — so its calls never feed the loop window (equivalent to listing its name in `loop_ignore_tools`). They still count toward `tool_calls()` and any `max_calls` in your action policy: mark polling, do not tune around a real loop. |
| `runbound.session(key, tags=None)` | Context manager: accounts everything inside it to `key`'s own session. Yields the `SessionState`, or `None` before `init()`. See [Runs keyed by any id](#runs-keyed-by-any-id). |
| `runbound.reset()` | Starts a fresh session with the same config: counters to zero, every keyed session forgotten, detectors re-armed. No-op before `init()`. |
| `runbound.current_session()` | The `SessionState` work is being accounted to — the enclosing `session()` block's, else the default one — or `None` before `init()`. |
| `runbound.is_tripped(key=None)` | The `Anomaly` that latched a session (`key=None` = the active one), else `None`. Never creates a session. |
| `runbound.session_status(key)` | Where a keyed session stands on [the abuse ladder](#many-callers-behind-one-service-on_spikelimit): a dict of `level` (0 quiet, 1 watching, 2 limited, 3 closed — a blocked key reads `level: 0`, since the session behind it is fresh; see `strikes`/`tripped_by` below), `strikes`, `allowance_left`, `cooldown_remaining_s`, `tripped_by` and `generation`. A blocked key is identified by `strikes == spike_max_strikes` with a permanent latch (`tripped_by == "spike"`, `cooldown_remaining_s == 0.0`), not by `level`. `None` before `init()` and for an unknown, cleared or evicted key. Never creates a session. |
| `runbound.tool_calls(key=None)` | How many times each tool has been *attempted* in a session (`{"send_email": 2}`) — what [`max_calls`](#action-policy--rules-for-what-your-agent-may-do) is measured against. A copy; `{}` before `init()` or for an unknown key. Never creates a session. |
| `runbound.circuit_state(provider="openai")` | Where one provider's circuit stands: `"closed"`, `"open"` or `"half_open"`. Takes a full endpoint label (`"openai@localhost:11434"`) or a bare shape (`"openai"`), which reads the **worst** state among that shape's endpoints. Counted under both `on_provider_failure` modes. Reads `"closed"` before `init()` and whenever it cannot be read. See [Retry storms](#retry-storms-and-the-provider-circuit-breaker). |
| `runbound.inflight_calls(provider="openai")` | How many guarded calls to that provider are in flight right now. Same label-or-shape resolution as `circuit_state()`, summed across matching endpoints. `0` before `init()` and when no `max_inflight_calls` is set (nothing is counted then). |
| `runbound.record_call(model, tokens_in, tokens_out, duration_s=0.0, *, provider="custom", error=None)` | Record one model call runbound did not make — in-process inference, your own HTTP client. Same path a wrapped client uses: budgets, spikes and limits all see it. With `error` it records a failed call instead (retry storm + that provider's circuit). See [Self-hosted models](#self-hosted-models). |
| `runbound.llm` / `runbound.llm(model=..., provider=..., tokens=...)` | Decorator for a function that performs inference itself: times it, applies the circuit and the in-flight cap before the body runs, records tokens via `tokens(result) -> (in, out)`, and records-and-re-raises whatever it throws. Sync and `async def`. |
| `runbound.active_sessions()` | How many keyed `session()` blocks are open right now, process-wide — blocks, not keys. The default session is never counted, so this is `0` before `init()` and in a program that uses no keys. |
| `runbound.coverage()` | What is actually instrumented right now: `auto_wrapped` labels, `wrapped_clients`, `decorated_tools`, `guarded_calls`, `tool_calls_seen`, `keyed_sessions_seen`, `providers_imported`, `providers_unguarded` (imported but never seen guarded), `last_guarded_call_age_s`, and any `warnings`. Read it when you want to know that the guard is on rather than assume it. |
| `runbound.assert_guarded()` | Raises `RuntimeError` when a provider SDK is imported and no guarded call has been recorded. For a startup check or a CI smoke test, so a forgotten `wrap()` fails loudly. |
| `runbound.unpatch()` | Undoes the class-level patching `auto_wrap` installed. `reset()` does not unpatch; this does. |
| `runbound.clear(key)` | Explicit forgiveness: un-blocks the key — next entry gets a fresh session, fresh baselines, re-armed detectors. No-op for unknown keys. In [fleet mode](#fleet-mode--one-truth-across-all-your-workers-control-plane) the plane is told too, so the key is let back in on every worker. |
| `runbound.plane_status()` | Where this worker's link to the control plane stands: `PlaneStatus(mode, last_contact_age_s, consecutive_failures, notice, entitlements, halt_stale_s)`. `mode` is `"local"` with no plane configured (and before `init()`), `"connected"` while it answers, `"limited"` when it answers but the org's plan has entry decisions made locally, and `"degraded"` when it does not answer at all — in which case every answer is being made locally. `halt_stale_s` is seconds since the last successful contact while a halt is currently enforced, else `None` — how close a `stale_halt="release"` halt is to lifting itself, or how long a `"hold"` halt has been running on a dead link. Reads cached state; never opens a socket. |
| `runbound.fleet_status(key)` | What the plane last said about one key: `{"fleet_spend_usd", "fleet_tokens", "strikes", "generation", "halt", "latched", "policy_version", "age_s"}`. The fleet-wide counterpart of `session_status(key)`. Reads the entry-decision cache only, so it never opens a socket and answers `None` without a plane, or for a key this worker has not opened a block for in the last few seconds. |
| `runbound.key_hash(key)` | The sha256 hex digest a session key travels as — the only form a key reaches the plane in, unless `send_session_keys=True`. Use it to join your own logs to anything the plane shows you. |
| `runbound.verify_webhook_signature(secret, timestamp, body_bytes, signature)` | Receiver-side check for a delivery from the control plane's webhook adapter (the same signing string this module used to send, kept for exactly this): constant-time compare of `"sha256=" + HMAC_SHA256(secret, f"{timestamp}.{body}")`, and a ±5-minute replay window. Returns `False` rather than raising for anything malformed. Verify the **raw** body bytes. See [Alerting](#alerting). |

Before `init()` is called, decorated tools and wrapped clients run exactly as if
runbound were not installed.

---

## Works with

`wrap()` recognizes clients by **shape, not type** — runbound never imports
`openai` or `anthropic`:

- `.chat.completions.create` and/or `.responses.create` → the OpenAI path
  (every surface present on the client is guarded, and `wrap()` logs which)
- `.messages.create` → the Anthropic path

OpenAI and Anthropic are supported and tested against the real SDKs.
OpenAI-compatible servers are supported through that same shape-matched
`wrap()` and proven live on Ollama; other compatible providers — vLLM, Groq,
OpenRouter, Azure OpenAI, LM Studio — share that code path but are not
individually tested yet. Native adapters for Gemini, Bedrock, Mistral and
Cohere are not built; use `record_call()` / `@runbound.llm` there. See the
[compatibility matrix](#compatibility-matrix) below for exactly what is
proven versus expected-by-shape. Sync and async clients, streamed or not, all
go through that one call — see [Async and streaming](#async-and-streaming).
For framework-driven agents there is a [LangChain / LangGraph
handler](#langchain--langgraph).

### Compatibility matrix

Filled in only where a test proves it — a checkmark with no footnote is a
claim we are not making. An empty cell means "probably works by shape, not
proven here," not "does not work."

| Provider | sync | async | stream | tool calls | usage | live-tested |
|---|---|---|---|---|---|---|
| OpenAI | yes [^sdk] | yes [^sdk] | yes [^stream] | yes [^sdk] | yes [^sdk] | |
| Anthropic | yes [^sdk] | yes [^async] | yes [^stream] | yes [^sdk] | yes [^sdk] | |
| Azure OpenAI | | | | | | |
| Ollama / vLLM / OpenAI-compatible | yes [^ollama] | | yes [^ollama] | yes [^ollama] | yes [^ollama] | yes [^ollama] |
| Gemini | | | | | | |
| Bedrock | | | | | | |
| Mistral / Cohere | | | | | | |
| LangChain | yes [^lc] | | | yes [^lc] | yes [^lc] | |

[^sdk]: Sync/tool-calls/usage cells for OpenAI and Anthropic, and the async
    cell for OpenAI, are proven against the real `openai` and `anthropic` SDK
    classes, over a fake HTTP transport (no live network) —
    `tests/test_real_sdk.py`.
[^async]: Anthropic async is proven only against a fake async client
    (`tests/test_async_wrappers.py`), not the real `anthropic` SDK —
    `tests/test_real_sdk.py` exercises real-SDK async for OpenAI only.
[^stream]: Sync for both providers, async for OpenAI — including
    abandoned-stream accounting: `tests/test_streaming.py`,
    `tests/test_streams_abandoned.py`. Async Anthropic streaming has no
    test; the wrapper handles it by shape.
[^ollama]: Live, over the network, against a real running Ollama server —
    sync only, includes a streaming scenario (`max_inflight_calls` while a
    stream is in flight), a decorated and an undecorated tool loop, and real
    token usage — `examples/live/ollama_verify.py` (13 scenarios; not part of
    `pytest` — run manually against a local Ollama server). Ollama is reached
    through the OpenAI-compatible surface, not a distinct SDK, so this row
    stands in for vLLM / Groq / Together / OpenRouter / LM Studio too, by
    shape, but only Ollama has actually been run live.
[^lc]: The LangChain callback handler, with `langchain_core` faked out (no
    real network call) — `tests/test_langchain.py`.

Azure OpenAI, Gemini, Bedrock, and Mistral/Cohere have no test naming them
anywhere in this repo; Azure is expected to work through the OpenAI shape
(same client class, different `base_url`) but that expectation is untested
here. Gemini, Bedrock, Mistral and Cohere use their own SDK shapes and are not
wrapped at all today — see [Paths that are not guarded
today](#paths-that-are-not-guarded-today).

**Priced model families** (USD per 1M tokens, list prices that drift — override
with `custom_prices` when you need exact numbers):

- OpenAI: `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1`, `gpt-4.1-mini`,
  `gpt-4.1-nano`, `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `gpt-3.5-turbo`,
  `o3`, `o3-mini`, `o4-mini`
- Anthropic: `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5`,
  `claude-opus-4-1`, `claude-opus-4`, `claude-sonnet-4`, `claude-3-7-sonnet`,
  `claude-3-5-sonnet`, `claude-3-5-haiku`, `claude-3-opus`, `claude-3-haiku`

Dated releases resolve by longest prefix, so `gpt-4o-mini-2024-07-18` prices as
`gpt-4o-mini`, not `gpt-4o`.

**These prices were last checked on `runbound.pricing.as_of()`** —
`"2026-09-12"` today. A price that changed after that date is wrong until
this table is updated; `custom_prices` always wins over it, which is how you
fix a stale number without waiting for a release.

#### Cached input tokens

A repeated system prompt, a long few-shot block, an agent's growing message
history — anything the provider recognizes as a repeat of something it just
saw — is billed as a **cache hit**, at a discount runbound now reads and
prices correctly: OpenAI at 50% of its input rate, Anthropic at 10%. Before
this, runbound had no reader for either field and priced every cached token
at the full input rate — an agent with a large cached system prompt was
over-charged on nearly every call, which trips `budget_usd` **before the
money is actually gone**, the one direction a dollar wall must never err in.

Nothing to configure: every guarded OpenAI and Anthropic call already reads
its own cache fields (`prompt_tokens_details.cached_tokens` /
`input_tokens_details.cached_tokens` for OpenAI, `cache_read_input_tokens`
for Anthropic) and prices the discount automatically, streamed or not. A
model with no published cached rate (`gpt-4-turbo`, `gpt-3.5-turbo` — both
predate prompt caching) prices every token at the full input rate instead of
guessing a discount that was never published.

Anthropic also bills a **cache write** (`cache_creation_input_tokens`) at a
125% premium — writing a new cache entry costs more than an ordinary input
token, not less. runbound counts a cache write inside `tokens_in` (so
`total_tokens` stays honest) *and* prices it at its own published rate
(50%/10% for a read, 125% for a write are the two directions a cache-pricing
mistake can run, and only one of them is safe: under-counting a read trips
`budget_usd` early, before the money is gone — annoying, but safe;
under-counting a write does the opposite, letting a customer spend past a
budget they set, which this product cannot afford). A model with no
published cache-write rate falls back to the plain input rate, same as an
unpublished read rate — never a guessed number, in either direction.

`budget_admission`'s pre-call estimate (below) has no way to know before a
call how many of its tokens will be cache hits, so it estimates every call
at the plain input rate — conservative, the same direction an unpriced
model's estimate already leans.

**Local and free models: use token limits, not dollars** (or price your own
hardware — see [Self-hosted models](#self-hosted-models)). By default an
unknown model prices at `$0.00` (`on_unpriced_model="zero"`, with a
once-per-model warning), so `budget_usd` will never trip on Ollama or a
self-hosted vLLM on its own. Guard those runs with `max_total_tokens` and
`tokens_per_minute_limit`, which are model-agnostic:

```python
runbound.init(max_total_tokens=500_000, tokens_per_minute_limit=60_000,
                max_steps=100, on_anomaly="raise")
```

For a hosted model runbound does not have a price for, supply one:

```python
runbound.init(budget_usd=5.0, on_anomaly="raise",
                custom_prices={"my-gateway/llama-3.3-70b": (0.60, 0.60)})
```

**Or change what "no price" means.** `on_unpriced_model` decides what happens
to a dollar budget when neither the static table nor `custom_prices` has an
answer: `"zero"` (the default, above) counts it as free and warns once;
`"estimate"` prices it from a `unpriced_price_per_1m_usd` fallback pair
instead, marking the number `priced="estimated"`; `"refuse"` stops the call at
the door — before it goes out — rather than let an unpriced model spend
against a budget silently, whatever `on_anomaly` says:

```python
runbound.init(budget_usd=5.0, on_anomaly="raise",
                on_unpriced_model="refuse")   # an unknown model never runs at all
```

---

## Self-hosted models

If you run the model yourself — vLLM, TGI, Ollama, llama.cpp, LM Studio,
LiteLLM, a fine-tune on your own GPUs — the runaway is the same and the
currency is not. **Nobody sends you an invoice, so nothing stops.** A looping
agent on a rented H100 costs you the hour either way; what it actually burns is
*capacity* — the queue every other request is waiting in.

**It already works.** Every OpenAI-compatible server is guarded by shape —
by `auto_wrap` or by an ordinary `wrap()` — with no adapter and no configuration:

```python
import openai, runbound

runbound.init(max_total_tokens=500_000, max_inflight_calls=8, on_anomaly="raise")
client = runbound.wrap(openai.OpenAI(base_url="http://gpu-box:8000/v1", api_key="x"))
```

That exact setup is what `examples/live/ollama_verify.py` runs against a real
local model on every release — thirteen scenarios, real tokens, real durations,
`$0.00`.

**The knobs that protect capacity.** Dollars are the wrong meter here; these
are the right ones:

| Knob | What it protects |
|---|---|
| `max_inflight_calls` | The GPU itself. Calls to one endpoint that may be in flight at once; the next one is refused **before it goes out** (detector `inflight`), rather than joining a queue that makes every user slower. Set it to the concurrency your server is actually sized for. |
| `tokens_per_minute_limit` | Sustained throughput — one session hogging the batch. |
| `max_tokens_out_per_call` | The output explosion: a model that will not stop generating. |
| `max_call_seconds`, `max_session_seconds` | Latency and the run that never ends. |
| `max_active_sessions`, `max_session_depth`, `max_child_sessions` | Fan-out — the sub-agent cascade that fills the queue with work nobody asked for. |
| `max_total_tokens`, `max_steps`, `max_events` | The session-wide walls that need no prices at all. |

**Want dollars anyway? Price your own hardware.** `custom_prices` is
`model -> (usd per 1M input tokens, usd per 1M output tokens)`, and nothing
says those have to be a vendor's numbers — GPU-hour cost divided by the tokens
an hour produces is a perfectly good internal price, and `budget_usd`,
`max_cost_per_call_usd` and the cost spike detector all start working the
moment you supply one:

```python
runbound.init(budget_usd=25.0, on_anomaly="raise",
                custom_prices={"qwen2.5:7b": (0.05, 0.05)})   # your cents, your GPU
```

**Servers that report no usage.** Some self-hosted stacks answer without a
`usage` object at all, and a guard that counts zero tokens reports green while
seeing nothing. `estimate_tokens=True` (opt-in, off by default) fills that gap
with `ceil(chars / 4)` over the request text and the answer — chat messages,
Responses input, Anthropic content blocks, and streams by accumulating text
deltas. It is used **only** when the response carried no usage; a server that
reports real numbers is always believed, and the estimate is announced once per
process so nobody mistakes it for measurement.

**Inference that never goes over HTTP.** In-process generation — `llama-cpp-python`,
`transformers`, a `torch` model in the same worker — has no client to wrap. Two
generic hooks put it on the same path everything else uses:

```python
@runbound.llm(model="llama-3.1-8b", provider="llama.cpp@local",
                tokens=lambda out: (out["usage"]["prompt_tokens"],
                                    out["usage"]["completion_tokens"]))
def generate(prompt: str) -> dict:
    return llama(prompt, max_tokens=256)

# or, when you would rather report by hand:
runbound.record_call("llama-3.1-8b", tokens_in=812, tokens_out=210,
                       duration_s=1.9, provider="llama.cpp@local")
runbound.record_call(None, 0, 0, provider="llama.cpp@local", error=exc)  # it failed
```

The decorator times the call, applies the circuit and the in-flight cap
**before** your body runs, records the tokens your callback reads, and on an
exception records the failure and re-raises it untouched. Sync and `async def`.
Budgets, loops, spikes, storms and circuits then treat local inference exactly
as they treat a hosted call.

**One circuit per endpoint.** The provider label is
`"{shape}@{host}"` — `"openai@gpu-box:8000"` — so your own box failing opens
its own circuit and refuses nothing else. This is the bug the label format
fixes: keyed by shape alone, a dead vLLM server would have failed fast on
OpenAI proper. `circuit_state("openai")` still answers for the whole shape (the
worst of its endpoints); `circuit_state("openai@gpu-box:8000")` answers for
that box.

**Honest limits.** The in-flight cap is **per process**, not per cluster —
eight replicas with `max_inflight_calls=8` allow 64 concurrent calls, and a
shared cap is what the control plane is for. A stream holds its slot until it
ends, is closed, **or is garbage collected** — an abandoned stream frees its
slot and is counted as one partial call the moment Python collects it (see
[Async and streaming](#async-and-streaming)); it does not hold the slot until
the process exits. The estimator is an
approximation, not a tokenizer: treat estimated dollars as an order of
magnitude, not a bill. And native, non-OpenAI-shaped SDKs — TGI's own client,
Bedrock, Vertex — are not wrapped yet; use `record_call` / `@runbound.llm`
around them today.

---

## LangChain / LangGraph

When the framework makes the calls, there is no client to `wrap()` and no
function to decorate. Pass a callback handler instead:

```bash
pip install "runbound[langchain]"   # pulls in langchain-core
```

```python
import runbound
from runbound.integrations.langchain import GuardrailCallbackHandler

runbound.init(budget_usd=5.00, loop_threshold=3, on_anomaly="raise")

agent.invoke(
    {"input": "research the market"},
    config={"callbacks": [GuardrailCallbackHandler()]},
)
```

The handler records a tool call on `on_tool_start` — **before** the tool runs,
so a loop is broken on the repeat that would have made it — and a model call on
`on_llm_end`, priced from whatever usage the result carries. It holds no state
of its own: several handlers across several chains share one session, one budget
and one step count.

It sets `raise_error = True` so LangChain lets `GuardrailTripped` out of the
callback and the chain actually stops. Every other exception inside the handler
is caught and logged, so that setting cannot turn a runbound bug into a
broken chain.

`langchain_core` is imported lazily, on first access to
`GuardrailCallbackHandler`, so `import runbound` still works without it
installed.

Two caveats. LangChain reports usage on `on_llm_end` only, so a streamed
response whose provider omits usage is recorded as a zero-token step. And the
handler records no call duration, so
[spike detection](#spike-detection-zero-config) on a LangChain-driven agent
works from output tokens alone — the duration baseline never arms, and
`max_call_seconds` has nothing to measure. Use `wrap()` on the client where you
want the timing signal.

---

## Async and streaming

**Async clients use the same `wrap()`.** `AsyncOpenAI` and `AsyncAnthropic` are
recognized by the same shapes; an `async def create` gets an `async def`
replacement, so your `await` sites are unchanged.

```python
client = runbound.wrap(AsyncOpenAI())
```

**Streams are guarded.** A `create(stream=True)` response comes back wrapped in
a proxy that yields the provider's chunks unchanged and in order. Exactly **one
model call is recorded when the stream ends** — by exhaustion, by `close()` /
`aclose()`, or by leaving a `with` / `async with` block. Everything the proxy
does not define is delegated to the provider's own stream object.

Usage on a stream is best-effort:

- **OpenAI** sends usage on a final chunk only when the request asked for
  `stream_options={"include_usage": True}`. runbound never adds that option to
  a request it did not write, so without it a stream records as a zero-token
  step — one step, no dollars.
- **Anthropic** reports usage across `message_start` and `message_delta`, so
  streams there are priced without any extra request options.

Exhausting the stream, calling `close()`/`aclose()`, or leaving the `with`
block reports it immediately — do that in your code. Garbage collection is
the safety net for the stream you forgot, not the mechanism.

**An abandoned stream is recorded as one partial call, when it is garbage
collected — never at interpreter exit.** A stream that is never exhausted and
never closed still ends, eventually, when nothing references it any more;
runbound registers a `weakref.finalize` callback that holds no strong
reference to the stream proxy itself, so it runs exactly when the proxy is
collected, in the session that opened the stream, and reports:

- **duration** — the time actually spent streaming (last chunk seen minus
  first), not the time since the call started;
- **tokens** — usage from the provider if any chunk carried it, else
  `ceil(chars / 4)` of the text that was actually streamed before it was
  abandoned, marked `estimated=True` so it is never mistaken for a measured
  number; zero if neither is available, and a truthful zero is trusted rather
  than skipped;
- **the circuit and in-flight cap** — the slot is freed, but the call counts
  as neither a success nor a failure, so an abandoned stream can neither close
  a circuit nor open one.

A stream that *is* exhausted, closed, or exited normally still reports exactly
once, as before — abandonment accounting is the fallback for the one case that
used to go completely uncounted, not a second report on top of the first.

---

## Privacy

Telemetry here is **content-minimizing, not content-free** — say plainly what
it still reveals rather than call it "safe" and leave you to find out. What it
reveals: tool names, model names, call timing, counts (tokens, steps, calls),
provider error classes, and salted argument-equality hashes (below). What it
never reveals: prompts, replies, tool arguments themselves, or error text.

- **Tool arguments are sha256-hashed before storage, salted per process.** The
  loop detector compares digests of `(tool_name, args, sorted kwargs)` mixed
  with a random salt generated once at import, never the arguments themselves.
  Keyword order does not change the digest, but the salt does: the same call
  hashes differently in a different process, so `args_hash` is an equality
  token for spotting a repeat inside this process, not a stable fingerprint
  someone could use to correlate calls across your fleet from the hash alone.
- **Raw arguments never leave your process.** They are not stored, not logged,
  and not included in any alert payload. An [action
  policy](#action-policy--rules-for-what-your-agent-may-do) hands them to your
  own constraint and approval callbacks for the duration of that call and
  nothing more; a policy violation records the tool and the rule, never the
  arguments that broke it.
- **Session keys and tags are stored as you wrote them in your own process,
  and reach a connected plane as a hash.** Detectors name the key untouched in
  their own local `message` and `details`, so a key session with an id you
  are willing to see in your own logs, not with an email address. The plane —
  hosted or self-hosted, the only destination the SDK sends to any more — gets
  `sha256(key)` and nothing else, unless you opt in with
  `send_session_keys=True`. By default the plane's own alert adapters (Slack,
  PagerDuty, a webhook) read only that hash back off the ledger too. Opt in
  and the raw key can reach a delivery — through your own `link_template`'s
  `{key}`, or a detector's own `message`/`details`, unredacted — because it
  is now sitting in the ledger for the plane to pass along exactly as told.
- **No network calls except the ones you configure.** No telemetry, no
  phone-home, no hosted backend you did not point us at. With no `token` and
  no `control_plane_url` set, runbound opens no sockets at all. With
  either, it sends hashes and counts on a fixed, published list of fields —
  [what we send](#what-we-send--hashes-and-counts-never-content) — and never
  prompts, replies, tool arguments or error messages. Slack, PagerDuty,
  Opsgenie and a signed webhook are the control plane's job now (0.3.0);
  the SDK never opens a socket to any of them itself.
- Exception messages from a failing tool are truncated to 500 characters and
  kept in-process for the event record; only the exception's class name, never
  its message, is what reaches the plane.

---

## Guarantees and limitations

**Fail-open is the promise.** A bug in runbound can never take down your agent.
Every failure inside detection, pricing, hashing, wrapping, or alerting is
caught, logged to the `"runbound"` logger, and swallowed — your call proceeds
as if runbound were not there. A detector that raises is skipped; a client
runbound cannot patch runs unguarded; a broken alerter is logged past. The only
exception that escapes on purpose is `GuardrailTripped`, and only if you chose
`on_anomaly="raise"`.

The one deliberate exception to fail-open is `init()` itself: bad configuration
raises immediately, at startup, where you will see it.

Honest limitations today:

- **runbound guards only what passes through its sensors.** An auto-wrapped
  or `wrap()`ped client, a `@runbound.tool`, `record_call()` / `@runbound.llm`,
  or the LangChain handler. Raw HTTP to a provider and SDKs that are not
  OpenAI- or Anthropic-shaped are invisible, and an invisible call looks exactly
  like a quiet one: every detector reads green.
  [What the SDK actually sees](#what-the-sdk-actually-sees--and-what-it-never-sees)
  says which numbers go blind without each sensor, and `runbound.coverage()` /
  `runbound.assert_guarded()` are how you check instead of hoping.
- **An abandoned stream is recorded, but as an estimate, and only when
  garbage collected.** A guarded stream reports normally when it is exhausted,
  closed, or exited; one that is simply dropped is still reported — as one
  partial call, with the time actually streamed and usage-or-estimated
  tokens — but only once Python collects it, which is not necessarily
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
  [Fleet mode](#fleet-mode--one-truth-across-all-your-workers-control-plane)
  shares the budget, the latch, the strike count, the org policy and the
  provider circuits; the four bullets below are what it does **not** fix.
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
- **Costs are estimates** from a static list-price table. Cached input, batch
  discounts, and negotiated rates are not modeled.
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

## Roadmap

Shipped since:
[fleet mode](#fleet-mode--one-truth-across-all-your-workers-control-plane) — a
shared budget, a shared latch, shared strikes, org-wide action policy with
dry-run rollout, fleet-wide provider circuits and a kill switch.

Still open:

- Cross-instance **spike baselines**, so a session's normal survives a restart
  and is shared between workers rather than re-learned per replica
- A shared **`max_calls` tally**, so "once per session" is once for the fleet
  and not once per worker
- Fleet-wide **fan-out counters and in-flight caps**, so a concurrency cap is
  the cluster's rather than each worker's
- Native wrappers for non-OpenAI-shaped SDKs (TGI, Bedrock, Vertex)
- OpenTelemetry export

Explicitly out of scope: hallucination scoring, answer-quality judgement,
prompt-injection blocking, and anything that needs an LLM to decide whether to
page you.

---

## License

MIT — see [LICENSE](LICENSE). Release history is in
[CHANGELOG.md](CHANGELOG.md). The promises above, stated as bounds with the
tests that assert them (and where nothing does yet), are in
[INVARIANTS.md](INVARIANTS.md).
