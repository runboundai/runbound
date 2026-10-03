# Three levels of protection

[← Docs](../README.md)

You choose how far in to go. Each level is a small, separate step, and most of
the value arrives at the first one.

**Level 1: runtime limits.** Two lines. `runbound.init()` wraps the
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
`on_anomaly` — a raised `ExecutionRefused` your own handler catches (see
[Handling refusals](../guides/handling-refusals.md)), your callback, or a
WARNING line in your logs — and `on_trip` decides whether the session stays
stopped afterwards. Forever, offline. Being *told* about it without reading
your own logs or writing your own handler — Slack, PagerDuty, a signed webhook
— is a paid feature: it starts with a token from your runbound dashboard (see
[Fleet mode](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane)).
Nothing is decorated, no policy is written.

```python
import runbound
runbound.init(budget_usd=5.0, on_anomaly="raise")   # everything below is now guarded
```

Two honest limits hold at this level, and only this level: action-body
enforcement needs `@runbound.tool` (see Level 3), and an unsupported or raw
provider path is not guarded at all — `runbound.coverage()` is how you find
out which one you're in. See
[Boundaries](boundaries.md) for what this level deliberately does not decide.

**Level 2: identity limits.** One line per unit of work. Wrap each run in
`runbound.session(key)` — the key is any id you already have: a run id, a job,
a tenant, a customer — and every budget and posture above becomes per key
instead of per process: this run's budget, this run's ladder, this run stopped,
every other caller untouched. Circuits and caps stay per process. A run budget and a key's own budget are checked side by
side, and whichever is tighter wins — see
[Runs keyed by any id](../guides/runs.md#key-and-run-are-two-different-things).
With the control plane the key's budget and latch hold across every worker
that serves it.

```python
with runbound.session(f"run:{run_id}"):
    answer = client.chat.completions.create(...)
```

**Level 3: capability limits.** Only for the tools that matter. Agents have
stopped only writing text: they send the email, issue the refund, delete the
record. Decorate the two or three functions that are irreversible or reach
the outside world with `@runbound.tool`, write a policy (deny this tool, at
most once per run, needs approval, or a predicate of your own over the
arguments, run in your process), and the call is refused *before the
function body runs*. Narrowing a run's posture — `restricted`, `read_only`,
`stopped` — denies a whole class of actions at once, without ending the run;
the refusal carries the `Decision` that explains why. Roll a policy out in
dry run first and read what it would have blocked. The rules are yours;
runbound enforces and records them, it never judges the action.

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
would not stop" is this one. Free forever, on every level: see
[Free SDK, connected plane](free-and-connected.md) for exactly where the
open-source line sits.
