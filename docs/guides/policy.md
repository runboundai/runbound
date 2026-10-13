# Per-tool policy

[← Docs](../README.md)

A tool call is checked before its body runs. The rules come from three places,
and every one of them can only take freedom away:

1. **Code**: keywords on `@runbound.tool`, plus `init(tool_policy=...)` for what a
   decorator cannot say.
2. **Capability class rules**: `init(capabilities=...)`, a verdict per class.
3. **The console**: per-tool rows a connected control plane serves. Rows are
   folded with the code rules field by field, and the most restrictive value wins.

Nothing here asks a model. A rule is a number, a name or your own predicate.

## Rules on the decorator

```python
import runbound
from runbound import ToolCall


def under_500(call: ToolCall) -> bool:
    return call.kwargs["amount"] <= 500


def on_call_says_yes(call: ToolCall) -> bool:
    return call.session_key == "user:trusted"


runbound.init()


@runbound.tool(effects={"financial"}, max_calls=1, constraint=under_500)
def issue_refund(*, user: str, amount: float) -> str:
    return f"refunded {user}"


@runbound.tool(effects={"external"}, blocked=True)  # the model may ask; it never runs
def send_email(to: str) -> str:
    return "sent"


@runbound.tool(effects={"financial"}, require_approval=on_call_says_yes)
def wire_money(account: str, amount: float) -> str:
    return "wired"


@runbound.tool(effects={"read"}, reviewed=True)  # looked at, deliberately unrestricted
def lookup_order(order_id: str) -> dict:
    return {"order": order_id}
```

| Keyword | Value | Refuses a call when |
|---|---|---|
| `blocked` | `True` | always. Reported as the `deny` rule. |
| `max_calls` | `int >= 1` | the tool has already been **admitted** `max_calls` times in this session and this call would be one more. The call being judged counts itself, so `1` lets the first call run and refuses the second; a call an earlier check refused does not use one up. |
| `constraint` | `predicate(call) -> bool` | the predicate returns `False` or raises. |
| `require_approval` | `predicate(call) -> bool` | the predicate returns `False` or raises. Each tool has its own. |
| `reviewed` | `True` | never. It says the tool was looked at and is deliberately unrestricted, which satisfies `require_rules`. It is not an allow-list. |
| `effects` | a set of capability classes | never on its own. Class rules and the plane-loss rules read it (below). |
| `approval="required"`, `mode`, `timeout_s` | | a person denies it or does not answer. See [Approvals](approvals.md). |

A predicate receives a `ToolCall`: `name`, `args`, `kwargs`, `session_key` and
`tags`. It sees the arguments as the caller passed them; nothing is rebound to
parameter names, so a keyword-only parameter (`*, amount`) is the reliable way
to find a value in `call.kwargs`. The arguments live only for the duration of
the predicate call and are never stored or logged.

A predicate that raises is a refusal (fail-closed), and only the exception's
type is reported, never its message. A keyword that can never be enforced
(`max_calls=0`, a `constraint` that is not callable, `blocked=True` together
with `reviewed=True`) raises `ValueError` at decoration.

`max_calls` counts admitted calls, not attempts: a call that is refused (by a block, a validator, a
capability class, an approval) or deferred for approval gives its unit back, and calls running at the
same moment each hold one, so they cannot jointly pass the limit. `runbound.tool_calls(key=None)` is
the other tally: it counts every attempt, refused ones included, and reads without making a call.

### `require_rules`: the CI gate

`init(require_rules=True)` makes a tool with no rule a `ValueError`, named at
`init()` for every tool already imported and at decoration for each one after.
A rule is `blocked`, `max_calls`, `constraint`, `require_approval` or
`reviewed=True`. A tool carrying `financial` or `destructive` needs a rule that
can refuse it: `reviewed=True` alone does not pass for those two classes.

## `init(tool_policy=...)`

`ToolPolicy` says what a decorator cannot: an allow-list across all tools, and
rules for tools you did not decorate yourself.

```python
import runbound
from runbound import ToolPolicy

runbound.init(
    tool_policy=ToolPolicy(
        allow=["lookup_order", "issue_refund"],  # only these tools may run
        deny=["wire_money"],
        max_calls={"framework_search": 5},
        require_approval=["delete_account"],
        approval_callback=lambda call: call.session_key == "user:trusted",
        on_violation="block",  # or "block_and_latch", "dry_run"
    ),
)
```

