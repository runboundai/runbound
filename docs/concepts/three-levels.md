# Three levels of protection

[← Docs](../README.md)

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
[Fleet mode](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane)).
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
