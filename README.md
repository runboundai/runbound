# runbound

**Runbound is an execution control plane for autonomous AI: it decides what
an agent may consume, how far it may run, and what it may do, and enforces
those boundaries across the fleet.** The open-source SDK is the runtime for
that — free forever, no account required.

Add one initialization line. No agent rewrites, decorators, or policy code
required. runbound counts, times, hashes and prices what an agent does; it
never reads a prompt or a reply (and never stores, logs or sends one on),
puts no gateway in your hot path, and never speaks to your users — when it refuses something, it hands control straight
back to your own code.

## Install

```bash
pip install runbound
```

See the whole core loop in thirty seconds — no clone, no key, no network:

```bash
python -m runbound.demo
```

There are no runtime dependencies — stdlib only. LangChain support is the one
extra: `pip install "runbound[langchain]"`.

## Quick start

```python
import runbound
runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")

runbound.record_call("gpt-4o", tokens_in=1_000, tokens_out=500)   # guarded; needs no provider package
```

With a provider SDK (`pip install openai`), `runbound.init()` wraps the client
for you, automatic for a client built after `init()`:

```python
from openai import OpenAI

client = OpenAI()   # wrapped: every call below is counted, priced and limited
```

Give it a user key when a limit should follow that person across requests
instead of resetting every call — a **run** budget that starts fresh on every
entry, checked side by side with the **key**'s own daily total:

```python
runbound.init(run_budget_usd=1.0, budget_usd=20.0, budget_window="day")

with runbound.session(f"user:{user_id}"):
    client.chat.completions.create(...)   # refused by whichever budget is tighter
```

Decorate the two or three tools that matter and runbound refuses a call
before its body runs:

```python
@runbound.tool(max_calls=1)
def issue_refund(user: str, amount: float): ...
```

The five-minute version — wiring every limit, checking what's actually
guarded, and handling a refusal — is in
[Getting started](docs/getting-started.md), which also shows the graded loop
and [a runaway, start to finish](docs/guides/a-runaway.md).

Not a Python agent? [Which door](docs/which-door.md) covers the gateway (change
one base URL) and the action API (one HTTP call before an action).

## Three levels of protection

**Level 1: runtime limits.** Two lines. `runbound.init()` wraps the OpenAI
and Anthropic clients for you — wrap is automatic for a client built after
`init()`, or call `runbound.wrap(client)` yourself for one built earlier or
shaped like a gateway. From then on every model call is counted, timed and
priced with no network call and no account:

- dollar limits
- token limits
- per-call caps
- run and event limits
- run timeouts
- error storms
- provider circuits
- loop detection from the tool calls a model asks for
- spike detection
- local safe mode
- local enforcement

These are what you can switch on, not what one line sets: `init(budget_usd=5.0)`
sets the dollar budget; token, step, event, per-call and run limits are off until
you state them. Error-storm, loop and spike detection are on by default and notify;
a loop is only contained with `on_spike="limit"`.

Two honest limits, in the same breath: this level only sees what a wrapped
client or `@runbound.tool` reports it, so an action's own body is only
governed once that tool carries `@runbound.tool`, and a raw or unsupported
provider path is not guarded at all — `runbound.coverage()` is how you find
out which one you're in.

**Level 2: identity limits.** One line per unit of work,
`runbound.session(key)` — the key is any id you already have: a run, a job,
a tenant, a customer. Every budget and posture above becomes per key instead of per
process, so one caller's run stops without touching anyone else's. Circuits and
caps (fan-out, in-flight, active sessions) stay per process.

**Level 3: capability limits.** Decorate the tools that are irreversible or
reach the outside world with `@runbound.tool`, state a rule (deny, a call
cap, an approval, a predicate over the arguments), and the call is refused
*before its body runs*. Narrowing a run's posture — `restricted`,
`read_only`, `stopped` — denies a whole class of actions at once without
ending the run.

## When Runbound refuses

There is no fake success. A refusal is a typed `ExecutionRefused` — never a
swallowed error, never an answer made up to look normal — carrying `reason`,
`retryable` and `provider_called`, so your own code decides what happens
next: retry, queue, fall back, or tell the caller. Catch that one name:
every refusal is an `ExecutionRefused` (`GuardrailTripped` is the same class,
and `SafeModeViolation` and `PolicyViolation` are subclasses), and it is never
one of a provider's own exceptions. See
[Handling refusals](docs/guides/handling-refusals.md).

## Content-independent enforcement

Every decision above is made from counts, hashes, timings and prices — never
from reading what an agent said or what a model answered:

- Prompts and model replies are never read, stored, or sent.
- Tool arguments are sha256-hashed before storage, salted per process; raw
  arguments never leave your process.
- A session key reaches a connected plane as a hash, never in the clear. That
  hash is unsalted, so the same caller is recognisable across your workers.
- No network calls except the ones you configure — no telemetry, no
  phone-home, nothing sent anywhere with no `token` set.

Full accounting: [What the SDK actually sees](docs/concepts/what-it-sees.md)
and [Boundaries](docs/concepts/boundaries.md) — what runbound deliberately
does not do, and what to use instead.

## Open-source runtime. Cloud control plane.

Everything above is free forever, in the open-source SDK, configurable in
code, with no account. A connected control plane adds what one process
cannot give itself: coordination across every worker in your fleet, state
that survives a restart, central policy and posture, and durable evidence.
See [Free SDK, connected plane](docs/concepts/free-and-connected.md).

## Documentation

The manual lives in [`docs/`](docs/README.md), and that repository manual is
canonical: [runbound.co/docs](https://runbound.co/docs) renders it at build time,
with the snippets the tests execute.

- **[Getting started](docs/getting-started.md)** — install, the core loop, and
  checking what is actually guarded.
- **[Concepts](docs/README.md#concepts)** — the three levels, boundaries, and
  the free SDK / connected plane split.
- **[Guides](docs/README.md#guides)** — keyed runs, action policy, handling
  refusals, async and streaming.
- **[Reference](docs/README.md#reference)** — every configuration field, the
  detectors, and the guarantees.

[Roadmap](docs/roadmap.md) is what is shipped, what is still open, and what is
explicitly out of scope.

## Getting help

**Something not working, or a question?** Open an issue at
[github.com/runboundai/runbound/issues](https://github.com/runboundai/runbound/issues),
or email **runboundai@gmail.com** if it is not something you can post in
public.

```python
import runbound
print(runbound.__version__)
print(runbound.coverage())   # after init() and your first guarded call
```

`coverage()` shows at a glance whether runbound can see your traffic at all —
the most common cause of "it isn't catching anything." **Never paste** an API
key, a control-plane token, a prompt, a reply, or a real session key into an
issue. Security issues go to email, not to an issue — see
[SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). Release history is in
[CHANGELOG.md](CHANGELOG.md). The promises above, stated as bounds with the
tests that assert them, are in [INVARIANTS.md](INVARIANTS.md).
