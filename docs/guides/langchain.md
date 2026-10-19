# LangChain / LangGraph

[← Docs](../README.md)

## The LangChain / LangGraph reference integration (opt-in)

Runbound supports an opt-in LangChain / LangGraph reference integration for locally executed tools through `RunboundMiddleware` (`wrap_tool_call` / `awrap_tool_call`) and `govern_tools`. It provides tool-boundary policy enforcement, explicitly configured approval handling and correlated action records. Support is limited to the tested combinations, execution layouts and approval configurations documented below; installing the extra does not enable governance automatically. This is an alpha release, not a compatibility claim for every version in the dependency range.

### Tested combinations

| Python | langchain | langgraph |
|---|---|---|
| 3.11.17 | 1.2.11 | 1.2.0 |
| 3.13.16 | 1.3.0 | 1.2.0 |
| 3.13.16 | 1.4.6 | 1.2.16 |
| 3.12.15 | 1.4.6 | 1.2.16 |

All four rows ran on macOS arm64 with langchain-core 1.6.12, langgraph-prebuilt 1.1.1, langgraph-checkpoint 4.3.0, langgraph-checkpoint-sqlite 3.1.2 and pydantic 2.14.1: 111 tests per row (the harness, the middleware and `govern_tools` layouts, the approval wait and the suspended path against a real control plane). These four rows are not four full reruns of every scenario: the process-restart and race checks ran at one pinned point (Python 3.13.14, langchain 1.4.6, langgraph 1.2.16).

Linux, Windows, versions between these rows and other checkpointers are **untested**; the ranges the installer accepts do not make them supported. A free resolver may choose versions outside this table, so to reproduce a tested combination install its pins:

```bash
uv venv --python 3.13
uv pip install "runbound[langchain]" "langchain==1.4.6" "langchain-core==1.6.12" "langgraph==1.2.16" \
    "langgraph-prebuilt==1.1.1" "langgraph-checkpoint==4.3.0" "langgraph-checkpoint-sqlite==3.1.2" "pydantic==2.14.1"
```

### Using it

Enable it deliberately, in code or in the environment (it is off until you do). A complete agent, with plain LangChain tools (the middleware is the boundary, so do not also decorate them with `@runbound.tool`):

```python
import runbound
from langchain.agents import create_agent
from langchain_core.tools import tool
from runbound.integrations import langchain_early
from runbound.integrations.langchain_early import RunboundMiddleware

runbound.init(
    token="rb_...", control_plane_url="https://plane.example", service="support",
    otel_endpoint="https://plane.example",   # self-hosted: the plane's own address takes the spans; without it the console's Traces stay empty
)
langchain_early.enable()                      # or RUNBOUND_LANGCHAIN_EARLY=1

@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"

@tool
def issue_refund(order_id: str, amount_cents: int) -> str:
    """Refund an order."""
    return f"refunded {amount_cents} on {order_id}"

agent = create_agent(
    model, [lookup_order, issue_refund],
    middleware=[RunboundMiddleware(effects={"lookup_order": {"read"}, "issue_refund": {"financial"}})],
)
agent.invoke({"messages": [("user", "refund order A1002")]})   # a refused call comes back to the model as an error ToolMessage; invoke does not raise
```

For a bare `ToolNode`, use `govern_tools(tools, effects={...})` in its place. Classify every tool with `effects=`: a tool you leave out is judged as unclassified.

**What reaches the model, and what raises.** With the middleware (or `govern_tools`) on the tool node, a refusal reaches the model as an error `ToolMessage` (`refused <reason>`) and the graph carries on. That holds whether the middleware itself decides the call or the tool is a `@runbound.tool` function wrapped by `tool(...)`: the inner decorator still decides (nothing is loosened or re-decided), and the middleware only presents its refusal the same way. A callback handler is different: it governs from `on_tool_start`, and its refusal is *raised* out of `invoke` as it always was (a conversation turn is lost unless you catch it).

| What is on the tool | Who decides | A refusal |
|---|---|---|
| a plain LangChain tool + the middleware / `govern_tools` | the middleware | an error `ToolMessage`, the turn continues |
| a `@runbound.tool` function wrapped by `tool(...)` + the middleware / `govern_tools` | the decorator | the same error `ToolMessage`, the turn continues |
| a `@runbound.tool` function wrapped by `tool(...)`, **without** the middleware | the decorator | `ExecutionRefused` raised out of `invoke` |
| `RunboundCallbackHandler` attached, with or without the middleware | the handler | `PolicyViolation` raised from `on_tool_start`, out of `invoke` |

