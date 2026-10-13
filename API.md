# runbound public API

Everything importable as `runbound.<name>` and listed in `runbound.__all__`.
`tests/test_public_api.py` checks this file against `__all__`, and checks that
each name removed in S1 raises `AttributeError`.

## Entry points

- `init` — configure the SDK for the process (token, plane URL, tool policy, ...).
- `reset` — start a fresh session, keeping the configuration and engine (counters to zero, keyed sessions forgotten).
- `tool` — decorator: every call of the function is checked against policy and recorded.
- `tools` — the registry of decorated tools.
- `tool_calls` — per-tool call tally for the current session.
- `coverage` — which registered tools are guarded, which are not.
- `check` — local self-check report; makes no plane call.
- `assert_guarded` — raise `RuntimeError` when nothing is decorated with `@runbound.tool`.
- `session` — run/key context for traces and per-session `max_calls`; `session(key, tags=None, requested_for=None)`: `requested_for` (an email or user id, at most 128 characters) rides the approval request of a tool that asks a person inside the block, and the plane answers 409 `self_approval` when that same person decides it.
- `current_request` — the request id of the deferred approval being executed right now, else `None`.
- `current_session` — the active session, if any.
- `events` — recorded anomalies, refusals and runtime changes.
- `tool_results` — recorded `tool_result` records (one per call: `tool`, `outcome`, `duration_s`, `flags`, `flag_score`, `flagged`, `truncated`, sizes, `tags`; never the result). Its own ring, apart from `events`, because it grows with every call and would push the refusals out; `on_event` receives the same records as they happen.
- `decisions` — recorded decisions.
- `plane_status` — state of the control-plane link.

## Types

- `RunboundConfig` — the `init` configuration. (`GuardrailConfig`, the older name, still imports: see Deprecated names.)
- `ToolCall` — one attempted tool call, as policy sees it.
- `ToolPolicy` — policy over tools.
- `ToolRules` — per-tool rules.
- `Violation` — a policy finding.
- `Event` — a recorded event.
- `Decision` — a recorded decision.
- `Anomaly` — an anomaly event payload.
- `PlaneStatus` — the value `plane_status` returns.
- `SessionState` — per-session state: the call counters, the run id and the latch.

## Exceptions

- `ExecutionRefused` — base class: a call was refused before its body ran.
- `PolicyViolation` — refused by tool policy.
- `GuardrailTripped` — a guard tripped.
- `CapabilityRefused` — refused because an `init(capabilities=...)` class rule denies one of the tool's classes.
- `is_retryable` — whether an exception is worth retrying.

## Hooks (`runbound.hooks`)

The hook chain, in this fixed order for every call (RS-3): user rewrite hooks,
argument validation of the final arguments, the tool policy rules, the capability
class rules and action cap, approval, then user gate hooks, then
the body. Built-ins run before user gate hooks, and the first refuse wins.

Set hooks with `init(pre_hooks=[...], gate_hooks=[...], hook_timeout_s=5.0)` and
`@tool(pre_hooks=[...], gate_hooks=[...])`; init's run first. A hook takes a
`ToolCall` and returns:

- `hooks.allow()` (or `None` / `True`) — carry on.
- `hooks.refuse(reason)` (or `False`) — refuse; `reason="hook_refused"`.
- `hooks.rewrite(args=None, kwargs=None)` — replace the arguments. `pre_hooks` only:
  a rewrite from a gate hook is a hook error.

A user hook that raises or outlives `hook_timeout_s` refuses with
`reason="hook_error"` when the tool carries a consequential class (`financial`,
`destructive`, `privileged`) or no class at all; for a tool that only reads, writes
or reaches `external` it is logged once and skipped (the `hook_error:<name>` tag on the
`tool_result` event records it). A timed-out hook's thread cannot be killed: it keeps
running after the call moves on, so a hook that can hang should bound its own work. A built-in that raises (the
validator, the policy, the capability rules) refuses every class. Invalid arguments
are refused with `reason="invalid_arguments"` before the body runs, sync or async;
nothing is coerced.

## Results (the post chain)

What a decorated tool returns passes through, in this order, before the caller sees it:

1. **redact** — values of secret-named dict keys (Sentry's 33 plus `access_token`,
   `refresh_token`, `client_secret`, `bearer`; exact, case-insensitive, recursive) become
   `"[REDACTED]"`. `init(redact_keys=[...])` adds more. Plain strings are not redacted.
