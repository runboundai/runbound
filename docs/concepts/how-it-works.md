# How it works

[← Docs](../README.md)

`@runbound.tool` wraps a function. Every call of it goes through one fixed chain of checks
before the body runs, and through a post chain after the body returns. Nothing else in your
process is touched: runbound sees the calls of decorated functions (and of LangChain tools the
[callback handler](../guides/langchain.md) is attached to), and nothing more. Before
`runbound.init()` has run, a decorated function calls straight through.

The chain is `Engine.admit_call` in `runbound/engine.py`; the post chain is
`hooks.run_post` in `runbound/hooks.py`.

## The chain, in order

```
 call issue_refund(order="A-1", amount=40)
   |
   |  tool_call event recorded (it counts toward max_calls whether or not it is refused);
   |  a session stopped by a block_and_latch policy is refused here
   v
 1  halt                     plane halted the fleet; on_halt="raise" refuses   -> halt
 2  plane gate               plane gone, consequential or unclassified tool    -> plane_unavailable
 3  user pre hooks           init(pre_hooks=) then @tool(pre_hooks=); may rewrite the arguments
 4  argument validation      final arguments vs signature and type hints       -> invalid_arguments
 5  policy rules             deny, allow, max_calls, constraint                -> policy
 6  console row              blocked, then the per-session cap                 -> tool_blocked, session_max_calls
 7  capability + action cap  init(capabilities=), max_actions_per_run          -> capability, blast_radius
 8  approval spec fold       code's approval= and the row's approval_required, most restrictive
 9  quota                    the row's quota (not taken when a person will be asked)
                                                                               -> quota_exceeded, quota_plane_unavailable
10  approval rule            require_approval=<predicate> returned False       -> approval
11  approval                 a person, through the plane (sync waits; deferred returns at once)
                                                                               -> approval_denied, approval_timeout,
                                                                                  approval_unavailable, quota_exceeded
12  user gate hooks          init(gate_hooks=) then @tool(gate_hooks=); refuse only
                                                                               -> hook_refused, hook_error
   |
   v
    body
   |
   v
    post chain               redact -> truncate -> scan -> user post hooks
   |
   v
    tool_result event + span end          outcome: ok | error | refused | withheld | pending_approval
```

The right-hand column is `exc.reason` on the `runbound.ExecutionRefused` the caller gets. The
first refusal wins and ends the call: nothing after it runs, the body never runs, and the
`tool_result` event records `outcome="refused"`. See
[Handling refusals](../guides/handling-refusals.md) for every reason.

What each slot does:

1. **Halt.** A fleet halt from the plane. With `on_halt="raise"` (the default) every tool is
   refused; with `on_halt="warn"` the call goes on and a warning is logged at most once a minute.
2. **Plane gate.** Only with a plane configured. Once the plane has not answered for
   `consequential_grace_s` (default 60), a tool carrying `financial`, `destructive` or
   `privileged`, or no class at all, is refused (`plane_unavailable`, retryable); while the link
   is still connecting, such a tool is refused unless the on-disk policy cache was confirmed in the
   last 15 minutes. `read`, `write` and `external` tools keep running.
   See [Plane loss](../guides/plane-loss.md).
3. **User pre hooks.** The only slot where the arguments can change (`hooks.rewrite(...)`).
   Everything after it sees the final arguments.
4. **Argument validation.** Checked against the function's signature and type hints. Nothing is
   coerced: `"40"` for a `float` is refused, and so is `True` for an `int`.
5. **Policy rules.** The rules on the decorator (`blocked=`, `max_calls=`, `constraint=`) folded
   over `init(tool_policy=...)`; a decorator's rule wins where both state one. With a plane, the
   org policy it serves is merged in, and the merge can only remove freedom.
6. **Console row.** The plane's per-tool row, if one exists for this tool: `blocked`, then
   `max_calls_per_session`. A row in `dry_run` state refuses nothing and records `would_refuse`.
7. **Capability class rules and the action cap.** `init(capabilities={"destructive": "deny"})`
   refuses a decorated tool carrying a denied class (`runbound.CapabilityRefused`). A row's
   `risk_class` can raise a tool's class, never lower it. `max_actions_per_run` caps executed
   calls per session.
8. **Approval spec fold.** Whether this call needs a person: the decorator's
   `approval="required"` or the row's `approval_required`, either one. The code's mode wins when it
   states one; the lower timeout wins.
9. **Quota.** The row's quota, in a fixed UTC window. A call that will ask a person takes nothing
   here (the plane takes the unit when the approval is created), and an approved deferred call has
   its unit already. A call refused at an earlier slot consumes no quota; one refused at a later
   slot has spent its unit.
