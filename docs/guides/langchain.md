# LangChain / LangGraph

[← Docs](../README.md)

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
