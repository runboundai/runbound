# How it works

[← Docs](../README.md)

Your agent keeps calling its LLM client and its tools exactly as it does today.
`runbound.wrap(client)` is [the canonical mechanism](what-it-sees.md#wrap-is-canonical-auto_wrap-is-a-convenience):
it patches one client's `create` method in place, works on any OpenAI- or
Anthropic-shaped client, and is what everything else builds on.
`runbound.init()` with `auto_wrap` (the default) is the convenience: it
calls `wrap()` for you on the OpenAI and Anthropic SDK classes themselves, so
a client built after it is guarded already with no explicit call. `@runbound.tool`
decorates your functions. Every model call and tool call that passes through
one of those emits a small immutable event into an in-process session — one per process by
default, or one per session key inside a `runbound.session()` block. A model
call that *fails* emits one too (`llm_error`), and so does every tool call the
model **asks** for in its answer (`tool_request`), before your code dispatches
it. Eight detectors read that session after every event; the first anomaly
triggers your configured reaction (log, raise, or your own callback) and is
reported to the control plane, if one is configured. A guarded tool call is also checked against your
[action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do), if you set
one, before the function body runs. Failed calls are counted a second time
against [the provider's own circuit](../reference/circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker),
which is process-wide rather than per session.

```
   your agent
       |
       |  client.chat.completions.create(...)      @runbound.tool
       v                                                  |
  wrapped client  --------> Event(step, tokens, cost, duration) <----+
       |                    llm_call | llm_error | tool_request
       |                    tool_call | tool_error
       |                              |
       |                              v
       |            SessionState (counters, sliding windows, call baseline)
       |                one per process, or one per session key
       |                              |
       |                              v
       |    loop | budget | velocity | steps | events | spike | error_storm | timeout
       |                    (pure functions, no LLM)
       |                              |
       |                       Anomaly detected
       |                         /          \
       |      warn | raise GuardrailTripped | callback    reported to the
       |      (logs) (stops the agent)  (your kill switch) control plane, if
       |                                                   one is configured
       |                                          (it routes and delivers; the
       |                                           SDK never sends an alert)
       |
       +--> failed call --> provider circuit (process-wide, per provider)
                                     |
                        open --> CircuitOpen before the next call
                                 (only under on_provider_failure="open")
```

---