10. **Approval rule.** The `require_approval=` predicate on the decorator (or the policy's
    `require_approval` list with its `approval_callback`). It runs in your process; `False`, or an
    exception, refuses.
11. **Approval.** A request to the plane, built from the final arguments. `mode="sync"` waits up
    to `timeout_s`; `mode="deferred"` returns `{"status": "pending_approval", "request_id": ...}`
    at once and the body runs later, on a worker of the service, once a person approves. With no
    plane connected the call is refused at once (`approval_unavailable`).
    See [Approvals](../guides/approvals.md).
12. **User gate hooks.** Your last word: `hooks.allow()` or `hooks.refuse(reason)`. A gate hook
    cannot rewrite. Built-ins run before it, so a gate hook cannot soften a refusal.

A user pre or gate hook that raises or runs past `hook_timeout_s` (default 5) refuses the call
(`hook_error`) when the tool carries `financial`, `destructive` or `privileged` or no class; for
a `read`, `write` or `external` tool it is logged once and skipped. A timed-out hook's thread
cannot be killed and keeps running. A built-in that raises (the validator, the policy, the
capability rules) refuses every class.

### After the body

What the body returns passes four steps before the caller sees it:

1. **redact**: values of secret-named dict keys become `"[REDACTED]"`; `init(redact_keys=[...])`
   adds names. Plain strings are not searched.
2. **truncate**: strings, lists, dicts and nesting are cut with a marker, and the serialized size
   is capped (16,000 characters by default; `init(result_limits=...)`,
   `@tool(max_result_chars=)`).
3. **scan**: prompt-injection patterns. A hit tags the result (`flags`, `flag_score`, `flagged`);
   it never blocks.
4. **user post hooks**: `hooks.replace(result)` or `hooks.withhold(reason)`.

A redaction that fails withholds the output for every tool. A user post hook that raises or times
out withholds the output for a tool that carries a consequential class or none, and is skipped
for `read`, `write` and `external`. Exception text never reaches the model. A body that raises is recorded (`outcome="error"`) and its exception is re-raised unchanged.

Every call ends in one `tool_result` record (`runbound.tool_results()`, and `init(on_event=)`):
the tool, the outcome, `duration_s`, the scan's flags, whether it was truncated, sizes and tags.
It never carries the arguments or the result. If a span receiver is configured (an OpenTelemetry
provider, or `init(otel_endpoint=...)`), the call is one `execute_tool {tool}` span, started
before slot 1 and ended here. See [OpenTelemetry](../guides/opentelemetry.md).

### A deferred approval, when it runs

When a person approves a deferred request, the plane offers it to the service's workers on their
next heartbeat. A worker verifies the seal **before any hook runs**: an HMAC over the service,
tool, request id, arguments and expiry, keyed with `approval_secret` (with no secret, nothing is
sealed and only the worker that asked can run the request, from its memory). Then it runs the call
through the whole chain again, with three differences: a pre hook may not change the arguments (a
rewrite that would is a hook error), the quota is not taken again, and no person is asked again.
`runbound.current_request()` returns the request id while it runs, and `init(on_deferred_result=)`
is called with the outcome.

## Sessions and keys

Counters live in a session. `init()` starts one default session for the process; `reset()`
starts a fresh one and forgets every keyed session.

```python
import runbound

runbound.init()


@runbound.tool(max_calls=3)
def search(query: str) -> list:
    return []


with runbound.session("user-8842", tags={"plan": "free"}):
    search("refund policy")  # counted under user-8842's session
```

A key names one session, reused by every block with that key. `max_calls`, the row's
`max_calls_per_session` and `max_actions_per_run` all count per session, and so does
`runbound.tool_calls(key)`. A refused attempt still counts. The binding is a context variable: a
thread or asyncio task that opens its own block is unaffected by others, and blocks nest. At most
`max_sessions` keys (default 10,000) are kept, least recently used out.

A constraint or a hook sees the key as `call.session_key` and the tags as `call.tags`, so a rule
can depend on who is calling. What leaves the process is a sha256 of the key, unless
`init(send_session_keys=True)`; see [Privacy](../reference/privacy.md).

## Local, and what needs a plane

Slots 3, 4, 5, 7, 10 and 12, the post chain, the `tool_result` records and the spans all run in
your process with no account. The halt, the plane gate, console rows, quotas and approvals come
from a connected plane: with none configured, slots 1, 2, 6 and 9 pass every call, and slot 11
refuses at once. [Free SDK, connected plane](free-and-connected.md) lists both sides.