2. **truncate** — JSON stays valid: strings cut at 200 chars, lists and dicts at 20 items,
   nesting at depth 6, each with a marker (`…[truncated N chars]`, `…[N more items truncated]`);
   the serialized size is capped at 16,000 chars (head and tail around a marker, which says so
   when the structure is no longer valid JSON). `init(result_limits={"max_chars", "max_str",
   "max_items", "max_depth"})` and `@tool(max_result_chars=)` change the caps.
3. **scan** — 15 prompt-injection patterns; the result is *flagged* once the weights of the
   patterns that hit sum to 1.0. It tags, never blocks, and reports pattern names, never text.
4. **user post hooks** — `init(post_hooks=[...])` then `@tool(post_hooks=[...])`. A hook takes a
   `hooks.ToolResult` and returns `allow()`, `hooks.replace(result)` or `hooks.withhold(reason)`.

A redaction that raises **withholds** the output (`"Output withheld: post-hook redact failed"`)
for every class. A user post hook that raises or times out withholds the output for a tool that
carries a consequential class or none, and is tagged and skipped for read, write and external.
A failed truncation falls back to the plain size cap; a failed scan leaves the result untagged.
Exception text never reaches the model. Non-JSON results (objects) pass through untouched.

Every call ends in a **`tool_result` event** (observers, the `on_event` callback and `runbound.tool_results()`; not `runbound.events()`): `duration_s`,
`outcome` (`ok`, `error`, `refused`, `withheld`, `pending_approval`), the scan's
`flags`, `flag_score`, `flagged`, `truncated`, sizes (`result_chars`, `result_chars_out`) and
`tags` (`hook_error:<name>` for a failing user hook of any chain slot that was skipped,
`scan_failed`, `truncate_failed`). It never carries the result. It shares its `tool_call`'s step
and is recorded even in a latched session. The wire (`/v1/events`) carries the existing event
fields only. The LangChain handler's
`on_tool_end` records the same event (it cannot replace the output).

## Per-tool rows and quotas (the dashboard's rules)

`GET /v1/policy` also serves `rows` (one per tool, state `dry_run` or `enforced`) and `rows_hash`. Each row is
folded with the tool's code rules, **most restrictive wins, field by field**: `blocked` OR; the session cap the
lower; `approval_required` OR; `approval_mode` code wins when declared (else the row; a deferred row on a tool
whose arguments are not JSON falls back to sync); `approval_timeout_s` the lower; `risk_class` the stricter
rank (`read < write < external < financial < destructive < privileged`); `fail_mode` `closed` wins. A row can add
rules and never loosen a code rule. A `dry_run` row evaluates everything and refuses nothing: it records
`would_refuse` (a Decision, and a `would_refuse:<rule>` tag on the `tool_result`).

Order, with the refusal reason each rule gives: `blocked` -> `tool_blocked`; session cap -> `session_max_calls`;
quota -> `quota_exceeded` (with `retry_after`) / `quota_plane_unavailable`; then approval. A refused call
consumes no quota and asks no person. The rows are replaced only when `rows_hash` changes: a bumped
`policy_version` is shared with other policies and proves nothing, so a fetch that returns the same hash keeps
what is held. The hello echoes `rows_hash` and `policy_version_seen`; the policy cache stores rows and hash.

A row's `quota` is `{"limit", "window": "minute"|"hour"|"day", "per": "service"|"key"}` in a **fixed
calendar-aligned UTC window** ("20 per clock hour"). The hello's `quota_held` reports what this worker holds for
the current window and the reply's `quota_leases` adds `granted` (an entry with `"drop": true`, sent for a tool that now asks a person, pops everything held for it and adds nothing); everything held is dropped when `window_start`
changes. Leases are spent first; with none left one call is taken with `POST /v1/quota/take` and a refusal is
cached until the window ends (one round trip per window). A per-key quota is never leased: it always takes, with
the session key's hash. With the plane away the lease in hand keeps being spent; past it every class refuses
`quota_plane_unavailable` while `fail_mode` is `closed`; `open` continues at 1/10 of the limit per heartbeat
period. A tool that will ask a person takes nothing here: `POST /v1/approvals` takes the unit before any
approval exists, and a deferred execution never takes a second.

## Plane loss and the policy cache

The last policy the plane served is kept on disk (`init(cache_dir=)` / `RUNBOUND_CACHE_DIR`, else the
platform cache dir, else the temp dir, else memory only; never `XDG_RUNTIME_DIR`; file
`<dir>/runbound/<sha256(plane_url)[:12]>/<service>.json`, dir `0700`, file `0600`). A restarted worker
loads it and enforces it **before its first heartbeat**. It is written atomically (temp file, fsync,
`os.replace`, no locks), never lets an older `policy_version` overwrite a newer one, and holds no token,
argument or result. A `mac` over the body (keyed from the token) makes a hand-edited, truncated,
other-service or other-token file be ignored, as if absent, with one log line. That is tamper-evidence, not
a boundary against anyone who can read the token. Rules declared in code never come from the cache.

