# Approvals

[← Docs](../README.md)

An approval puts a person in front of one tool call. The tool is declared in
code; the person decides in the control plane's console (or through a signed
link the plane sends to a channel you configured there). The SDK asks, waits or
returns, and runs the body only after an approval.

```python
import runbound

runbound.init(token="...", control_plane_url="https://plane.example", service="payments")

@runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=300)
def wire_money(account: str, amount: float) -> str:
    ...
```

A console row can also turn approval on for a tool whose code says nothing
(`approval_required`); the two are folded, and code wins on the mode. See
[Per-tool policy](policy.md).

## Where it sits

Approval is **slot 5** of the [hook chain](../concepts/how-it-works.md). The request
is built from the *final* arguments, after rewrite hooks, validation, the policy
rules and the capability rules have all passed. A call that those refuse never
asks anybody and never takes a quota unit. Your own gate hooks run after the
approval and can still refuse.

With no plane connected the call is refused at once with
`reason="approval_unavailable"`; the body never runs.

## Sync (the default)

The call waits for the answer, up to `timeout_s` (default 600). It long-polls the
plane (`GET /v1/approvals/{id}?wait_s=25`) against a monotonic deadline, so the
plane is the only clock. A transport error is retried until the deadline and is
never read as a denial. Async tools wait off the event loop.

| Outcome | What the caller gets |
|---|---|
| approved | the body runs; the outcome is reported to the plane |
| denied | `ExecutionRefused`, `reason="approval_denied"` |
| no answer in time | `ExecutionRefused`, `reason="approval_timeout"` |
| plane away (new request) | `ExecutionRefused`, `reason="approval_unavailable"` |
| the quota is full | `ExecutionRefused`, `reason="quota_exceeded"` with `retry_after` |

A sync approval already waiting when the plane goes away rides the outage out to
its own deadline.

## Deferred

`mode="deferred"` returns at once, without running the body:

```python
@runbound.tool(effects={"write"}, approval="required", mode="deferred")
def delete_customer(customer_id: str) -> str:
    # runbound.current_request() is the idempotency key while this runs
    ...

result = delete_customer("c-42")
# {"status": "pending_approval", "request_id": "..."}
```

When a person approves, the plane offers the request to a worker on its next
hello. That worker verifies the **seal** (below), then runs the tool with the
approved arguments through the **whole** chain again (policy, quota, hooks, post
chain), reports `executed` or `failed`, and calls
`init(on_deferred_result=fn)` with `{request_id, tool, outcome, result | error}`.

- **Idempotency.** A request can be offered twice: a worker paused past its claim,
  or a replica that took over. `runbound.current_request()` is the request id
  while the body runs; key your side effect on it.
- **Claims.** A worker that is executing a request names it on every hello, which
  extends its claim, so a slow body is not re-offered. If the plane reports the
  claim lost, the SDK stops renewing it and starts nothing new for it.
- **Registration.** A deferred tool whose arguments cannot travel as JSON (`*args`,
  a `datetime`, an unresolvable type hint) raises `ValueError` when decorated.
- **A policy that tightens after approval** stops the deferred execution; the
  approval does not override a rule that has since changed.

## The seal

Deferred requests are sealed so that the plane (or anyone who can reach it)
cannot make a worker run something a person did not approve:

```
seal = {"v": 1, "not_after": "<ISO Z>",
        "mac": HMAC-SHA256(secret, canonical_json({v, service, tool, request_id, args, not_after}))}
```

The secret is per service: `init(approval_secret="...")` or the environment
variable `RUNBOUND_APPROVAL_SECRET`, the same on every replica. **It is never
sent to the plane.** On a mismatch, or when the seal is older than `not_after`
(plus 300 s of tolerance), nothing runs, the plane is told `tampered`, and
`on_deferred_result` receives `outcome="tampered"`. A clock problem is reported
as `clock_skew`, not as tampering.

With no secret nothing is sealed. Only the worker that made the request can then
run it, from its own memory, and the SDK warns once at registration. Set the
secret for any fleet of more than one process.

## Quota

A tool that will ask a person takes no quota unit in the SDK. The plane takes it
when the approval is created, and a deferred execution never takes a second.
A tool that asks a person is never leased quota (code-declared or row-declared).

## When the report cannot be delivered

If the outcome cannot reach the plane after a few retries, it is appended to a
`<service>.reports.jsonl` file next to the policy cache (digests and ids only,
never an argument or a result) and delivered, once, on the next connected hello.
Otherwise an action that ran would look unexecuted and the plane would expire it.

## What it does not do

The approval request carries the final arguments, because the approver has to see
them. A compromised plane can still *approve* a real pending request: it holds the
decision. The seal stops it from changing what is run, not from deciding.
See [Guarantees](../reference/guarantees.md).
