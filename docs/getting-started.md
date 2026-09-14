# Quick start (under 5 minutes)

[← Docs](README.md)

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

That run is stopped on the model call that takes its spend past $5, and on its
51st model turn. The dollar wall lands **after** the call that crossed it —
usage exists only once a call has returned — and `budget_admission=True` is the
opt-in pre-call estimate; [what is exact and what is
estimated](#what-is-exact-and-what-is-estimated) is the whole line between the
two. `max_steps` counts turns: one `create()` call is one step however many
tools the model then asks for and your code then dispatches. The knob that
counts everything instead — model calls, tool calls, tool requests, failures —
is `max_events`, and a tool-using run records several of those per turn. They
are two different walls, not one wall at two thresholds; set either, or both.
See [`max_steps` and
`max_events`](#max_steps-and-max_events-steps-are-turns-events-are-events).

`wrap()` returns the same client object it was given, so nothing downstream
changes — and with `auto_wrap` at its default you can drop it entirely for an
OpenAI or Anthropic client built after `init()`. Keep it for the unusual cases:
a gateway or proxy object, or a client constructed before `init()` ran. What
`init()` cannot do for you is the other two sensors: a tool is only guarded if
it carries `@runbound.tool`, and limits are only per key inside
`runbound.session(key)`. [What the SDK actually sees](concepts/what-it-sees.md#what-the-sdk-actually-sees--and-what-it-never-sees)
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
on rather than assume it. See [How to be sure](concepts/what-it-sees.md#how-to-be-sure) for the full
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
[`PolicyViolation`](guides/policy.md#action-policy--rules-for-what-your-agent-may-do) (the agent
tried something your rules forbid) and
[`CircuitOpen`](reference/circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker) (the provider is
down and we failed the call fast). Both are `GuardrailTripped`, so the broad
catch above keeps working unchanged.

See it work, offline, with no API key and no `openai` package installed:

```bash
.venv/bin/python examples/runaway_demo.py
.venv/bin/python examples/policy_demo.py
```

The first runs a deliberately looping agent, stops it on the third identical
tool call before that call executes, then runs a spend spiral and stops it on
the call that took it over the budget cap. The second is a support agent that
tries four things the business wrote down that it may not do — a deny list, a
per-session call cap, an argument constraint and an approval gate — and each
one is refused before the function body runs.
`examples/openai_agent.py` is the same wiring against a real OpenAI key.

The second worked example — many people behind one service, each on their own
key — is in [Runs keyed by any id](guides/runs.md#runs-keyed-by-any-id) below.

---
