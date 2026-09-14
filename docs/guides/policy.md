# Action policy — rules for what your agent may do

[← Docs](../README.md)

Detectors watch how *much* an agent is doing. A policy states what it is
*allowed* to do — and once an agent can send email, issue refunds and move
money, that is where the liability sits. Monitoring can tell you afterwards
which refund went out; only something sitting on the tool call can refuse it.
`@runbound.tool` already fires before the function body runs, so a policy is
evaluated there: **we enforce the rules you state, we never judge the action,
and no model is involved in the decision.**

## The rule lives on the tool

No policy file, no CLI, no second place to look. A tool's rule is a keyword on
its decorator, so it sits in the same line — and lands in the same diff, and
gets the same review — as the function it governs:

```python
import runbound

runbound.init(on_anomaly="raise")             # governs detectors; policy is separate

@runbound.tool(max_calls=1, constraint=under_500)
def issue_refund(user: str, amount: float): ...

@runbound.tool(blocked=True)                  # the model may ask for it; it never runs
def send_email(to: str): ...

@runbound.tool(require_approval=ask_a_human)
def wire_money(account: str, amount: float): ...

@runbound.tool
def lookup_order(order_id: str): ...          # known, allowed, no rule
```

| Keyword | You give | A call is refused when |
|---|---|---|
| `blocked` | `True` | ever. The blunt one: this agent may never do this. Reported as the `deny` rule. |
| `max_calls` | `int >= 1` | the tool has been attempted more times than the limit **in this session**. The count includes the attempt being judged, so `1` means the first call runs and the second is refused. |
| `constraint` | `predicate(call) -> bool` | your predicate returns `False`. It receives a `ToolCall` — `name`, `args`, `kwargs`, `session_key`, `tags` — so it can decide on the real arguments, or on who is calling ("free-tier users may not do this"). |
| `require_approval` | `predicate(call) -> bool` | your callback returns `False`. Same `ToolCall`. Each tool gets **its own** callback: two tools can ask two different people. |
| `allow` | `True` | never. It states that this tool was reviewed and is deliberately unrestricted, so it satisfies `require_rules` below. It is **not** an allow-list and it restricts nothing. |

The rules take effect the moment the decorator runs, which is normally *after*
`init()` — your module configures runbound at the top and defines its tools
below. A keyword that could never be enforced (`max_calls=0`, a `constraint`
that is not callable) raises `ValueError` right there at decoration, the same
way a bad `init()` argument does.

## The CI gate: `require_rules=True`

```python
runbound.init(require_rules=True)
```

Now a `@runbound.tool` that states no rule at all is a `ValueError` — named at
`init()` for every tool already imported, and raised at decoration for every
one declared afterwards. Any CI step that imports your app fails with it, so a
tool cannot reach production without a stated rule. A tool that genuinely needs
none says so out loud with `reviewed=True`.

## `tool_policy=` on `init()`: the fleet allow-list, and tools with no decorator

Two things a decorator cannot say, so this stays:

- **An allow-list across all tools.** `ToolPolicy.allow` is the one rule that
  *inverts*: set it and the listed tools are the only ones permitted, which is
  a statement about the whole process rather than about any one tool.
- **A rule for a tool you did not write.** A framework's tools (LangChain's,
  say) carry no decorator of yours; name them here.

```python
from runbound import ToolPolicy

runbound.init(
    tool_policy=ToolPolicy(
        allow=["lookup_order", "issue_refund"],   # ONLY these tools may run
        deny=["wire_money"],
        max_calls={"framework_search": 5},
        require_approval=["delete_account"],
        approval_callback=lambda call: ask_a_human(call.name),
        on_violation="block",                     # "block_and_latch" | "dry_run"
    ),
)
```

A plain dict of the same fields is identical — useful when the policy comes out
of your own config file. It is coerced and validated at `init()`, and an unknown
key raises there rather than silently dropping a rule you believed was enforced:

```python
runbound.init(tool_policy={"deny": ["wire_money"], "max_calls": {"framework_search": 5}})
```

`on_violation` belongs here and only here: it is what a *broken rule* does, a
property of the policy rather than of any one tool.

**Where both name the same tool, the decorator wins** — the rule that ships in
the same diff as the function is the one that was reviewed with it — and a
warning says so once, naming the tool. A tool the decorator blocks is dropped
from an `allow` list here, exactly the way an org policy's deny resolves the
same disagreement.

The five rules are evaluated in a fixed order — `deny`, `allow`, `max_calls`,
`constraint`, `approval` — and the first one broken is the one reported, so an
explicitly blocked tool reads as "denied" however many other rules also cover
it.

**Catch a refusal apart from a runaway.** `PolicyViolation` is a subclass of
`GuardrailTripped`, so code that already catches trips keeps working; catch it
first when you want to tell "the agent tried something it may not do" from "the
agent ran away":

```python
try:
    run_agent()
except PolicyViolation as exc:
    # exc.violation.rule is "deny" | "allow" | "max_calls" | "constraint" | "approval"
    log.warning("refused %s (%s): %s", exc.violation.tool, exc.violation.rule, exc)
    ask_the_user_what_to_do()
except GuardrailTripped as exc:
    shut_down_cleanly(exc.anomaly)
```