`plane_status().mode` is one of four states:

- **connecting** — before the first hello (3 missed hellos from here count toward `degraded`).
- **connected** — a hello was answered.
- **degraded** — 3 hellos missed in a row, or one reply saying the plane could not read its own state.
  **Everything keeps running on the cache.** Only two things refuse: a quota past its lease
  (`quota_plane_unavailable`) and a *new* approval (`approval_unavailable`); a sync approval
  already waiting rides out the outage to its own deadline.
- **unavailable** — 60 s since the last answered hello (or since start, with none). Time, not a count.

A deferred approval being executed is named in the hello's `renew_claims` (at most 20) on every hello, which
extends its claim; the reply's `claims_lost` lists ids that are no longer this worker's (stop renewing, start
nothing new; a body already running reports as usual). Hellos go on every `poll_s` (±10% jitter) in every state; there is no separate probe. While the plane is
away, `read`, `write` (with another tag) and `external` tools keep enforcing the cache of any age (`stale` after 24 h), and a bare `write` (no other tag: unclassified for fail mode) refuses `plane_unavailable` only once the worker holds no policy within 24 h;
`financial`, `destructive`, `privileged` and unclassified tools refuse with `reason="plane_unavailable"`
(retryable) once `init(consequential_grace_s=60)` (15 to 60) has passed
since the last answered hello, and, while still `connecting`, unless the cache was last confirmed within
15 minutes. The cache is rewritten at most once a minute by a hello that confirms the same policy, so its
`written_at` means "last confirmed". A process with no plane configured is unaffected.
`plane_status()` also reports `cache_age_s`, and `plane_degraded`: what the plane last said is wrong with itself while still answering hellos (`"ledger_unreachable"`: it cannot reach its own database), `None` when it said nothing or said it is fine. It is informational, changes no decision and is not the link's health (`mode`): a plane can be `connected` and degraded at once. Each change of it is one `plane_degraded` entry in `runbound.events()` and to `on_event` (`from`, `to`, `cleared`), never one per hello; an unknown key in a hello is ignored, as ever. (The mode once called `degraded` after three failures is now
`unavailable`; missed hellos are what count.)

## Approvals (`@tool(approval="required")`)

`@runbound.tool(approval="required", mode="sync" | "deferred", timeout_s=600)` puts a person in the
loop. It is **slot 5** of the hook chain: the request is built from the *final* arguments, after
rewrite hooks, validation, policy and capability. It talks to the control plane's approval routes
(`POST /v1/approvals`, `GET /v1/approvals/{id}?wait_s=25`, `POST .../report`) with the service's key.
No plane connected: the call is refused at once (`reason="approval_unavailable"`), never run.

- **sync** (default): waits up to `timeout_s` on a monotonic deadline (the plane is the only clock);
  transport errors are retried until then and are never a denial. A denial refuses with
  `reason="approval_denied"`, expiry or no answer with `"approval_timeout"`; the refusal is the
  tool's result. After an approved run the outcome is reported to the plane. Async tools wait off
  the event loop.
- **deferred**: returns `{"status": "pending_approval", "request_id": ...}` at once; the body does
  not run (`tool_result` outcome `pending_approval`). Once a person approves, the plane offers the
  request in `hello.approved[]`; the worker **verifies the seal before any hook runs**, executes
  the tool with the approved arguments through its full chain, reports `executed` / `failed`, and
  calls `init(on_deferred_result=fn)` with `{request_id, tool, outcome, result | error}`.
  `runbound.current_request()` names the request while it runs. A rewrite that would change the
  arguments at that point is a hook error. Registration raises `ValueError` for a deferred tool whose
  arguments are not JSON-friendly (`*args`, `datetime`, unresolvable hints, ...).
