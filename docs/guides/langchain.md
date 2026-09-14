# LangChain / LangGraph

[← Docs](../README.md)

When the framework makes the calls, there is no client to `wrap()` and no
function to decorate. Pass a callback handler instead:

```bash
pip install "runbound[langchain]"   # pulls in langchain-core
```

```python
import runbound
from runbound.integrations.langchain import GuardrailCallbackHandler

runbound.init(budget_usd=5.00, loop_threshold=3, on_anomaly="raise")

agent.invoke(
    {"input": "research the market"},
    config={"callbacks": [GuardrailCallbackHandler()]},
)
```

The handler records a tool call on `on_tool_start` — **before** the tool runs,
so a loop is broken on the repeat that would have made it — and a model call on
`on_llm_end`, priced from whatever usage the result carries. It holds no state
of its own: several handlers across several chains share one session, one budget
and one step count.

It sets `raise_error = True` so LangChain lets `GuardrailTripped` out of the
callback and the chain actually stops. Every other exception inside the handler
is caught and logged, so that setting cannot turn a runbound bug into a
broken chain.

`langchain_core` is imported lazily, on first access to
`GuardrailCallbackHandler`, so `import runbound` still works without it
installed.

Two caveats. LangChain reports usage on `on_llm_end` only, so a streamed
response whose provider omits usage is recorded as a zero-token step. And the
handler records no call duration, so
[spike detection](spike-detection.md#spike-detection-zero-config) on a LangChain-driven agent
works from output tokens alone — the duration baseline never arms, and
`max_call_seconds` has nothing to measure. Use `wrap()` on the client where you
want the timing signal.

---
