# OpenTelemetry

[← Docs](../README.md)

Every decorated tool call is one **span**. The SDK sends it to your own
OpenTelemetry pipeline, or to an OTLP/HTTP endpoint you name. It never sends an
argument, a result or a prompt. The built-in OTLP path sends no exception message
either; a span on your own provider records the exception the body raised, as
OpenTelemetry's `record_exception` does.

```bash
pip install --pre "runbound[otel]"   # opentelemetry-api and opentelemetry-sdk (optional)
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
- **Identities and the record of one call.** `runbound.action_id`, `.attempt_id` and
  `.evaluation_id` (random, carrying nothing about the call), `.parent_action_id` for a
  governed call made inside another's body, `runbound.boundary` (`sdk-decorator`, or `langchain-middleware` for a plain LangChain tool that `RunboundMiddleware` / `govern_tools` itself governs) and
  `runbound.contract_version`. A refused span carries `runbound.anomaly_id`, and the
  refusal's anomaly names the span back (`span_id`, `trace_id`). A call that was approved
  and re-checked carries `runbound.recheck.evaluation_id`, `.outcome`, `.reason` and what
  the re-check read. `runbound.capability_source`, `.capability_effective` and
  `runbound.service_version` say where the capability declaration came from.
- **The start marker.** Just before the tool's own code runs, a zero-length child span
  `execution_started` is ended, once per attempt: never for a refused call, one parked for
  an approval, or one the re-check refuses. It carries the attempt's identity and
  `runbound.marker.tool` (never `gen_ai.tool.name`) and no argument or decision. A receiver
  that counts every span counts it as a span of its own: Runbound's ingest excludes it by
  name, and must be deployed before an SDK that emits it. A death inside the exporter's
  one-second window loses it; under your own `TracerProvider` it goes to your tracer.
- **Cancellation.** A task cancelled while the body runs ends the span with outcome
  `cancelled` (the `tool_result` says `cancelled`, not an error).
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
   or 415 drops the batch; a 413 splits it (below); a network error or 5xx is retried.

   **Batch identity.** Every post carries `X-Runbound-Batch: <boot_id>.<exporter_id>.<seq>`
   (`boot_id` is drawn once per process, `exporter_id` is 8 random hex characters per
   exporter instance, `seq` counts that exporter's batches from 0), so an ingest that
   understands it can drop a retried insert instead of counting it twice; an ingest that
   does not simply ignores the header. A batch whose post fails retryably is kept as an
   *immutable* unit, the token plus the exact compressed bytes, and is resent first, byte
   for byte, until it is accepted or dropped. Spans that arrive meanwhile queue behind it
   and the drop-oldest rule never touches it. A 413 *halves* the unit: each half is a
   part with its own bytes and a token made of the parent's plus `.p0` or `.p1`, so a
   part refused again halves again (`<token>(.p<N>)*`), down to four levels (200 spans, then
   100, 50, 25, 12); the parent token is retired. A part refused at the limit, or a single
   span, is dropped, counted once, with one warning that says so. Only one flusher touches a unit at a time. Dedupe is bounded, not
   exactly-once: it covers a retry that reaches the endpoint, not a span lost with the process.

With neither, no span is made and a call pays one lookup.

## Where the built-in exporter sends

| `otel_endpoint` | A plane is connected | Built-in tool-call span exporter |
|---|---|---|
| a URL | either | that collector (it wins) |
| unset | yes | the connected plane's existing `/v1/traces`, with the worker's token |
| unset | no | none |
| `False` | either | disabled, with no fallback |

Starting with a7, when you connect a control plane and leave `otel_endpoint` unset, Runbound configures its built-in tool-call span exporter to send to that plane. Set `otel_endpoint=False` before upgrading to disable this built-in export, or specify an explicit collector URL. An application-configured OpenTelemetry provider continues to take precedence and must be configured separately. This setting does not disable other control-plane communication.

What the built-in payload carries is metadata: tool, service, worker, run and action IDs, timings, policy and approval references, reported outcomes and argument digests. It is not anonymous. If you configure your own `TracerProvider` the spans go there instead, the plane default is not started, and a tool body that raises is recorded on your provider with its exception, so exception text can reach your own pipeline; what your provider exports is yours to configure. Install your `TracerProvider` before `runbound.init()`, or set `otel_endpoint=False`: the provider is looked for when `init()` runs, so one installed after `init()` still gets the spans while the plane default is on (the plane then receives them too). Batching, shutdown, errors and delivery loss still apply, and nothing promises that every call appears. `python -m runbound check` shows the resolved destination (never a token or URL credentials) and what this process has sent, queued and dropped.

When the built-in exporter is on, per-call events stop going to `/v1/events`, which
keeps only the low-volume audit records: refusals, approvals and tool-registry
changes.

## Delivering the queue before the process ends

Spans are queued and posted in the background. Python posts what is left at a normal exit and on Ctrl-C, but **not when the process is stopped by SIGTERM** (the default action), which is what Docker and Kubernetes send on a stop or a redeploy: the spans still queued are lost, the last few calls of that process included.

- `runbound.flush(timeout=3.0)` posts what is queued now and keeps running (the end of a job, a request handler).
- `runbound.shutdown(timeout=3.0)` stops the heartbeat and the exporters and posts what is queued. It is idempotent, does nothing before `init()`, and holds the caller at most three seconds whatever `timeout` says (a receiver that stalls cannot hold the process). After it the process should exit: later calls are still governed on the last policy but are no longer exported.

**What this does not do:** `flush` drains to a reachable ingest; spans queued during an outage are lost if the worker exits before it reconnects. There is no disk spool in this release, and the SDK does not claim lossless telemetry.

The recipe for a container: call `runbound.shutdown()` in your SIGTERM handler, or in the `preStop` hook of the pod. It also tells the plane this worker stopped, so the console shows it as stopped and not as disconnected ([Plane loss](plane-loss.md#a-worker-that-stopped-is-not-a-worker-that-disconnected)).

```python
import signal, sys
import runbound

signal.signal(signal.SIGTERM, lambda *_: (runbound.shutdown(), sys.exit(0)))
```

If your server already handles SIGTERM (uvicorn, gunicorn), call `runbound.shutdown()` from its own shutdown hook instead (a lifespan handler, `worker_exit`); installing a second handler would replace theirs.

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
