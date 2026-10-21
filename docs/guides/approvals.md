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
    # runbound.current_request() is the idempotency key while this runs (deferred mode only: it is None in sync mode)
    ...

result = delete_customer("c-42")
# {"status": "pending_approval", "request_id": "..."}
```

When a person approves, the plane offers the request to a worker on its next
hello. That worker verifies the **seal** (below), then runs the tool with the
approved arguments through the **whole** chain again (policy, quota, hooks, post
chain), reports `executed` or `failed`, and calls
`init(on_deferred_result=fn)` with `{request_id, tool, outcome, result | error}`.
If the policy moved after the approval and the execution-entry re-check refuses the call
(`approval_superseded`), the body does not run, the plane is told `superseded` with the
decision record (it gives the quota unit back), and `on_deferred_result` receives
`outcome="superseded"`, `error="approval_superseded"`: not `failed`.

- **Idempotency.** A request can be offered twice: a worker paused past its claim,
  or a replica that took over. `runbound.current_request()` is the request id
  while a *deferred* body runs; key your side effect on it. In sync mode the body runs once in your own call stack and `current_request()` is `None`: use a key of your own (the order id, say).
- **Claims.** A worker that is executing a request names it on every hello, which
  extends its claim, so a slow body is not re-offered. If the plane reports the
  claim lost, the SDK stops renewing it and starts nothing new for it.
- **Registration.** A deferred tool whose arguments cannot travel as JSON (`*args`, a
  file, a callable, a class with no codec, an unresolvable type hint) raises
  `ValueError` when decorated, naming the parameter and the fix. See
  [Injected parameters](#injected-parameters) and [Supported argument types](#supported-argument-types).
- **A policy that tightens after approval** stops the deferred execution; the
  approval does not override a rule that has since changed.

## Injected parameters

Real tools take two kinds of parameter: what the model supplies (always JSON) and what
your app supplies (a DB session, an HTTP client, the current tenant). Mark the second
kind `runbound.Injected`:

```python
from typing import Annotated
import runbound

runbound.init(provide={Session: make_session, "tenant": current_tenant})

@runbound.tool(effects={"write"}, approval="required", mode="deferred")
def archive(ticket: int, db: Annotated[Session, runbound.Injected],
            tenant: Annotated[str, runbound.Injected] = "acme") -> str:
    ...