`ToolPolicy`, `ToolCall`, `Violation` and `PolicyViolation` are all exported from
the package root. `runbound.tool_calls(key=None)` reads the tally `max_calls`
is measured against, without provoking a violation:

```python
runbound.tool_calls("user:8842")
# {'send_email': 2, 'issue_refund': 2, 'wire_money': 1}
```

Those are **attempts, not successes** — a call the policy refused is counted,
because it was made. The attempt is recorded before the policy judges it, so a
refused call still counts towards the loop window and the step count too, and an
agent that is both running away and misbehaving can be stopped by a detector
before the policy ever sees the call. That is the right order: the session is
already over.

**Roll it out with `dry_run`.** Nobody's first draft of a policy is right, and a
wrong rule in `block` mode breaks a working agent. Ship with
`on_violation="dry_run"` for a week: every violation is logged
(`[runbound] Policy dry-run: would block tool 'send_email' ...`) and alerted,
and every tool still runs. Read what it would have refused, fix the rules that
were wrong, then change one word to `"block"`.

Four honest notes:

- **An approval nobody can answer is a refusal.** Each tool's own
  `require_approval` callback is asked first; a tool that has none falls
  through to the `approval_callback` you configured on `init()`; if there is
  neither, the call is refused, not permitted. Fail-**closed**, like every
  other gate here.
- **A gate that errors refuses the call.** Everywhere else in runbound a bug
  is swallowed so your call proceeds. A constraint or approval callback is the
  one exception: it is a permission gate you opted into, and a gate that cannot
  answer must not wave the call through. A predicate that raises therefore
  counts as a violation — fail-**closed** — exactly as if it had returned
  `False`. Only the exception's *type* reaches the anomaly (`error: 'KeyError'`),
  never its message, which usually quotes the argument it choked on.
- **The real arguments go to your predicate and nowhere else.** `ToolCall.args`
  and `.kwargs` live for the duration of your call and are never stored,
  logged, or put in an anomaly or an alert — only the salted digest the loop
  detector compares is kept. A violation names the tool, the rule
  and the numbers behind it — never a refund amount or an email address.
- **Approval callbacks are synchronous by contract.** runbound calls yours on
  the agent's own thread and waits, which is what makes the refusal arrive
  before the tool runs. In an async app a callback that blocks on a human blocks
  the event loop: return a decision you already have (a cached approval, a
  queue, a flag) rather than waiting inside it.

Two boundaries worth stating plainly:

- **`max_calls` is per session.** Inside `runbound.session(key)` each key gets
  its own tally; outside one, everything counts against the default session
  `init()` created. Both are per process.
- **Under LangChain, constraints see the input string.** The handler enforces
  the policy in `on_tool_start`, where LangChain hands us the tool's name and a
  single `input_str` — so `call.args == (input_str,)` and `call.kwargs == {}`.
  Deny, allow, `max_calls` and approval work exactly as they do elsewhere;
  a constraint that wants typed arguments needs the `@runbound.tool`
  decorator on the function itself.

The whole thing, offline, in three acts —
dry-run, block, and a denied tool that latches the session:
[`examples/policy_demo.py`](../../examples/policy_demo.py).

## An org policy merged with yours: the algebra

In [fleet mode](fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane)
the plane states one policy per `service`, and the SDK merges it with the
`tool_policy` you wrote locally. The merge can only ever *remove* freedom,
and it does so one rule at a time:

| Rule | Merged how | Why that one |
|---|---|---|
| `deny` | **Union** | A ban either side states is a ban. A tool the org denies is also dropped from your `allow` list, so the two can never disagree about the same tool. |
| `allow` | **Intersection** | Only tools both sides permit survive. A side that states no allow-list is not restricting anything, so the other side's list carries unchanged — intersecting with "everything". |
| `max_calls` | **The lower of the two**, over every tool either side limits | A limit is a ceiling; the lower ceiling is the one that holds. The tally it is counted against stays local (one process, one session). |
| `constraints`, `approval_callback` | **Local only** | A policy that arrived over the wire cannot carry code, and runbound will not run someone else's predicate in your process. Your callables are exactly as you wrote them. |
| `require_approval` | **Union** | A tool either side wants approved is approved — by *your* callback, since the org's rule brings no code with it. If there is no local callback to ask, the merge raises `ValueError` at configuration time rather than build a gate nobody can answer. |
| `on_violation` | **The stricter of the two** (`dry_run` < `block` < `block_and_latch`) | Propagation may tighten a reaction, never loosen one. Unless the org states no mode at all, or its policy is being rolled out `dry_run` — in both cases yours stands unchanged. |

A violation of a rule the org contributed reports `details["origin"] == "org"`,
and `details["dry_run"] is True` while that policy is still being rolled out
dry, so a log line tells you whose rule stopped the call.

The one property underneath all six rows: **the merged policy never permits a
call that either input would have refused.** `tests/test_policy_merge.py`
pins each row by hand; `tests/test_policy_merge_properties.py` asserts the
property itself over 500 randomly generated policy pairs (stdlib `random`, a
fixed seed, 11,900 probes) so the combinations nobody thought to write down
are covered too.

---
