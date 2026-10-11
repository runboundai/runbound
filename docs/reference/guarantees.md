# Guarantees and limits

[← Docs](../README.md)

What the SDK promises about a tool call, and where those promises stop. Each guarantee below has
an exact statement, and the tests that assert it, in [INVARIANTS.md](../../INVARIANTS.md); the
section named in each heading's last line is where to look. This page says what each one means
for you.

Terms: a **consequential** tool carries a `financial`, `destructive` or `privileged` class; an
**unclassified** tool declares no class. A tool with several classes counts as consequential if
any one of them is.

## What is guaranteed

### A refusal happens before the body runs

- Once `init()` has run, a call to a function decorated with `@runbound.tool` passes every check
  before its body starts, sync or async: a fleet halt, plane loss, your pre hooks, argument
  validation, the tool policy, the dashboard's row for the tool, the capability class rules and
  the action cap, the quota, approval, then your gate hooks. The order is fixed, the first refusal
  wins, and nothing after it runs.
- A refusal raises `ExecutionRefused` (`PolicyViolation` and `CapabilityRefused` are subclasses;
  `GuardrailTripped` is the same class under its older name). The body never runs. A deferred
  approval returns `{"status": "pending_approval", "request_id": ...}` instead, and the body does
  not run then either.
- A refused call takes no quota unit and asks no person. It still counts as an attempt in
  `tool_calls()`, so `max_calls` includes it, but never as an executed action.
- `exc.reason` is always one of the fixed set in `runbound.exceptions.REASONS`. `exc.retryable`
  (and `runbound.is_retryable(exc)`) is true only for `plane`, `plane_unavailable`, `quota_exceeded` and `quota_plane_unavailable`.
  `exc.retry_after` gives the seconds to wait for a quota. See
  [Handling refusals](../guides/handling-refusals.md).
- Arguments are validated against the signature and type hints and never coerced. A refusal
  (`invalid_arguments`) names the parameter and the types, never the value.
- A decorated call raises only the tool's own exception (re-raised unchanged) or an
  `ExecutionRefused`. A runbound failure outside the checks (looking up the session, recording an
  event, an observer, your `on_event` callback, the plane client) is logged and the call goes
  ahead. Bad configuration raises `ValueError` at `init()` or at decoration instead.

In INVARIANTS.md: "The host application" and "Refusal happens before the body".

### Failures fail by capability class

When something in the chain itself fails, what happens depends on the tool's classes:

| What failed | `read`, `write`, `external` | consequential or unclassified |
|---|---|---|
| A user pre or gate hook raised, returned nonsense, or outlived `hook_timeout_s` | logged once, skipped, tagged `hook_error:<name>` on the `tool_result` | refused, `reason="hook_error"` |
| A built-in raised (the validator, policy evaluation, a `constraint` or approval callback, the capability rules) | refused | refused |
| The redaction of the result raised | output withheld | output withheld |
| A user post hook raised or timed out | tagged and skipped | output withheld |
| Truncation failed | the plain size cap applies (`truncate_failed`) | the same |
| The injection scan failed | the result passes untagged (`scan_failed`) | the same |

Exception text from a failing hook or built-in reaches neither the model nor the event. The post
chain never passes an unprocessed result on. In INVARIANTS.md: "Failures fail by capability
class".

### The plane can only tighten

- The policy the plane serves is merged with your `init(tool_policy=...)`: bans (`deny`,
  `require_approval`) are unioned, `allow` lists intersected, a per-session `max_calls` becomes the
  lower of the two, and `on_violation` the stricter of the two when the plane states one. The merge never permits a call either side
  refuses. Predicates (`constraints`, `approval_callback`) stay exactly as your code wrote them:
  a policy that arrives over the wire carries no code.
