# Getting started

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

There are no runtime dependencies — stdlib only. There are two optional extras:
`pip install "runbound[langchain]"` for LangChain and
`pip install "runbound[otel]"` for OpenTelemetry export.

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

That one line sets a $5.00 dollar budget, and `on_anomaly="raise"` makes a
refusal raise. Four things are on by default and need nothing more:
error-storm detection (10 failures), a circuit per provider that notifies but
does not block (`on_provider_failure="open"` makes it block), loop detection
from the tool calls a model itself asks for (a log line, then an alert;
**containment needs `on_spike="limit"`**), and spike detection (`on_spike="notify"`).
Token limits, step and event limits, per-call caps and run timeouts are off
until you set them. All of it is local, free, no network call. See [Three levels of
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
`exc.decision.level` says `"run"` or `"key"`. The dollar figure is a list-price
estimate (the model's published per-token price times the tokens counted), not
your invoice; see [What is exact and what is estimated](concepts/what-it-sees.md). See [Runs keyed by any
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

issue_refund("u1", 20.0)   # refused before the body runs
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

**A loop is answered in rungs, not all at once.** A tool called again and
again with the same arguments is a loop. The third repeat is a line in your
log, the sixth pages a person, and the ninth narrows the session to the
`restricted` posture, so the financial tool is refused before its body runs
while reads and writes still work. If it keeps repeating, the session is
closed for a cooldown and then served again. **Containment is off until you
set `on_spike="limit"`**: with the default (`"notify"`) the ninth call is one
more notice and the tool runs. It also needs a keyed session and
`on_anomaly="raise"`.

```python
import time

import runbound

runbound.init(
    on_anomaly="raise",
    on_spike="limit",               # without this, containment is off: the ninth call would just run
    spike_limit_calls=2,            # how many more repeats the limited session gets before it is closed
    spike_cooldown_seconds=0.5,     # the default is 300; short here so this page can run
)

ran = []

@runbound.tool(effects={"financial"})
def issue_refund(amount: float) -> str:
    ran.append(amount)
    return "refunded"

refused = []
with runbound.session("user:7"):
    for call in range(1, 13):               # the same refund, again and again
        try:
            issue_refund(25.0)
        except runbound.ExecutionRefused as exc:
            refused.append((call, exc.reason))

# Eight calls ran. The ninth was refused before its body ran, and so was every one after it.
print(len(ran), "ran; first refused:", refused[0])

# The session was closed for its cooldown. Entering it now is refused; once the cooldown is
# over, the same key is served again.
try:
    with runbound.session("user:7"):
        pass
except runbound.ExecutionRefused:
    pass
time.sleep(0.7)
with runbound.session("user:7"):
    print(issue_refund(25.0))
```

For the same story told from `events()`, see [A runaway, start to
finish](guides/a-runaway.md).

**4. Handle the refusal.** A stop raises a subclass of one typed exception,
`runbound.ExecutionRefused`; catch that one name. Posture and policy refusals
raise always; a budget a finished call crossed, or a provider outage, raises
under `on_anomaly="raise"` and only warns by default (a request whose stated
output cap would cross the budget is refused at the door whatever `on_anomaly`
says; an open circuit blocks only under `on_provider_failure="open"`). (`SafeModeViolation` and
`PolicyViolation` are `ExecutionRefused`; `GuardrailTripped` is the same class
under its older name.) It is never a fake success and never silently swallowed:

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

Or ask runbound for the whole picture in one command: which clients are
wrapped, which tools are guarded and under what capability classes, whether a
plane is connected (its address, never its key), the posture in force, the
budgets and limits, and the last ten events. It makes no network call and needs
no key:

```python
import runbound

runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")

@runbound.tool(effects={"financial"})
def issue_refund(user: str, amount: float) -> str:
    return "refunded"

@runbound.tool(effects={"read"})
def lookup_order(order_id: str) -> str:
    return "found"

report = runbound.check()        # prints what is guarded in THIS process, and returns it
assert report["guarded"]          # the same fact `python -m runbound check` turns into its exit code
```

From a terminal, or a CI step, point the same report at a script or a module:

```bash
python -m runbound check agent.py        # add --json for a stable machine-readable form
```

It loads the target as `runbound_check` (so an `if __name__ == "__main__":`
block, a server loop for instance, does not run), then reports; it exits `0` when
at least one client, tool or guarded call exists, `1` when nothing is guarded,
and `2` when the target cannot be loaded. Code that wires runbound only inside a
`main()` should call `runbound.check()` there instead.

`coverage()` reports `auto_wrapped` labels, `wrapped_clients`,
`decorated_tools`, `guarded_calls`, `tool_calls_seen`, `keyed_sessions_seen`,
`providers_imported`, `providers_unguarded` (imported but never seen guarded),
`last_guarded_call_age_s`, and any `warnings` — read it to know the guard is
on rather than assume it. See [How to be
sure](concepts/what-it-sees.md#how-to-be-sure) for the full picture,
including the once-per-process warning that fires on its own if a provider is
imported and nothing guarded has happened.

**6. Verify it.** Everything above leaves a record. Print what this process
saw:

```python
import runbound

runbound.init(budget_usd=0.01, on_anomaly="raise")

try:
    runbound.record_call("gpt-4o", 1_000_000, 1_000_000)   # far over a one-cent budget
except runbound.ExecutionRefused:
    pass

for event in runbound.events():
    print(event["kind"], event.get("detector"), event.get("reacted"))
```

You should see an `anomaly` row and a `refusal` row, both from the `budget`
detector: the refusal you just caused. With a control plane connected, the
same `refusal` appears as a row on the console's Refused actions page.

See it work against real code, offline, with no API key and no `openai`
package installed. These examples are in the repository, not in the pip
package, so this step needs the clone from step 1:

```bash
.venv/bin/python examples/core_loop_demo.py
.venv/bin/python examples/runaway_demo.py
.venv/bin/python examples/policy_demo.py
```

`examples/openai_agent.py` is the same wiring against a real OpenAI key.

---
