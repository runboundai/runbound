# Getting started

[← Docs](README.md)

No account, no key, no network for steps 1 to 5. A control plane (step 6) adds the
console, quotas, approvals and a fleet-wide view.

**1. Install.**

```bash
pip install runbound --pre      # 1.0.0a4 is an alpha; without --pre you get 0.11.2, which ignores console rules
```

There are no runtime dependencies: stdlib only. There are two optional extras:
`pip install "runbound[langchain]"` for LangChain and
`pip install "runbound[otel]"` for OpenTelemetry.

**2. Guard your tools.** Add one initialization line and one decorator per tool.
No agent rewrites or policy code required. Decorate the functions that act;
`@runbound.tool` checks every call before the body runs and records how it ended.

```python
import runbound

@runbound.tool(effects={"read"})
def lookup_order(order_id: str) -> dict:
    return {"order": order_id}

@runbound.tool(effects={"financial"}, max_calls=1)
def issue_refund(order_id: str, amount: float) -> str:
    return "refunded"

runbound.init(max_actions_per_run=20)
```

**Decorate before `init()`.** Import your tool modules first, then call `init()`:
the first heartbeat to a control plane carries the tool report, and a tool that
appears afterwards can run for a moment before the plane knows its rules.

`effects` is the tool's capability class (`read`, `write`, `external`, `financial`,
`destructive`, `privileged`). It decides what is refused when the control plane is
unreachable: the consequential classes fail closed, the rest keep running on the
last policy. See [Plane loss](guides/plane-loss.md).

Arguments are checked against the function's signature and type hints before the
body runs; nothing is coerced. A call with an invalid argument is refused with
`reason="invalid_arguments"`.

**3. Handle the refusal.** A stop raises one typed exception,
`runbound.ExecutionRefused`; catch that one name. It carries `reason`, `retryable`
and, for a quota, `retry_after`. It is never a fake success and never swallowed:

```python
import runbound
from runbound import ExecutionRefused

@runbound.tool(max_calls=1)
def lookup_order(order_id: str) -> dict:
    return {"order": order_id}

runbound.init()

caught = None
with runbound.session("run:refused"):
    lookup_order("A1")
    try:
        lookup_order("A2")
    except ExecutionRefused as exc:
        caught = (exc.reason, exc.retryable)
```

Your code decides what happens next: retry, queue, fall back, or tell the caller.
See [Handling refusals](guides/handling-refusals.md).

**4. State the rules.** Rules live next to the tool, in `init(tool_policy=...)`, or
as rows in the console. They only ever add restrictions; the most restrictive
wins. A row can be set to `dry_run` first: it evaluates everything, refuses
nothing and records what it *would* have refused, then you switch it to
`enforced`. See [Per-tool policy](guides/policy.md).

**5. Check what is guarded.** An unguarded path looks exactly like a quiet one.
`check()` reports which tools are guarded and under what classes, whether a plane
is connected (its address, never its key), and the last ten events. It makes no
network call:

```python
import runbound

@runbound.tool(effects={"financial"})
def issue_refund(user: str, amount: float) -> str:
    return "refunded"

runbound.init()
report = runbound.check()        # prints what is guarded in THIS process, and returns it
assert report["guarded"]
```

From a terminal or a CI step, point the same report at a script or a module:

```bash
python -m runbound check agent.py        # add --json for a stable machine-readable form
```

It loads the target as `runbound_check` (an `if __name__ == "__main__":` block does
not run). It exits `0` when at least one tool is decorated or a call was guarded,
`1` when nothing is guarded, and `2` when the target cannot be loaded.

**6. Connect a control plane (optional).**

```python
runbound.init(
    token="rb_...",
    control_plane_url="https://plane.example",
    service="payments",
    worker_id="payments-7f9c",   # a stable name: the pod or host name
    approval_secret="...",   # or RUNBOUND_APPROVAL_SECRET, for deferred approvals
)
```

Set `worker_id` to a stable name (a pod or host name) so a restart is recognised as
the same worker; each process also has its own boot id, which tells the processes
apart. Unset, it is `<hostname>:<pid>:<random>`, a new name on every start.

The SDK says hello to the plane every `poll_s` seconds, fetches the policy when it
changes, and keeps the last policy on disk so a restart is enforced before the
first answer. Then the console can block a tool, cap it, put a quota on it
("20 per clock hour") or ask a person first. See [Approvals](guides/approvals.md),
[Plane loss](guides/plane-loss.md) and [Free SDK, connected plane](concepts/free-and-connected.md).

The runnable examples are in [`examples/`](../examples/README.md).