A dict with the same fields is the same policy. It is checked at `init()`: an
unknown key, a tool in both `deny` and `allow`, a `max_calls` that is not an
int of at least 1, or `require_approval` with no `approval_callback` raises
`ValueError`.

```python
import runbound

runbound.init(tool_policy={"deny": ["wire_money"], "max_calls": {"framework_search": 5}})
```

Where a decorator and `tool_policy` both state a rule for the same tool, the
decorator wins and a warning names the tool once. A tool the decorator blocks
is dropped from the `allow` list.

`on_violation` is what a broken code rule does:

- `"block"` (default): refuse the call.
- `"block_and_latch"`: refuse it and stop the session; every later call in that
  session is refused with the same refusal.
- `"dry_run"`: log `Policy dry-run: would block tool ...` and let the call run.

The code rules are checked in a fixed order (`deny`, `allow`, `max_calls`,
`constraint`, then `approval` later in the chain) and the first one broken is
reported. All of them refuse with `PolicyViolation`: `reason="tool_blocked"` for `deny`, `reason="policy"`
for the others (`reason="approval"` for the approval callback).

### An org policy from the plane

A connected plane may also serve a policy body in the same shape. It is merged
with yours, and the merge only removes freedom:

| Field | Merged |
|---|---|
| `deny`, `require_approval` | union |
| `allow` | intersection when both state one, else whichever does |
| `max_calls` | the lower, per tool |
| `constraints`, `approval_callback` | yours only: code never comes over the wire |
| `on_violation` | the stricter (`dry_run` < `block` < `block_and_latch`), unless the org states none or its policy is a dry run |

A refusal by a rule the org added carries `details["origin"] == "org"`. A merge
that cannot be enforced (an org approval rule with no local callback) is logged
and your own policy applies alone.

## Capability classes

`effects=` names the classes a tool carries: `read`, `write`, `external`,
`financial`, `destructive`, `privileged`. A tool with none is unclassified.
`init(capabilities=...)` gives a class a verdict, `allow`, `approve` or `deny`;
a class you leave out allows.

```python
import runbound

runbound.init(capabilities={"destructive": "deny", "financial": "approve"})
```

A tool is refused with `CapabilityRefused` (`reason="capability"`) when any one
of its classes is denied. `approve` also refuses, saying the approval is
missing: a class rule has no approval queue. An unclassified tool is allowed
only while every class allows; as soon as one class is narrowed it is refused,
because nothing said what it does. A capability refusal latches nothing.

`init(max_actions_per_run=n)` caps the tool calls a session may execute; the
next one is refused with `reason="blast_radius"`.

## Console rows

> **Only runbound 1.x enforces console rows.** A worker on 0.x (0.11.2 is what a plain
> `pip install runbound` installs) ignores them and keeps just the rules in your code and the older
> organisation-wide policy. See [Upgrading from 0.11.x](upgrading-from-0-11.md).

A control plane serves one row per tool of a service with `GET /v1/policy`
(`rows` and `rows_hash`; see [Plane link](plane-link.md)). A row is in state
`dry_run` or `enforced`, and it is folded with the tool's code rules **field by
field, the most restrictive wins**. A row can add a block, a cap, an approval or
a quota. It can never loosen what the code states.

| Row field | Folded with code |
|---|---|
| `blocked` | OR with `blocked=True` |
| `max_calls_per_session` | both apply, so the lower holds |
| `approval_required` | OR with code approval |
| `approval_mode` | code wins when it declares a mode; else `sync`. A row never makes a tool deferred: only the tool's own `mode="deferred"` registration does. |
| `approval_timeout_s` | the lower |
| `risk_class` | the stricter by rank `read < write < external < financial < destructive < privileged`. A tool that declares no class stays unclassified (it fails closed) under a `read`, `write` or `external` row; a `financial`, `destructive` or `privileged` row raises it to that class. Either way, `init(capabilities=...)` judges it as the stricter of unclassified and the row's class. A row never makes a tool less strict. |
| `quota` | enforced as below |
| `fail_mode` | used by the quota: `closed` unless the row says `open` |

