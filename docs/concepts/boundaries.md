# Boundaries

[← Docs](../README.md)

runbound is a boundary in front of tool calls. It decides whether one call of one decorated tool
may run, with these arguments, now; it shapes what the call returns; and it records what happened.
Everything below is outside that boundary on purpose. Each item says what to use instead.

## It does not read prompts or replies

There are no provider wrappers. runbound never sees a model request, a completion, a token count
or a price, and the LangChain handler listens to tool callbacks only. What a model is told and
what it answers is out of scope.

Instead: put prompt and reply controls where the model is called (your provider's own settings,
or a gateway in front of it). runbound sees the consequence: the tool call the model asks for.

## It does not watch call patterns

The SDK keeps counts, not models of behaviour. There is no detection of loops, repeated calls,
bursts or error storms, and no spend or token limit. The counts it does enforce are ones you
state: `max_calls` per tool per session, the row's `max_calls_per_session`, `max_actions_per_run`
per session, and a row's `quota` per fixed window.

Instead: cap what matters with those counts, and analyse the spans (one per tool call, see
[OpenTelemetry](../guides/opentelemetry.md)) in your tracing backend. Spend limits belong with
your model provider.

## It guards only what is decorated

A function without `@runbound.tool` is invisible to it: called directly, through another name or
from a code path the decorator does not cover, it runs unchecked and unrecorded. A decorated
function called before `runbound.init()` also runs straight through. A framework tool is seen only
while the [LangChain handler](../guides/langchain.md) is attached. It declares no capability
class, so class rules never refuse it and it counts as unclassified when the plane is away, and
its output cannot be redacted or truncated: a callback cannot replace a tool's output.

To find what is not guarded:

```python
import runbound

report = runbound.coverage()
print(report["decorated_tools"], report["decorated_tool_names"], report["tools_by_class"])

seen_but_undecorated = [t["name"] for t in runbound.tools() if not t["decorated"]]

runbound.check()          # prints the tools, their classes, the plane link and the last events
runbound.assert_guarded()  # RuntimeError when nothing is decorated
```

From a shell, `python -m runbound check agent.py` loads the file (its `__main__` block does not
run) and prints the same report; it exits 0 when at least one tool is decorated and 1 when none
is. `init(require_rules=True)` makes every decorated tool state a rule or `reviewed=True`, and a
tool carrying a consequential class state a rule that can refuse it; otherwise `init()` (for tools
already imported) or the decorator raises `ValueError`. None of
these can list a function the SDK never saw: an undecorated function is not in any report.

## It does not sandbox the body

Once a call is admitted, the body runs in your process with your process's permissions,
credentials and network. runbound does not limit how long it runs, what it touches, or what it
does with its arguments. A hook that times out is abandoned, not killed: its thread keeps running.

Instead: give the tool's credentials only what the tool needs, and run a body that must be
contained in a container or a separate process. Use `constraint=` and hooks to keep the arguments
inside the range the body can safely handle.

## The scan tags, it does not block

The prompt-injection scan in the post chain sets `flags`, `flag_score` and `flagged` on the
`tool_result` record and on the span. It never refuses or changes the result by itself.

Instead: a post hook that returns `hooks.withhold(reason)` when the scan or your own check says
so. Redaction covers values of secret-named dict keys only; plain strings are not searched.

## A compromised plane can approve

The plane can add restrictions (rows, an org policy merged in, a halt) and it can never loosen a
rule written in code. Approvals are different: the plane is where a person says yes, so a plane
that is compromised can approve a real pending request. With `approval_secret` set, the seal
stops it from changing a deferred request's tool or arguments, or having it run past its expiry: a
worker verifies the seal before any hook runs, and on a mismatch nothing runs and the plane is
told `tampered`. The secret is never sent to the plane.

Instead: for an action no one system should be able to approve alone, keep a code rule that
refuses it (`constraint=`, `max_calls=`, `blocked=True`) or a gate hook that checks a second
source; neither can be loosened from the plane.

## Related

- [How it works](how-it-works.md): the chain, slot by slot.
- [Free SDK, connected plane](free-and-connected.md): what needs an account.
- [Guarantees and limitations](../reference/guarantees.md).