So to get refusals as tool messages, put the middleware on the tool node and leave the callback handler off.

- **Layouts.** `create_agent` with the middleware, and governed tools in the documented `ToolNode` layout. For resumable execution: one tool call per task with stable identities. Two interrupts, or parallel resumable calls, inside one task are excluded (independent calls are not the same thing as several interrupts inside one task).
- **Approvals are a separate opt-in.** With neither approval mode enabled, a call that needs a person is refused `approval_unavailable`; nothing waits. `langchain_early.enable(approval_wait=True)` (or `RUNBOUND_LANGCHAIN_APPROVAL_WAIT=1`) lets a fresh call wait for the decision. In a chat agent that holds the conversation turn until the person answers (or the request times out); the `mode="deferred"` of the decorator instead hands the model a `pending_approval` result and delivers the outcome to `on_deferred_result`, outside the conversation.
- **Suspended and resume: experimental in this release.** It needs `init(approval_bind=True)`, `langchain_resume.enable()` (or `RUNBOUND_LANGCHAIN_RESUME=1`) and a checkpointer, and it was tested with the SQLite checkpointer only. How long an approved request stays resumable is a setting of the control plane's deployment (`RUNBOUND_SUSPENDED_RESUME_S`), not something the SDK or the console selects, and the seven-day default it falls back to has not been ratified. Until that bound can be set explicitly where you configure the service, treat suspended mode as experimental. Fresh-call governance and approval waits are not affected.
- **Outside this support:** provider-hosted tools, custom nodes that call tools themselves, unwrapped native tool paths, and the middleware-to-MCP combined path. The legacy admission API does not gain console-rule parity.
- **Refusals** are error `ToolMessage`s with the reason code, not exceptions, and the graph carries on. (The callback handler below raises.)
- **What it does not claim.** No exactly-once effects, and no independently verified external outcome: an execution bind is not a start marker and is not evidence the tool finished. Redaction is by key, not by scanning free text. Records from SDKs up to 1.0.0a4 can lack an action identity or start evidence, and those SDKs emit no span for a cancelled call.

### Moving from the callback handler

Nothing is migrated for you. To switch, add the middleware (or `govern_tools`) for your layout, keep the handler while you compare decisions on the same fixtures, then **remove the old handler yourself**. A refusal becomes an error `ToolMessage` instead of the handler's exception, and a tool you did not classify (`effects=`) is judged as unclassified, which the handler never did.

Installing both is detected, so a call is admitted once and uses one unit of a quota. But the boundary closest to the tool governs: while the old handler is attached to the call or the tool it is authoritative and **keeps its weaker checks** (no argument validation, no class rules for an undeclared tool). Having both installed is not evidence that the new middleware's guarantees apply; remove the handler to get them.

## The callback handler

When the framework makes the calls, there is no function to decorate. Pass a
callback handler instead:

```bash
pip install "runbound[langchain]"   # pulls in langchain-core
```

```python
import runbound
from runbound.integrations.langchain import RunboundCallbackHandler

runbound.init(tool_policy=runbound.ToolPolicy(max_calls={"lookup_order": 2}))

agent.invoke(
    {"input": "look up my order"},
    config={"callbacks": [RunboundCallbackHandler()]},
)
```

The handler checks a tool call on `on_tool_start`, **before** the tool runs,
against the same policy and action cap a decorated tool meets. It also records the
call's outcome on `on_tool_end` and `on_tool_error` (a `tool_result` event, and one
`execute_tool {tool}` span, see [OpenTelemetry](opentelemetry.md)). It holds no
state of its own: several handlers across several chains share one session.

What it cannot do, because LangChain's callbacks do not allow it: replace or
rewrite the tool's output, and run argument validation or hooks. A tool the
framework calls that no `@runbound.tool` declares has no capability class, so
class rules do not refuse it here. Decorate the underlying function when you need
the whole chain.

It sets `raise_error = True` so LangChain lets the refusal out of the callback and
the chain actually stops. Every other exception inside the handler is caught and
logged, so that setting cannot turn a runbound bug into a broken chain.

`langchain_core` is imported lazily, on first access to `RunboundCallbackHandler`,
so `import runbound` still works without it installed.