- **reports survive an outage**: if `/report` cannot be delivered (a few retries), the outcome is appended
  to `<service>.reports.jsonl` beside the policy cache (`0600`, fsynced; approval id, outcome, digest, worker
  id: never an argument or a result; memory only without a cache directory) and sent on the next connected
  hello, once (idempotent on approval id + outcome, as the plane's report is). Otherwise an action that ran
  would look unexecuted: the plane would expire the row and release its quota unit.
- **the seal**: `{"v": 1, "not_after": "<ISO Z>", "mac": hex HMAC-SHA256(secret,
  canonical_json({v, service, tool, request_id, args, not_after}))}`, canonical = `json.dumps(sort_keys=True,
  separators=(",", ":"))`. The secret is per service (`init(approval_secret=)` or `RUNBOUND_APPROVAL_SECRET`),
  shared by the replicas and **never sent to the plane**. On a MAC mismatch
  nothing runs, the plane is told `tampered`, and `on_deferred_result` gets `outcome="tampered"`
  (`approval_tampered`); a valid MAC past `not_after` (+300 s) is this worker's clock, not an attacker:
  nothing runs and the outcome is `clock_skew`. With no secret nothing is sealed: only the requesting worker can run the
  request, from its own memory (not persisted), and the SDK says so once at registration.
- Accepted limit: a compromised plane can still *approve* a real pending request.
- A full quota: a 429 `quota_exceeded` on create refuses with `reason="quota_exceeded"` carrying
  `retry_after` (seconds); no approval exists, nothing is retried, and the same (service, tool, key) is
  not asked again until the window is over. The create carries the session key's hash as `key` (per-key
  quotas) and a retried create reuses its `request_id`, so the plane never takes a unit twice. An
  approval object with a non-null `quota` means the plane already took the unit for that call.
- Refusal reasons added: `approval_denied`, `approval_timeout`, `approval_tampered`, `approval_unavailable`, `quota_exceeded`.
  Spans carry `runbound.approval.request_id`.

## Spans (one per tool call)

Every decorated call (and every LangChain tool the handler sees) is one INTERNAL span named
`execute_tool {tool}`, instrumentation scope `runbound` at the SDK version. Two paths, chosen
per call, never both:

1. **A configured provider.** If `opentelemetry` is installed and its global `TracerProvider`
   is a real one, the span goes to it, nests under the current span, and is current while the
   body runs. The SDK imports it lazily, depends on nothing, and never installs a provider.
2. **`init(otel_endpoint="http://host:4318")`.** Otherwise the span is posted there as
   OTLP/JSON (`/v1/traces`; stdlib `json` + `gzip` + `urllib`), `token` as the Bearer key,
   batched with backoff; 400/401/403/413 drop the batch, network errors and 5xx retry. With
   `otel_endpoint` set, per-call events leave `/v1/events`, which keeps refusals and changes.

With neither, no span is made and a call pays one lookup. Attributes (strings pinned in
`runbound/otel_names.py`, semconv never imported): `gen_ai.operation.name`, `gen_ai.tool.name`,
`gen_ai.tool.type`, `gen_ai.tool.call.id` (LangChain `run_id`), `error.type`,
`runbound.service|worker_id|run_id|capability|outcome|args_digest|result_bytes`,
`runbound.decision.verdict|kind|boundary|level|detector|policy_version|reason`,
(`verdict` is `allow`, `refuse`, or `would_refuse` for a call a dry-run row's rule would have refused: it ran, `boundary` and `detector` are `policy` and `reason` is the rule),
`runbound.result.flags` (names only) and `runbound.result.flag_score`. Never an argument, a
result or an exception message on the `otel_endpoint` path (a span handed to your own provider records the raised exception, as OpenTelemetry's `record_exception` does). A body exception sets status ERROR with `error.type`; a refusal
leaves status UNSET with `outcome=refused` and `error.type=runbound.refused`.
`runbound.approval.request_id` is set for a call that asked a person.

## Integrations

- `runbound.integrations.langchain.RunboundCallbackHandler` — LangChain callback handler for tools a framework dispatches. Needs `runbound[langchain]`.

## Deprecated names

Each is the same object as its replacement; the first use in a process emits one `DeprecationWarning` naming the new name.

- `runbound.GuardrailConfig` (also `runbound.config.GuardrailConfig`) — the former name of `RunboundConfig`.
- `runbound.integrations.langchain.GuardrailCallbackHandler` — the former name of `RunboundCallbackHandler`.

## Metadata

- `__version__` — `1.0.0a3`.

## Removed in S1

`wrap`, `unpatch`, `llm`, `record_call`, `budget`, `BudgetView`, `circuit_state`,
`inflight_calls`, `active_sessions`, `posture`, `envelope`, `safe_mode`,
`enter_safe_mode`, `exit_safe_mode`, `fleet_status`, `is_tripped`, `clear`,
`session_status`, `key_hash`, `verify_webhook_signature`, `Refusal`.
All of them raise `AttributeError` on `runbound`, and none exists in `runbound.api` either.
