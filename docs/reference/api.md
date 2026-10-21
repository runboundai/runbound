# API reference

[← Docs](../README.md)

The full public API, with the hook chain, the result chain, rows and quotas, plane loss, approvals
and spans, is [API.md](../../API.md). `tests/test_public_api.py` checks that file against
`runbound.__all__`. This page is the index: every public name, one line each. The parameters of
`init()` are on the [configuration reference](configuration.md).

## Set up and guard

| Name | What it is |
|---|---|
| `runbound.init(**kwargs)` | Configure the SDK for this process and start a session. Bad values raise `ValueError`, unknown names `TypeError`. |
| `runbound.tool` | Decorator: every call of the function is checked before its body runs and recorded. Takes the rule keywords (`blocked`, `max_calls`, `constraint`, `require_approval`, `reviewed`), `effects`, hooks, `max_result_chars`, and `approval`, `mode`, `timeout_s`. |
| `runbound.reset()` | Start a fresh session with the same configuration: counters go back to zero and keyed sessions are forgotten. Does nothing before `init()`. |
| `runbound.session(key, tags=None)` | Context manager: everything inside counts toward `key`'s own session (per-session `max_calls`, traces). |
| `runbound.current_session()` | The `SessionState` work is counted toward right now, or `None` before `init()`. |
| `runbound.current_request()` | The request id of the deferred approval being executed right now, else `None` (always `None` in sync mode). |
| `runbound.Injected` | Marker for a tool parameter the app supplies (`Annotated[Session, runbound.Injected]`): never stored, sealed, digested or shown in a deferred approval, and supplied again from `init(provide=...)` when the request runs. See [Approvals](../guides/approvals.md#injected-parameters). |

## Check what is guarded

| Name | What it is |
|---|---|
| `runbound.tools()` | The tool report: one entry per tool this process knows. |
| `runbound.tool_calls(key=None)` | How many times each tool was attempted in a session. |
| `runbound.coverage()` | Which registered tools are guarded, which are not, with the call counters. `middleware_installed` and `middleware_tool_names` say whether a LangChain `RunboundMiddleware` / `govern_tools` governs calls in this process and which tools it has governed (known from their first call). |
| `runbound.check(file=None, as_json=False)` | Print and return a local report of what is guarded (decorated tools and the LangChain middleware); makes no plane call. `python -m runbound check` is the command form. |
| `runbound.assert_guarded()` | Raise `RuntimeError` when nothing is decorated with `@runbound.tool`. |
| `runbound.plane_status()` | The state of the control-plane link, as a `PlaneStatus`. |
| `runbound.flush(timeout=3.0)` | Post what is queued (call spans and events) now, waiting at most `timeout` seconds (never more than 3), and keep running. Does nothing before `init()`. |
| `runbound.shutdown(timeout=3.0)` | Stop the heartbeat and the exporters, posting what is queued within the same bound. Idempotent. Call it from a SIGTERM handler or a container `preStop` hook (see [OpenTelemetry](../guides/opentelemetry.md#delivering-the-queue-before-the-process-ends)). |

## Read what happened

| Name | What it is |
|---|---|
| `runbound.events(n=100)` | Recent anomalies, refusals and runtime changes, in memory. |
| `runbound.tool_results(n=100)` | Recent `tool_result` records, one per call: outcome, duration, scan flags, sizes, tags; never the result. |
| `runbound.decisions(n=100)` | Recent `Decision` records. |

## Types

| Name | What it is |
|---|---|
| `runbound.RunboundConfig` | The configuration `init()` builds. `GuardrailConfig`, the older name, still imports (it warns once). |
| `runbound.ToolCall` | One attempted tool call, as policy and hooks see it. |
| `runbound.ToolPolicy` | Policy over tools: `deny`, `allow`, `max_calls`, `constraints`, `require_approval`, `on_violation`. |
| `runbound.ToolRules` | The rules one tool states on its decorator. |
| `runbound.Violation` | A policy finding. |
| `runbound.Event` | A recorded event. |
| `runbound.Decision` | A recorded decision. |
| `runbound.Anomaly` | An anomaly event payload. |
| `runbound.PlaneStatus` | What `plane_status()` returns. |
| `runbound.SessionState` | Per-session state: the call counters, the run id and the latch. |

## Exceptions

| Name | What it is |
|---|---|
| `runbound.ExecutionRefused` | A call was refused before its body ran. Carries `reason`, `decision`, `retryable`, `retry_after`, `scope`. |
| `runbound.GuardrailTripped` | The same class as `ExecutionRefused`, under its older name. |
| `runbound.PolicyViolation` | Refused by tool policy; the rule is on `exc.violation`. |
| `runbound.CapabilityRefused` | Refused because an `init(capabilities=...)` class rule denies one of the tool's classes. |
| `runbound.is_retryable(exc)` | Whether `exc` is a refusal a retry may get past. |

See [Handling refusals](../guides/handling-refusals.md).

## Hooks: `runbound.hooks`

| Name | What it is |
|---|---|
| `hooks.allow()` | A hook's "carry on". `None` and `True` mean the same. |
| `hooks.refuse(reason)` | Refuse the call (`reason="hook_refused"`). `False` means the same. |
| `hooks.rewrite(args=None, kwargs=None)` | Replace the arguments; pre hooks only. |
| `hooks.ToolResult` | What a post hook receives. |
| `hooks.replace(result)` | A post hook's replacement result. |
| `hooks.withhold(reason)` | A post hook's "do not pass this result on". |

## Integrations

| Name | What it is |
|---|---|
| `runbound.integrations.langchain.RunboundCallbackHandler` | A LangChain callback handler for tools a framework dispatches. Needs `runbound[langchain]`. See [LangChain](../guides/langchain.md). |
| `runbound.integrations.langchain.GuardrailCallbackHandler` | Deprecated: the former name of `RunboundCallbackHandler`. The same class; warns once with a `DeprecationWarning`. |
| `runbound.otel.enable()` / `runbound.otel.disable()` | Send local refusals, anomalies and changes to your OpenTelemetry providers. Needs `runbound[otel]`. See [OpenTelemetry](../guides/opentelemetry.md). |

## Metadata

| Name | What it is |
|---|---|
| `runbound.__version__` | `1.0.0a7`. |

---
