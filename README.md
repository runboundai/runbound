# runbound

**Runbound is a policy boundary around the tools an AI agent can call.** Declare a
tool with one decorator and every call is checked *before its body runs*: against
rules in your code and rules set in a console, against a quota, and, when you say
so, against a person's approval. When the control plane is unreachable the
consequential tools fail closed. Every call leaves one OpenTelemetry span.

Add one initialization line and one decorator per tool. No agent rewrites or
policy code required. runbound sees tool names, argument hashes, timings and
outcomes; it never stores, logs or sends a prompt, a reply or a result, and an
argument leaves the process only when the tool asks a person (the approver must
see it). It puts no gateway in your hot path, and never speaks to your users: when it
refuses something it hands control straight back to your own code.

> **1.0.0a3 is an alpha.** The detection, provider-wrapper and cost
> features of 0.11.x are gone; see [CHANGELOG.md](CHANGELOG.md) for what was
> removed and how to migrate.

## Install

```bash
pip install runbound --pre
```

**Console tool rules need runbound 1.x.** Plain `pip install runbound` still installs 0.11.2,
because 1.0 is a pre-release, and a 0.x worker **ignores** the rules you set on a tool in the
console: it enforces only the rules in your code. Install with `--pre` (or pin
`runbound>=1.0.0a2`), and see [Upgrading from 0.11.x](docs/guides/upgrading-from-0-11.md).

Requires **Python 3.10 or newer** (pip refuses the install on older versions, and `import runbound` on
3.9 raises an `ImportError` that says so). No runtime dependencies: stdlib only. There are two optional extras:
`pip install "runbound[langchain]"` and `pip install "runbound[otel]"`.

## Quick start

```python
import runbound

@runbound.tool(effects={"read"})
def lookup_order(order_id: str) -> dict:
    return {"order": order_id}

@runbound.tool(effects={"financial"}, approval="required", mode="deferred")
def issue_refund(order_id: str, amount: float) -> str:
    return "refunded"

runbound.init(token="rb_...", control_plane_url="https://plane.example", service="support-agent")
```

**Decorate before `init()`.** Import your tool modules, then call `init()`: the
first heartbeat carries the tool report, and a tool declared afterwards can run for
a moment before the plane knows its rules.

A refused call raises one typed exception, never a fake success:

```python
try:
    lookup_order(order_id=123)          # wrong type: refused before the body runs
except runbound.ExecutionRefused as exc:
    print(exc.reason, exc.retryable)    # invalid_arguments False
```

## The hook chain

Every call passes the same fixed chain; the first refusal wins and the body never
runs:

your rewrite hooks, argument validation against the signature, the tool's rules,
the console row, the capability class, the quota, the approval, your gate hooks,
the body, and finally the *post chain* on what it returned (redact secret-named
keys, truncate, scan for injection patterns, your post hooks). Hooks that fail are
treated by class: a hook error refuses a consequential or unclassified tool and is
logged and skipped for a read, write or external one. See
[How it works](docs/concepts/how-it-works.md).

## Per-tool policy

Rules come from two places and only ever tighten each other: the decorator
(`blocked`, `max_calls`, `constraint`, `require_approval`, `effects`) and rows set
in the console (blocked, session cap, approval, risk class, fail mode, quota). The
most restrictive wins, field by field. A row starts as **dry run**: it evaluates
everything, refuses nothing and records `would_refuse`, so you see what it would
have stopped before you switch it to **enforced**.

Console rows are enforced by runbound 1.x only: a worker on 0.x ignores them and keeps just
its code rules (see [Upgrading from 0.11.x](docs/guides/upgrading-from-0-11.md)).

A quota is a fixed clock window: "20 per clock hour", per service or per key. A
full quota refuses with `quota_exceeded` (retryable, with a `retry_after`). A quota
the plane cannot confirm (it is unreachable, or answers the take with 400 or 404)
follows the row's `fail_mode`: `closed` refuses with `quota_plane_unavailable`.
A row never loosens a tool: its `risk_class` can raise what the code declares, and
a tool that declares nothing stays unclassified, so it still fails closed. See
[Per-tool policy](docs/guides/policy.md).

## Approvals

`@runbound.tool(approval="required", mode="sync" | "deferred")` asks a person.
*Sync* waits for the answer up to `timeout_s`. *Deferred* returns
`{"status": "pending_approval", "request_id": ...}` at once; when a person
approves, a worker verifies an HMAC **seal** (keyed by `RUNBOUND_APPROVAL_SECRET`,
which never leaves your processes) and runs the tool with the approved arguments
through the whole chain again. `runbound.current_request()` is the idempotency key.
See [Approvals](docs/guides/approvals.md).

## When the plane is unreachable

`plane_status().mode` is `connecting`, `connected`, `degraded` or `unavailable`.
The last policy is kept on disk and enforced before the first heartbeat after a
restart. While the plane is away `read`, `write` and `external` tools keep running
on it; `financial`, `destructive`, `privileged` and unclassified tools refuse with
`reason="plane_unavailable"` once `consequential_grace_s` (default 60) has passed.
A worker that has been told a policy exists but has not been served it (and holds
no saved one) is treated the same way: those tools refuse until it arrives. See
[Plane loss](docs/guides/plane-loss.md).

A worker with `export_events=False` exports nothing, refusals included, so the
console will not show that worker's refusals.

## OpenTelemetry spans

Each call is a span named `execute_tool {tool}`, scope `runbound`, sent to your
OpenTelemetry provider or, with `init(otel_endpoint=...)`, posted as OTLP/JSON. It
carries the tool, the outcome, the decision and a hash of the arguments, never the
arguments. See [OpenTelemetry](docs/guides/opentelemetry.md) and
[LangChain](docs/guides/langchain.md).

## What runbound does not do

It guards the tools you decorate; it does not read prompts, wrap model providers,
detect loops, price tokens or sandbox a tool's body. `runbound.coverage()` and
`python -m runbound check` tell you which tools are guarded. See
[Boundaries](docs/concepts/boundaries.md) and
[Guarantees](docs/reference/guarantees.md).

## Documentation

The manual is in [`docs/`](docs/README.md): [Getting started](docs/getting-started.md),
[Configuration](docs/reference/configuration.md), [API](API.md),
[Handling refusals](docs/guides/handling-refusals.md),
[Privacy](docs/reference/privacy.md) and three runnable [examples](examples/README.md).

## Getting help

Open an issue at [github.com/runboundai/runbound/issues](https://github.com/runboundai/runbound/issues),
or email **runboundai@gmail.com** for anything you cannot post in public. **Never
paste** a token, an approval secret, an argument or a real session key into an
issue. Security issues go to email: see [SECURITY.md](SECURITY.md).

```python
import runbound
print(runbound.__version__)
print(runbound.coverage())   # after init() and your first guarded call
```

## License

MIT, see [LICENSE](LICENSE). Release history is in [CHANGELOG.md](CHANGELOG.md); the
invariants, each with the test that asserts it, are in [INVARIANTS.md](INVARIANTS.md).
