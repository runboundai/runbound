# Privacy

[← Docs](../README.md)

What leaves the process, where it goes and when, field by field. The telemetry is content-minimizing,
not content-free: it carries tool names, counts, timings, hashes, your session tags and the
sentence a refusal says. This page lists exactly what that is.

The SDK opens a socket to two places only, and only when you configure them:

- **the control plane**, with `token` and `control_plane_url` (or `RUNBOUND_TOKEN` and
  `RUNBOUND_PLANE_URL`);
- **an OTLP collector**, with `otel_endpoint`.

With neither, nothing leaves the process. A configured OpenTelemetry provider is your own
pipeline inside the process: the SDK hands it spans and never installs or calls an exporter of its
own there.

## What never leaves

- **Tool arguments**, with one exception: a tool with `approval="required"` sends its final
  arguments to the plane in the approval request, so a person can decide
  ([below](#approvals)). Everywhere else an argument travels only as a salted digest.
- **Tool results.** At most their size (`runbound.result_bytes` on a span) and, after an approved
  deferred request ran, a sha256 of the result's JSON.
- **Exception messages.** A tool's exception is kept in-process (truncated to 500 characters). An
  exported event carries only `error_class`: the identifier at the front of the message when it
  starts with `Name:`, else `null`. Hooks and built-ins that fail are reported by name, not by
  their exception text. (The one exception is your own OpenTelemetry provider; see
  [spans](#spans).)
- **The session key in the clear.** It travels as `sha256(key)`. A refusal or anomaly that names
  the key has it replaced by the first 12 hex characters of that hash and `…`, unless you set
  `send_session_keys=True`.
- **The approval secret.** It signs the seal locally and is never sent.
- **Parameter default values and the rest of a docstring.** The tool report carries each
  parameter's name, annotation and whether it is required, and the docstring's first line only.

Local records (`runbound.events()`, `runbound.decisions()`, `runbound.tool_results()`) live in
memory and are never written to disk. Your `on_event` callback receives them in-process.

## The hashes

| Hash | How | What it allows |
|---|---|---|
| `args_hash` (events), `runbound.args_digest` (spans) | sha256 over a random 16-byte salt made once per process, then `repr((tool, args, sorted kwargs))` | Telling two calls apart inside one process. The same call hashes differently in another process, so it is not a fingerprint across workers or restarts. |
| `key_hash` | Unsalted sha256 of the session key | Following one caller across workers. Anyone who can guess a key can confirm the guess, so use opaque ids, not email addresses, as session keys. |
| `result_digest` (approval reports) | sha256 of the result serialized as sorted JSON | Recognising a result you also hold. |

## The control plane

Every call carries the headers `Content-Type: application/json`, `X-Runbound-Service` (the
`service`), `X-Runbound-Worker` (the worker id), `X-Runbound-SDK` (the SDK version) and, when the
token is not empty, `Authorization: Bearer <token>`.

### `POST /v1/hello`

Every `control_plane_poll_s` (±10%), in every link state.

| Field | What it is |
|---|---|
| `service`, `worker_id`, `sdk_version` | Who this worker is. |
| `policy_version_seen` | The version of the plane's policy this worker holds. |
| `active` | How many keyed sessions are open in this process. |
| `coverage` | `decorated_tools`, `decorated_tool_names`, `tool_calls_seen`, `keyed_sessions_seen`, and `tools_by_class` (a count per capability class). |
| `halt_ack` | Whether this worker is enforcing a halt. |
| `tools_hash` | A hash of the tool report. |
| `tools` | The tool report, only when its hash changed since the plane last acknowledged it: per tool, `name`, `decorated`, `params` (`name`, `annotation`, `required`), `doc` (first line), `module`, `rules` (what the decorator states; a predicate as `"module:qualname"`), `effects`, `polling`, `idempotent`, `retryable`. |
| `rows_hash` | The hash of the dashboard rows held, when there are any. |
| `quota_held` | Quota units this worker holds for the current window, per tool, when it holds any. |
| `renew_claims` | Ids of approved deferred requests this worker is running (at most 20), when there are any. |

The reply can carry approved deferred requests (`approved[]`), with the arguments that were
approved and their seal.

### `GET /v1/policy?service=<service>`

Only when the hello reply announces a new policy version. The request carries the service name and
nothing else. What comes back (the policy, its version, the rows and their hash) is written to the
[policy cache](#on-disk).

### `POST /v1/events`

Batched in the background, never on the calling thread. `export_events=False` sends none of it.

| Field | What it is |
|---|---|
| `batch_id`, `service`, `worker_id`, `sent_at`, `dropped` | The envelope; `dropped` counts records lost to a full queue. |
| `events[]` | One record per `tool_call`, `tool_error` and `tool_result`: `ts_wall`, `kind`, `key_hash`, `step`, `tool_name`, `args_hash` (on `tool_call`), `duration_s`, `error_class`. The wire format also has `tokens_in`, `tokens_out`, `tokens_reasoning`, `cost_usd`, `model`, `priced`, `partial`, `tokens_estimated` and `provider`; a tool call leaves them at their defaults (zero, `false` or `null`). A `tool_result`'s outcome, flags and sizes are local only. |
| `anomalies[]` | One per refusal or anomaly: `ts_wall`, `key_hash`, `detector`, `severity`, `message` (at most 256 characters), `details`, `reacted`, `anomaly_id`. `details` holds the tool, the rule, the hook name, the decision, the local session id and your session tags as written; only strings, numbers, booleans and lists or dicts of them, strings cut to 256 characters, at most 64 entries and 4 levels deep. |
| `changes[]` | Runtime changes such as a new `policy_version`: `ts_wall`, `kind`, `what`, `from`, `to`, and `key_hash`, `scope`, `source`, `reason`, `level` when set. |
| `exits[]` | Always empty. |

The `message` of a refusal is the sentence the refusal says. For `hooks.refuse(reason)` that is your
own `reason`, sent as written: never put an argument value in it. Argument validation names the
parameter and the types, never the value.

With `otel_endpoint` set, `tool_call`, `tool_error` and `tool_result` records are left out of
`/v1/events` (the spans carry them); refusals, anomalies and changes still go.

### Approvals

Only for a tool decorated with `approval="required"`. See [Approvals](../guides/approvals.md).

| Call | Body |
|---|---|
| `POST /v1/approvals` | `tool`, `request_id`, `args` (the final arguments, as one JSON object), `mode`, `timeout_s`, `service`, `worker_id`, `run_id` (the local session id); `key` (the session key's hash) when the session has a key; `requested_for` (the email or user id you passed to `runbound.session(requested_for=...)`, in the clear: it is what the plane compares the decider with) when the block names one; `seal` for a deferred request when an approval secret is set; `args_digest` (a sha256 of the tool name and the arguments, which the plane recomputes), `requirement` (the id of the rule that asks) and the decision record (rule ids, policy version, policy state, contributors, SDK version; no argument values). |
| `GET /v1/approvals/{id}?wait_s=N` | No body. |
| `POST /v1/approvals/{id}/report` | `outcome` (`executed`, `failed`, `tampered`, `clock_skew`), `worker_id`, and `result_digest` when a deferred request ran and returned a result. |

### `POST /v1/quota/take`

Only for a tool whose dashboard row has a quota, once this worker's lease is spent: `tool`,
`service`, and `key` (the session key's hash) for a per-key quota.

## Spans

One `execute_tool {tool}` span per call, only when something receives it. See
[OpenTelemetry](../guides/opentelemetry.md).

| Attribute | What it is |
|---|---|
| `gen_ai.operation.name` | `execute_tool` |
| `gen_ai.tool.name`, `gen_ai.tool.type` | The tool's name; `function`. |
| `gen_ai.tool.call.id` | LangChain's `run_id`, when the handler sees the call. |
| `error.type` | The exception's class name for a body that raised; `runbound.refused` for a refusal. |
| `runbound.service`, `runbound.worker_id`, `runbound.run_id` | The service, the worker id, the local session id. |
| `runbound.capability` | The tool's classes, comma-separated. |
| `runbound.outcome` | `ok`, `error`, `refused`, `withheld` or `pending_approval`. |
| `runbound.args_digest` | The salted argument digest. |
| `runbound.result_bytes` | The result's serialized size before the post chain. |
| `runbound.result.flags`, `runbound.result.flag_score` | The names of the injection patterns that matched, and their summed weight. Never the matched text. |
| `runbound.decision.verdict`, `runbound.decision.kind`, `runbound.decision.policy_version` | `allow` or `refuse`; `action`; the plane policy version held. |
| `runbound.decision.boundary`, `runbound.decision.level`, `runbound.decision.detector`, `runbound.decision.reason` | On a refusal: what refused it, and the sentence it says, with the session key replaced. |
| `runbound.approval.request_id` | The approval request, for a call that asked a person. |

- **To `otel_endpoint`:** posted to `<otel_endpoint>/v1/traces` as gzipped OTLP/JSON with
  `token` as the Bearer key. The resource carries `service.name` (the `service`),
  `service.instance.id` (the worker id) and `telemetry.sdk.name` (`runbound`). No argument, result
  or exception message is in it.
- **To your own provider:** the same attributes. When a tool body raises, the span also records the
  exception the OpenTelemetry way (`record_exception`), which includes its message and stack
  trace. That record stays in your pipeline, wherever you send it.

`runbound.otel.enable()` sends local refusals, anomalies and changes to your OpenTelemetry
providers as log records and two counters, with the session key as `runbound.key_hash` only.

## On disk

With a plane configured, two files can be written, both `0600` in a `0700` directory (see
`cache_dir` in the [configuration reference](configuration.md)):

- **The policy cache** `<service>.json`: the service, the plane's policy and its version, the
  dashboard rows and their hash, timestamps, a key id (`sha256(token)[:16]`) and a MAC. No token,
  argument or result.
- **The report queue** `<service>.reports.jsonl`, only when an approval outcome could not be
  delivered: the approval id, the outcome, the worker id, the result digest and when it was
  queued. No argument or result.

Nothing else is written to disk.

---