```

An injected parameter is **never** stored, sealed, digested, shown to the approver, read
by a rule's conditions, or part of the span's `args_digest`: the request carries
`{"ticket": 42}` only. When the approved request runs, each injected parameter is
supplied, in this order:

1. a provider in `init(provide={...})`, keyed by parameter name (a name beats a type) or
   by type (the hint's class, then its bases): a zero-argument callable, called on every
   execution after the seal is verified and before any hook, so a fresh session per
   execution is the behaviour and nothing is held across the wait;
2. else the parameter's default (a callable default is called, a plain one used as is);
3. else the request is **not run**: it ends `failed`, `on_deferred_result` receives
   `error="injected_unavailable:<parameter>"`, and the SDK logs once per parameter.

A provider that raises is a `failed` outcome, not a refusal; the body never ran. In sync
mode the injected value is simply passed through from the original call; it is excluded
from the digest, the conditions and the console the same way, so a sync and a deferred
request for the same call show identical arguments. A value for an injected parameter
that arrives from the wire is never used.

LangChain's `InjectedToolArg` (and so `InjectedState` and `InjectedToolCallId`) is
recognised the same way when `langchain_core` is already imported; the SDK never imports it.

## Supported argument types

The model's arguments are stored as JSON, so each is encoded when the request is filed
and decoded to the type the tool declares when it runs. The digest and the seal are
computed over the encoded JSON, so the plane's recomputation is unchanged, and a rule's
condition reads the encoded form (a numeric comparison on a `Decimal` is not supported).

| Declared type | Stored as |
|---|---|
| `str`, `int`, `float`, `bool`, `list`, `dict`, `Literal`, `Optional`/`Union` of these, `TypedDict`, dataclass, simple `Enum`, `NewType` | itself |
| a pydantic v2 model (`BaseModel`) | `model_dump(mode="json")`; `model_validate` on execution |
| `datetime.datetime`, `date`, `time` | ISO 8601 (`isoformat()`; a naive datetime stays naive) |
| `decimal.Decimal`, `uuid.UUID`, `pathlib.Path` | a string |
| `list`, `dict` (values), `Optional`, `Union` of the above | element by element |

Anything else is refused when the tool is decorated, by name:

```text
tool 'a' cannot be mode="deferred": its arguments are stored and replayed as JSON, and parameter 'handle' (a file object) cannot be. Mark a parameter runbound.Injected if the app supplies it (it is then never stored), give it a JSON-friendly type, or use mode="sync"
tool 'c' cannot be mode="deferred": ... parameter 'cb' (a callable) cannot be; parameter 'thing' (Custom, which has no JSON form) cannot be. Mark a parameter ...
tool 'b' cannot be mode="deferred": ... '*items' (variable positional arguments cannot be stored). Mark a parameter ...
```

A union is decoded by the arm that needs a codec first, so `Union[str, datetime]` reads an
ISO string as a `datetime`.

**Limits.**

- **The JSON form must give the value back.** Before a deferred request is filed, each
  encoded argument is decoded again and compared (`==`) with the value the caller passed.
  A model whose JSON form is lossy fails that check: a `SecretStr` dumps masked, a field
  with `exclude=True` is dropped, an alias without `populate_by_name` cannot read its own
  dump, a custom serializer or computed field reshapes it. The call is then refused
  (`reason="invalid_arguments"`, not retryable) with
  `parameter 'x' cannot round-trip through JSON: <why>; use mode="sync" or a JSON-friendly type`,
  **before any approval exists**, and the tool never runs. Sync mode replays nothing and is not checked.
- **Type hints must resolve when the tool is decorated, except on an injected parameter.** A
  hint that names a class imported only under `TYPE_CHECKING` (or any name the module cannot
  resolve at that point) cannot be inspected, so a deferred tool with one raises `ValueError`
  at registration, naming the parameter: `if the app supplies 'db', annotate it
  Annotated[Session, runbound.Injected]`. A parameter that IS marked
  `Annotated[..., runbound.Injected]` (or LangChain's `InjectedToolArg`) may carry such a hint:
  its type is never needed, because the app supplies the value. Every other parameter still
  has to resolve. **Limit:** for such a parameter the provider is found by **parameter name**
  (`init(provide={"db": make_session})`) or by the parameter's **default**, never by type; with
  neither, the request ends `failed` with `injected_unavailable:<parameter>`.

## Bound approvals (`approval_bind`)

An approved request is normally executed by whoever gets to it: the original caller, a deferred worker, or a replay after a crash. `init(approval_bind=True)` asks the plane for
requests that can be **bound** to exactly one execution attempt (`capabilities: ["bind_v1"]` on the create and on the hello). It is **off by default** in 1.0.0a5; a plane
that does not know the key files a legacy request, and nothing below applies to it.

For a bind-required request the order before the body is: the person approves, the worker **binds** (`POST /v1/approvals/{id}/bind` with this attempt's `attempt_id`,
`action_id`, the `args_digest` and, for a deferred execution, the `claim_token` the plane gave it), the policy is re-checked (`approval_superseded` if it moved), the start
marker is emitted, the body runs. The bind is *consumption evidence*, not proof that anything started.

| The bind answers | The call |
|---|---|
| `first: true` | goes on |
| `first: false`, or a 409 (`bound_to_other_attempt`, `claim_lost`, `not_approved`, `approval_expired`, `approval_binding_mismatch`, or a terminal status) | `prior_outcome_unknown`: not run again (a deferred execution that lost its claim is dropped without a report) |
| no answer, a 5xx, 401/403/404, `503 downgrade_in_progress` | `approval_unavailable`: fail closed, whatever the tool's fail mode |

- **`engine.resume_admit(session, call, approval_ref)`** is the entry for a call whose approval already exists (the LangChain middleware will use it). It reserves, once and
  atomically with the check of the current limits, the `max_calls` unit, the repetition count and a `max_actions_per_run` slot; a limit that filled while the approval
  waited refuses now. It returns a reservation: `commit()` when the start marker has been emitted locally (a marker lost in export never releases it), `release()` on any
  refusal before the start. It asks nobody and takes no plane quota.
- **`approvals.resolve_replay(shared, request_id, resume_context)`** is the one bounded (1 s) by-request lookup: only a `200 {"exists": false}` with no approval in the resume
  context is a `first_run`; a pending, resumable, already-bound, terminal, refused or unreadable answer maps to `pending`, `resume`, `no_dispatch` or `refuse`
  (`replay_unavailable` for any auth failure, 404, 5xx or timeout, `key_mismatch` for the wrong filing key).
- **`approvals.withdraw(shared, request_id, reason)`** withdraws a *pending* request. A transport failure leaves the call withdrawn locally and never dispatched.
- **A quota unit.** An approval holds the unit the plane took when it was created. If the plane says it holds none (`quota_held: false`), the normal quota contract applies
  at execution (its fail mode included); a held unit is never taken twice.

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