A row that cannot be read safely is never skipped: its tool is refused with
`reason="policy_unsupported"` until the row is changed or the SDK upgraded (the other tools are not
affected). That covers a field this SDK does not know, a value the console itself would not accept, and
lines it cannot read. The worker tells the plane which tools those are on every heartbeat. The rows a worker holds are replaced
only when `rows_hash` changes; a new `policy_version` alone keeps them. A
policy answer that says the plane could not read its rows (an empty `rows_hash`,
or HTTP 503 `policy_rows_unavailable`) is not an answer: the worker keeps what it
holds, saves nothing to its cache and asks again on the next heartbeat. The
rows and their hash are kept in the [policy cache](plane-loss.md), and rules
declared in code are never written there.

### Decision lines

A row can state its rule as **lines**, each `allow`, `ask` or `deny`, with an optional approval requirement
(an `ask` line) and optional limits. This SDK reports that it reads them (`policy_features` on every
heartbeat), and the plane then serves them beside the fields above. A line may carry a condition
(`when`, an `all of` / `any of` group of tests on the call's arguments, one nested group at most; and
an `unless`), and then it applies only to calls it matches. **The strictest matching line decides**
(`deny`, then `ask`, then `allow`), and **the limits of every matching line apply** (the smallest cap
and the smallest quota; a conditional line's cap counts the calls that matched the same lines). `otherwise`
decides only when a row has no lines. A row stated in the older fields is read as the lines the plane
derives from them, so it decides exactly as before. A line looser than a rule in your code changes
nothing, and a row whose older fields are stricter than its own lines is refused
(`policy_unsupported`, reported as `legacy_view_mismatch`), so a damaged lines payload can never run a
tool that the older view would have stopped.

When the plane tells a worker it cannot enforce a restriction on one tool and has no stricter
fallback, it halts that tool alone (`halted_tools`): the tool is refused with `reason="policy_halted"`
and the rest run, until the plane lifts it. A row may also carry a `candidate`: a dry-run revision served
beside the enforced one. It is read only to record `would_refuse`; it never decides, never takes a
quota unit and never asks anyone.

### Conditions

A test reads one of the tool's arguments, or the trusted context (`environment`, `version`, `service`; set
`RUNBOUND_ENVIRONMENT` and `RUNBOUND_SERVICE_VERSION`). The operators are a closed list, by the argument's type:

| Type | Operators |
|---|---|
| number | `>`, `>=`, `<`, `<=`, `between` (inclusive) |
| string | `equals`, `starts_with`, `ends_with`, `is_one_of` (case-sensitive, exact: no pattern matching, no trimming) |
| boolean | `is_true`, `is_false` |
| list | `contains`, `length_lt`, `length_gt` |
| context | `equals`, `is_one_of` |

A test is **true, false or unknown**. An argument that is missing, `None`, or the wrong type for the operator (a
bool is not a number, a string is not a list) makes the test unknown, never false. A line whose test is unknown
never matches by itself; but if it could have made the decision stricter, the call is refused with
`reason="condition_unevaluable"` rather than let through (unless the decision is already `deny`). The arguments a
test reads are the ones the tool will receive, after your rewrite hooks, by parameter name (a positional argument
too), with the parameter's default when the caller omitted it.

A line may also carry `repetition: {max: n, scope: "session"}`: the call that would be the (n+1)-th with the
same tool and the same arguments in one session is refused with `reason="repetition_exceeded"`. A call refused
for any other reason does not count. In a dry run a candidate rule records `would_refuse:<reason>` (a deny) or
`would_ask:approval` (an ask) and changes nothing.

### `dry_run`, then `enforced`

A `dry_run` row evaluates everything and refuses nothing. When it would have
refused, the call runs and the SDK records `would_refuse`:

- the call's `tool_result` gets the tag `would_refuse:<reason>`, for example
  `would_refuse:tool_blocked` (read it with `runbound.tool_results()`);
- a warning anomaly `Dry run: would refuse tool ...` and its Decision (boundary
  `row`, verdict `deny`) are recorded for **every** call it would have refused, in
  `runbound.events()` and `runbound.decisions()`, up to 100 per session, reason and
  tool; past that they are counted and one summary (`suppressed_count`) is sent when
  the session block exits. A dry run never pages anyone: its reaction stays `dry_run`.

