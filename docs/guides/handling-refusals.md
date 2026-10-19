# Handling refusals

[← Docs](../README.md)

A refused tool call raises an exception before the tool body runs. It is never
a fake result: the body did not run, and the caller is told so. What happens
next (tell the model, ask a person, try later, give up) is your code's
decision.

## The exceptions

```text
ExecutionRefused            every refusal; GuardrailTripped is the same class
└── PolicyViolation         a code rule refused: exc.violation names it
    └── CapabilityRefused   an init(capabilities=...) class rule refused
```

`runbound.GuardrailTripped` is not a subclass: it is the identical class object
as `ExecutionRefused`, so `except GuardrailTripped:` and
`except ExecutionRefused:` catch the same things. Catch the narrowest class
first.

Every refusal carries:

| Attribute | What it is |
|---|---|
| `reason` | a stable code from the closed set below. Switch on this, not on the message. |
| `retryable` | `True` only when repeating the same call unchanged could succeed (`plane`, `plane_unavailable`, `approval_unavailable`, `quota_exceeded`, `quota_plane_unavailable`). |
| `retry_after` | seconds until a retry could succeed when that is known (a quota's window), else `None`. |
| `boundary` | the Decision's dimension: `policy`, `row`, `capability`, `blast_radius`, `halt`, `plane`, `arguments`, `hook`, `hook_error` or `approval`. |
| `decision` | the full `Decision` (`verdict`, `kind`, `boundary`, `level`, `reason`, `detector`, `evaluation`). |
| `scope` | `{"level": ..., "key_hash": ...}`: `session`, `process` (a class rule) or `fleet` (a halt, the plane gone), and the sha256 hash of the session key, never the key. |
| `anomaly` | the recorded anomaly; `str(exc)` is its message. |
| `violation` | `PolicyViolation` and `CapabilityRefused` only: `tool`, `rule` (`deny`, `allow`, `max_calls`, `constraint`, `approval`, `capability`), `reason`, `details`. |

`str(exc)` names the tool and, inside a keyed session, the session key. Give
the model your own sentence built from `exc.reason` if the key should not
reach it. No refusal message contains a tool argument.

`runbound.is_retryable(exc)` is `exc.retryable` for a refusal and `False` for
any other exception, so it works as a predicate in a generic retry loop.

## Every reason

`runbound.exceptions.REASONS` is the closed set; `exc.reason` never returns
anything else.

| `reason` | Raised as | When | Retryable |
|---|---|---|---|
| `policy` | `PolicyViolation` | a code rule: not on `allow`, `max_calls`, a `constraint` returning `False` or raising, or the same rule from an org policy. A session stopped by `block_and_latch` raises its stored refusal again (as `ExecutionRefused`). | no |
| `approval` | `PolicyViolation` | a `require_approval` callback returned `False` or raised, or no callback exists for the tool. | no |
| `capability` | `CapabilityRefused` | a class rule denies (or says `approve` for) one of the tool's classes, or the tool is unclassified while some class is narrowed. | no |
| `blast_radius` | `ExecutionRefused` | `max_actions_per_run` executed calls already ran in this session. | no |
| `halt` | `ExecutionRefused` | the plane halted the service (whatever `on_halt` says). A halt holds until the plane lifts it. | no |
| `invalid_arguments` | `ExecutionRefused` | the arguments do not fit the signature or type hints. Nothing is coerced. | no |
| `hook_refused` | `ExecutionRefused` | one of your `pre_hooks` or `gate_hooks` returned `hooks.refuse(...)` or `False`. | no |
| `hook_error` | `ExecutionRefused` | one of your hooks raised or outlived `hook_timeout_s` on a `financial`, `destructive`, `privileged` or unclassified tool; or a built-in check (validation, policy, class rules) raised. | no |
| `tool_blocked` | `PolicyViolation` for a code rule, `ExecutionRefused` for a row | the tool is blocked: `blocked=True`, a `deny` entry (or an org policy's), or a console row. | no |
| `session_max_calls` | `ExecutionRefused` | a console row's per-session cap is reached. | no |
| `quota_exceeded` | `ExecutionRefused` | a row's quota is used up for this window, or the plane answered an approval request with 429. `retry_after` is set. | yes |
| `quota_plane_unavailable` | `ExecutionRefused` | a quota cannot be checked (the link is not `connected`, the take failed, or the plane answered it with 400 or 404) and the row's `fail_mode` is `closed`. | yes |
| `plane_unavailable` | `ExecutionRefused` | the plane is gone, or it announced a policy this worker has not received, and the tool is `financial`, `destructive`, `privileged` or unclassified. See [Plane loss](plane-loss.md). | yes |
| `policy_unavailable` | `ExecutionRefused` | the worker has not yet had a first answered hello or loaded a sealed policy cache, so no tool of any class runs yet (read, write, external; the other classes get `plane_unavailable`). See [Plane loss](plane-loss.md). | yes |
| `policy_expired_restricted` | `ExecutionRefused` | the plane is unreachable, the policy this worker holds is more than 24 hours old, and that policy blocked this tool or needed a person's approval for it. Fail mode does not let such a tool through. | no |
| `policy_halted` | `ExecutionRefused` | the plane halted this one tool because this worker cannot enforce a restriction the plane holds for it and has no stricter fallback (`halted_tools` in the hello reply). Other tools run; it holds until the plane lifts it. | no |
| `policy_unsupported` | `ExecutionRefused` | the console has a rule for this tool that this SDK cannot interpret safely: it carries a field this SDK does not know, or a value the console itself would not accept. Only this tool is refused, until the rule is changed or the SDK is upgraded. | no |
| `approval_denied` | `ExecutionRefused` | a person denied the request. | no |
| `approval_timeout` | `ExecutionRefused` | nobody decided within `timeout_s`, the request expired, or the plane could not be reached to file it before the deadline. | no |
| `approval_unavailable` | `ExecutionRefused` | a new approval cannot be asked: no plane, the link is not `connected`, the arguments are not JSON, or the plane refused or stopped answering the request. | no |
| `approval_superseded` | `ExecutionRefused` | a person approved the call (or a grant admitted it), but the policy the worker holds now refuses it: the tool was blocked, a deny line or an unreadable rule now applies, or the session cap is reached. The approval never overrides the policy; ask again under the new one. | no |
| `approval_tampered` | never raised | a deferred request whose seal does not verify. Nothing runs; `on_deferred_result` gets `outcome="tampered"` and `error="approval_tampered"`. | |
| `plane` | not raised | in the closed set for older handlers; no check in this release produces it. | yes |

`quota_exceeded` and `quota_plane_unavailable` are retryable and carry a `retry_after`:
wait that long first, because a retry before then is refused again. For
`quota_exceeded` it is the rest of the window; for `quota_plane_unavailable` it is
the plane poll interval (the next chance for the link to come back).

## Catching

```python
import runbound
from runbound import CapabilityRefused, ExecutionRefused, PolicyViolation

runbound.init(capabilities={"destructive": "deny"})


@runbound.tool(effects={"destructive"}, reviewed=True)
def drop_table(name: str) -> str:
    return f"dropped {name}"


def run_tool(name: str) -> dict:
    try:
        return {"ok": drop_table(name)}
    except CapabilityRefused as exc:
        return {"refused": exc.reason, "class": exc.violation.details["denied_class"]}
    except PolicyViolation as exc:
        return {"refused": exc.reason, "rule": exc.violation.rule}
    except ExecutionRefused as exc:
        return {"refused": exc.reason, "retryable": exc.retryable, "retry_after": exc.retry_after}


print(run_tool("orders"))  # {'refused': 'capability', 'class': 'destructive'}
```

A bounded retry that only repeats what can succeed:

```python
import time

import runbound


def call_with_retry(fn, *args, attempts=3, **kwargs):
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except runbound.ExecutionRefused as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            time.sleep(exc.retry_after or 1.0)
```

A background job re-queues on `retryable`, schedules on `retry_after`, and
records the reason either way:

```python
from runbound import ExecutionRefused


def run_job(job):
    try:
        job.run()
    except ExecutionRefused as exc:
        job.record_failure(reason=exc.reason, message=str(exc))
        if exc.retryable:
            job.requeue(delay_seconds=30.0)
        elif exc.retry_after is not None:
            job.requeue(delay_seconds=exc.retry_after)
        else:
            job.mark_failed()
```

## Not refusals

- **The tool's own exception.** If the body raises, that exception is re-raised
  unchanged (a `tool_error` event is recorded). Only checks before the body
  raise `ExecutionRefused`.
- **A withheld result.** When a post hook withholds the output, or a built-in
  post step fails, the call returns the string `"Output withheld: <reason>"`
  and its `tool_result` outcome is `withheld`. Nothing is raised.
- **A deferred approval.** `@runbound.tool(approval="required", mode="deferred")`
  does not raise while it waits. The call returns
  `{"status": "pending_approval", "request_id": "..."}` at once and the body has
  not run. Once a person approves, a worker runs the tool and calls
  `init(on_deferred_result=fn)` with `{"request_id", "tool", "outcome", "result"
  | "error"}`, where `outcome` is `executed`, `failed` (`error` is the exception
  type name), `tampered`, `clock_skew` or `superseded` (the policy moved after the approval and the
  re-check refused the call; `error` is `approval_superseded`). A denied or expired deferred request
  never runs and raises nothing in the original caller. See
  [Approvals](approvals.md).

```python
import runbound

runbound.init(token="...", control_plane_url="https://plane.example", service="crm")


@runbound.tool(effects={"write"}, approval="required", mode="deferred")
def delete_customer(customer_id: str) -> str:
    return f"deleted {customer_id}"


def describe(result) -> str:
    if isinstance(result, dict) and result.get("status") == "pending_approval":
        return f"waiting for a person: {result['request_id']}"
    return str(result)
```

## Sync and async tools

The same exceptions, at the same point. A sync tool raises from the call; an
`async def` tool raises from the `await`. A sync approval on an async tool waits
in a worker thread (`asyncio.to_thread`), so the event loop keeps running while
a person decides. Cancellation is not a refusal and is re-raised untouched.

Under LangChain the handler lets `ExecutionRefused` out of its callback so the
chain stops; see [LangChain](langchain.md).

## Where a refusal is recorded

Every refusal is also in `runbound.events()` (an anomaly naming the tool and
the rule), `runbound.decisions()` (its Decision) and `runbound.tool_results()`
(outcome `refused`), and, with a plane connected and `export_events` on, in the
`anomalies` lane of `/v1/events` ([Plane link](plane-link.md)). None of them
carries an argument or a result.

---