- A dashboard row folds onto the rules on `@runbound.tool`, field by field, most restrictive
  wins: `blocked` OR, the lower session cap, `approval_required` OR, the lower approval timeout,
  the stricter `risk_class` (a row can raise a tool's class, never lower it), `fail_mode` `closed`
  over `open`. A row can add rules; it never loosens one in code.
- A `dry_run` row or policy refuses nothing. It records what it would have refused
  (`would_refuse`) and lets the call run.
- Rules declared in code never come from the policy cache.

See [Action policy](../guides/policy.md). In INVARIANTS.md: "Policy only tightens".

### The seal on a deferred approval

- With `init(approval_secret=...)` (or `RUNBOUND_APPROVAL_SECRET`), a deferred request carries a
  seal: HMAC-SHA256 over the canonical `{v, service, tool, request_id, args, not_after}`. The
  worker that runs the approved request verifies it before any hook runs. A changed field or a
  wrong secret: nothing runs, the plane is told `tampered`, and `on_deferred_result` gets
  `outcome="tampered"`. A valid seal past its `not_after` (with 300 s of tolerance) is
  `clock_skew`, and nothing runs either.
- The secret is never part of the seal and never sent to the plane. The plane carries the
  arguments; it cannot change them unnoticed.
- An approved request runs through the tool's full chain again, so a policy that tightened since
  the approval still refuses it, and a rewrite at that point is a hook error.
- Without a secret, only the worker that asked can run the request, with the arguments it kept.

See [Approvals](../guides/approvals.md). In INVARIANTS.md: "Approvals".

### Plane loss

- The plane-loss gate never refuses a `read`, `write` or `external` tool, in any link state, on a
  cached policy of any age. (A quota or a new approval on such a tool still needs the plane.)
- Consequential and unclassified tools refuse with `reason="plane_unavailable"` once
  `consequential_grace_s` (15 to 60 s, default 60) has passed since the last answered hello, or
  since start with none. Before the first answer they also refuse unless the cached policy was
  confirmed within the last 15 minutes.
- A restarted worker enforces the cached policy before its first hello.
- A fleet halt lapses 60 s after the plane last answered. Because the grace period is at most
  60 s, consequential tools have stopped on their own by the time a halt lapses.
- A process with no plane configured has no plane gate at all.
- The plane is not on the call path: a call to a tool with no approval and no quota row makes no
  network call on the calling thread.

See [Plane loss](../guides/plane-loss.md). In INVARIANTS.md: "Plane loss and the policy cache".

### No raw arguments or results leave the process

- Arguments and results stay in the process, with one exception: an approval request sends the
  call's final arguments to the plane so a person can decide. Everywhere else they travel only as
  digests. An exception's message is not sent to the plane or to the OTLP collector; at most a
  class name is.
- A session key leaves only as an unsalted sha256, unless you set `send_session_keys=True`.
- With no plane and no `otel_endpoint`, the SDK opens no socket.

The full list of what is sent, and when, is on [Privacy](privacy.md). In INVARIANTS.md: "What
leaves the process".

## What is not guaranteed

- **A compromised plane can approve a real pending request.** The seal stops the plane from
  changing the arguments of a deferred request, not from approving it. A compromised plane can
  also halt the fleet or serve rules that refuse more; it cannot make a code rule looser.
- **A timed-out hook's thread cannot be killed.** After `hook_timeout_s` the call moves on, but
  the hook keeps running in its daemon thread. A hook that can hang should bound its own work.
- **An org policy the merge cannot honor is dropped.** For example, an org rule that requires
  approval when your code has no `approval_callback` to ask: the merge fails, one warning is
  logged, and only your local policy applies.
- **The policy cache is tamper-evident, not a boundary.** Its MAC is keyed from the token: a
  hand-edited or truncated file is ignored, but anyone who can read the token can write a valid one.
- **A halt does not outlive the plane.** It lapses 60 s after the plane last answered, so a plane
  that dies releases the fleet. `on_halt="warn"` never refuses at all.
- **Only what passes through runbound is guarded.** A function without `@runbound.tool` (or a
  LangChain tool the handler does not see) is invisible, and the capability class rules apply only
  to decorated tools. `runbound.coverage()` and `runbound.assert_guarded()` show what is covered.
- **Before `init()` a decorated tool is inert.** Its body runs and nothing is checked or recorded.
- **Counters are per process.** `max_calls`, a row's session cap and `max_actions_per_run` count
  the sessions of one worker, so N replicas allow N times as much. A dashboard quota is the one
  limit shared across workers, and it needs the plane.
- **A new thread does not inherit the current session.** Sessions are context variables: a thread
  started inside `runbound.session(...)` lands on the default session unless it enters the block
  itself. `asyncio` tasks do inherit.
- **Validation has edges.** A hint that cannot be checked from JSON (`datetime`, `UUID`, a type
  variable) is skipped with one warning; `*args` and `**kwargs` are never checked, and defaults are
  not validated.
- **The injection scan only tags.** It never blocks a result. Redaction matches dict keys only;
  plain strings are not redacted.
- **Without an approval secret, a deferred request lives in one worker's memory.** If that worker
  restarts, no other worker can run it.
- **A cached policy is enforced at any age.** After 24 h without the plane, `plane_status().stale`
  says so, but `read`, `write` and `external` tools keep running on it.
- Python 3.10 or newer.

---
