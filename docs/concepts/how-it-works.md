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

## Admit, execute, reconcile, record

The picture above is `execute -> observe -> detect -> react`: a call goes
out, its outcome is recorded as an event, the detectors read the session, and
only then does anything refuse. With `envelope=True` (the default since
0.4.0) the sequence gains a door in front of it, for a model call or a tool
action:

```
   request
      |
      v
   admit  --  circuit, unpriced model, [steps, run time, tokens]*, money hold
      |            (* config.envelope only; a pre-existing wall checked one
      |  allow       call earlier, so it honors on_anomaly like the wall
      |               does — "warn" logs and lets the call through)
      v
   execute  --  the provider call, or the tool body
      |
      v
   reconcile  --  settle the money hold against what the call actually cost
      |
      v
   record  --  the event lands in SessionState; the post-call walls
                (steps, timeout, the tokens half of budget) still run,
                exactly as before, in case anything got past the door
```

A tool action's door is the same shape, minus the money hold: posture, a
capability class rule, then (`config.envelope` only) `max_actions_per_run`.

`admit` never guesses about the call it is judging — every door stage is a
pure comparison of numbers already known (`runbound/admission.py`), and every
refusal raised **at the door** carries a `Decision` (`exc.decision`, also
`anomaly.details["decision"]`): the verdict, which boundary of the execution
envelope was hit, which detector's name it reuses, and the numbers behind it.
A post-call detector trip — the wall itself firing after a call already went
out — does not carry one. Steps, run time and tokens are not new controls;
they are `max_steps`/`max_session_seconds`/`max_total_tokens` themselves,
checked one call earlier, so they honor `on_anomaly` exactly as the wall
behind them always has — only `"raise"` actually refuses here, at the door.
`"warn"` and `"callback"` both log/alert and let the call through instead:
the call's own event still gets recorded, and the wall behind the door
detects the very same crossing on it and reacts exactly once — invoking your
callback and latching, under `"callback"`, one call later than the door.
`max_actions_per_run` and the money hold are not pre-existing walls, so they
always refuse, ignoring `on_anomaly`, as
they always have. `runbound.envelope(key=None)` reads the same boundaries
back as one object —
what this agent is allowed to do *right now* — composed from `budget()`, the
session's own counters, `posture()` and any configured class rules:

```python
>>> runbound.envelope("user:8842")
{"agent": "refunds-prod", "scope": "user:8842", "posture": "restricted",
 "budget": {"remaining": 4.20, "reserved": 0.80, "max_request": 4.20},
 "execution": {"steps_remaining": 13, "seconds_remaining": 41,
               "actions_remaining": 4, "descendants_remaining": 7},
 "capabilities": {"read": "allow", "write": "allow", "external": "deny",
                   "financial": "deny", "destructive": "deny", "privileged": "deny"}}
```

`envelope=False` restores 0.3.0 exactly: the door stages `steps`, `run time`
and `tokens` are skipped, and only the post-call walls ever fire — the
`envelope` flag changes nothing about money admission (`budget_admission`)
or posture/capability enforcement, which are their own, older opt-ins.