A `dry_run` row never asks a person, and its `risk_class` is not applied. Its
quota is counted (the unit is taken) so the numbers you read are the real ones.

Roll a rule out as `dry_run`, read the `would_refuse` tags, then switch the row
to `enforced`. For code rules the same step is `on_violation="dry_run"`, then
`"block"`.

## Quotas

A row's `quota` is `{"limit": n, "window": "minute" | "hour" | "day", "per":
"service" | "key"}`, counted by the plane in a **fixed, calendar-aligned UTC
window**: "20 per hour" means 20 per clock hour, not 20 in any sliding 60
minutes. The plane keeps one counter, so leases and takes together never exceed
the limit, however many workers run.

- **`per: "service"`.** The hello reply's `quota_leases` grants this worker units
  for the current window; the next hello reports what it still holds
  (`quota_held`). A held unit is spent first, with no network. Everything held is
  dropped when `window_start` changes or the window ends.
- **No lease left.** One call is taken with `POST /v1/quota/take`. A refusal is
  remembered for the `retry_after_s` the plane sends back (the rest of the
  window), so a loop costs one round trip per window.
- **`per: "key"`.** Never leased: every call takes, with the sha256 hash of the
  session key (never the key). A call outside a keyed `runbound.session(...)`
  is not counted against a per-key quota.
- **Over the limit.** `ExecutionRefused`, `reason="quota_exceeded"`, with
  `exc.retry_after` in seconds. It is `retryable`, after `retry_after`:
  retrying before then is refused again.
- **The plane away** (the link is not `connected`, a take fails, or the plane
  answers the take with HTTP 400 or 404). The lease in
  hand keeps being spent. Past it, every class is refused with
  `reason="quota_plane_unavailable"` while `fail_mode` is `closed` (the
  default). With `open` the worker admits `limit // 10` calls (at least 1) per
  `control_plane_poll_s` seconds, because it cannot know how many peers share the
  quota.
- **A take the plane does not recognise** (HTTP 400 or 404, for example a proxy
  that does not route `/v1/quota/*`, or a row that changed under the worker): the
  quota cannot be verified, so the row's `fail_mode` decides as above, and one
  warning a minute names the status.
- **A tool that will ask a person** takes nothing here. `POST /v1/approvals`
  takes the unit before an approval exists; a full quota there is
  `quota_exceeded` too, and the same tool and key is not asked again for the
  `retry_after_s` the plane sends (60 s if it sends none). A deferred request executed later never takes a second unit.

A call refused by an earlier rule consumes no quota and asks no person.

## The order of a call

Every decorated call passes these in order; the first refusal wins and the body
does not run.

| # | Check | Refusal `reason` |
|---|---|---|
| 0 | a session already stopped by `block_and_latch` | the stored refusal again |
| 1 | a fleet halt from the plane | `halt` |
| 2 | the plane gone, or a policy announced and not yet served, for a consequential or unclassified tool | `plane_unavailable` |
| 3 | your `pre_hooks` (may rewrite the arguments) | `hook_refused`, `hook_error` |
| 4 | argument validation against the signature and hints | `invalid_arguments` |
| 5 | code rules: `deny` (`blocked=True`), then `allow`, `max_calls`, `constraint` | `tool_blocked` for `deny`, else `policy` |
| 6 | row: `blocked`, then the session cap | `tool_blocked`, `session_max_calls` |
| 7 | capability class rules, then `max_actions_per_run` | `capability`, `blast_radius` |
| 8 | quota | `quota_exceeded`, `quota_plane_unavailable` |
| 9 | code `require_approval` callback | `approval` |
| 10 | a person (`approval="required"` or a row) | `approval_denied`, `approval_timeout`, `approval_unavailable`, `quota_exceeded` |
| 11 | your `gate_hooks` (refuse only) | `hook_refused`, `hook_error` |

Step 2 uses the tool's classes after a row's `risk_class` is folded in; a row never
opens a tool that fails closed. What
each reason means, and which are retryable, is in
[Handling refusals](handling-refusals.md).

---
