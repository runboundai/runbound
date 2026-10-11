# OpenTelemetry

[← Docs](../README.md)

Every decorated tool call is one **span**. The SDK sends it to your own
OpenTelemetry pipeline, or to an OTLP/HTTP endpoint you name. It never sends an
argument, a result or a prompt. The built-in OTLP path sends no exception message
either; a span on your own provider records the exception the body raised, as
OpenTelemetry's `record_exception` does.

```bash
pip install "runbound[otel]"   # opentelemetry-api and opentelemetry-sdk (optional)
```

## The span

- **Name:** `execute_tool {tool}` (the GenAI semantic-convention name), kind
  INTERNAL, instrumentation scope `runbound` at the SDK version.
- **Attributes:** `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.type`,
  `gen_ai.tool.call.id` (the LangChain `run_id`, when there is one),
  `error.type`, and `runbound.service`, `.worker_id`, `.run_id`, `.capability`,
  `.outcome`, `.args_digest`, `.result_bytes`, `.decision.*` (`verdict`, `kind`,
  `boundary`, `level`, `reason`, `policy_version`), `.result.flags` (pattern
  names only), `.result.flag_score` and, for an approval, `.approval.request_id`.
  The strings are pinned in `runbound/otel_names.py`.
- **Status:** an exception from the body is ERROR with `error.type`. A refusal is
  not a failure of the tool: status stays UNSET with `runbound.outcome=refused` and
  `error.type=runbound.refused`.

## Two paths, chosen per call, never both

1. **Your provider.** If `opentelemetry` is installed and its global
   `TracerProvider` is a real one, the span goes to it, nests under the current
   span and is current while the body runs. The SDK imports it lazily and never
   installs a provider.
2. **`init(otel_endpoint="http://collector:4318")`.** Otherwise the span is posted
   as OTLP/JSON to `/v1/traces` (stdlib `json`, `gzip` and `urllib`; no extra
   needed), with `token` as the bearer key, batched, with backoff. A 400, 401, 403
   or 413 drops the batch; a network error or 5xx is retried.

With neither, no span is made and a call pays one lookup.

When `otel_endpoint` is set, per-call events stop going to `/v1/events`, which
keeps only the low-volume audit records: refusals, approvals and tool-registry
changes.

## Log records and counters for refusals

`otel.enable()` additionally exports the SDK's own refusal and change records as
OpenTelemetry **log records** and two counters. It uses the global providers, or
the ones you pass:

```python
import runbound
from runbound import otel

runbound.init(max_actions_per_run=50)
otel.enable()
```

| `event_name` | when | notable attributes |
|---|---|---|
| `runbound.anomaly` | a limit was reached | `runbound.detector`, `runbound.severity`, `runbound.reacted` |
| `runbound.refusal` | a call was refused | `runbound.detector`, `runbound.reacted`, the Decision as `runbound.decision.*` |
| `runbound.runtime_change` | the runtime or a policy version moved | `runbound.runtime.what`, `.from`, `.to` |

The counters are `runbound.refusals` and `runbound.anomalies`. A session key leaves
only as `runbound.key_hash`, the same sha256 the control plane receives.
`otel.enable()` returns `False` and does nothing when the packages are absent;
`otel.disable()` stops it. Like every observer in runbound it is fail-open: an
exporter that raises is logged and never reaches the call that produced the record.

## What is never exported

No prompt, no reply, no result, no exception text in the built-in OTLP path. An
argument shows up only as `runbound.args_digest`, a hash. See [Privacy](../reference/privacy.md).
