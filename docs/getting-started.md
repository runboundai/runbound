# Getting started (under 5 minutes)

[← Docs](README.md)

No account, no clone, no key. Everything on this page runs offline.

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

**2. Run the demo.** The whole core loop — a runaway, detected, narrowed to a
safer posture, a dangerous action refused, the run stopped — against a fake
provider transport, printed in plain language, ending `PASS`:

```bash
python -m runbound.demo
```

Read `runbound/demo.py` alongside the output; every step below builds the
same loop by hand, in your own code.

**3. Build it yourself.** Add one initialization line. No agent rewrites,
decorators, or policy code required.

```python
import runbound
runbound.init(budget_usd=5.0, on_anomaly="raise")   # everything below is now guarded
```

That one line already gives you dollar and token budgets, per-call caps, step
and event limits, run timeouts, error-storm detection with a circuit per
provider, loop detection from the tool calls a model itself asks for, and
spike detection — all local, all free, no network call. See [Three levels of
protection](concepts/three-levels.md) for the honest limits on what one line
alone can and cannot see.

**A budget that follows a person, not a process.** Give a run a **key** — any
id you already have, a user, a tenant, a job — and two independent budgets
apply at once: a **run** budget that starts fresh on every entry, and the
**key**'s own budget, which can carry a `budget_window` so it resets on a
schedule instead of accumulating forever:

```python
import runbound

runbound.init(run_budget_usd=1.0, budget_usd=20.0, budget_window="day", on_anomaly="raise")

with runbound.session("user:8842"):
    runbound.record_call("gpt-4o", 1_000, 1_000)          # a normal call
    runbound.record_call("gpt-4o", 1_000_000, 1_000_000)  # refused: over the $1 run budget
```

A call is checked against both at once and refused by whichever is tighter —
`exc.decision.level` says `"run"` or `"key"`. See [Runs keyed by any
id](guides/runs.md) for the full picture, including what happens when the key
outlives the process.

**One `@runbound.tool`, and a posture that narrows what it may do.** Only the
model-call sensor is automatic. A tool's own body is governed once — and only
once — it carries `@runbound.tool` and declares the capability classes it
carries (`read`, `write`, `external`, `financial`, `destructive`,
`privileged`). Narrowing the run's **posture** denies a whole class of
actions at once, without ending the run:

```python
import runbound

runbound.init(on_anomaly="raise")

@runbound.tool(effects={"financial"})
def issue_refund(user: str, amount: float) -> str:
    return "refunded"

runbound.enter_safe_mode(reason="spend looks abnormal", posture="restricted")

issue_refund("u1", 20.0)   # raises SafeModeViolation before the body runs
```

The refusal carries the same `Decision` every refusal does:
`exc.decision.boundary == "posture"`, and `exc.decision.reason` says which
capability class the current posture denied. Nothing here judges whether
`issue_refund` was a good idea — it only says whether this run, right now, is
allowed to try.

**Stopping.** A run that keeps going past its own limits — not just one
call over a dollar line, but its own action count, step count, or wall clock —
is refused outright:

```python
runbound.init(on_anomaly="raise", max_actions_per_run=2)
```

A third decorated tool call in that run raises before its body runs, with
`exc.decision.boundary == "blast_radius"`.

**4. Handle the refusal.** Every stop — a budget, a posture, a policy, a
provider outage — raises the same typed exception,
`runbound.ExecutionRefused` (`GuardrailTripped` is the identical class under
its older name). It is never a fake success and never silently swallowed:

```python
from runbound import ExecutionRefused

try:
    runbound.record_call("gpt-4o", 1_000_000, 1_000_000)
except ExecutionRefused as exc:
    # exc.reason: a stable code ("budget", "posture", "policy", "circuit", ...)
    # exc.retryable: would blindly repeating this exact call ever help?
    # exc.provider_called: did the provider already run before this refusal?
    log_and_decide(exc.reason, exc.retryable, exc.provider_called)
```

Your code decides what happens next — retry, queue for later, fall back, or
tell the caller. See [Handling refusals](guides/handling-refusals.md) for the
full contract, `retryable` and `retry_after`, and worked FastAPI, Flask,
background-job and streaming handlers.

**5. Check what is actually guarded.** An unguarded path looks exactly like a
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
on rather than assume it. See [How to be
sure](concepts/what-it-sees.md#how-to-be-sure) for the full picture,
including the once-per-process warning that fires on its own if a provider is
imported and nothing guarded has happened.

See it work against real code, offline, with no API key and no `openai`
package installed:

```bash
.venv/bin/python examples/core_loop_demo.py
.venv/bin/python examples/runaway_demo.py
.venv/bin/python examples/policy_demo.py
```

`examples/openai_agent.py` is the same wiring against a real OpenAI key.

---
