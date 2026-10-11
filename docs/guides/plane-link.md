# Plane link

[← Docs](../README.md)

This page is the wire between the SDK and a control plane: every request the
SDK makes, when, and what it carries. Without a plane configured the SDK makes
no request at all.

```python
import runbound

runbound.init(token="...", control_plane_url="https://plane.internal.example.com", service="support-bot")
```

`control_plane_url` needs a `token` (`token=""` for a plane with no auth). Both
can come from `RUNBOUND_TOKEN` and `RUNBOUND_PLANE_URL`. A URL from the
environment with no token logs one warning and the process stays local. A
`token` with no URL means the hosted plane, which does not exist yet: one
warning, and the process stays local. `service` is what the plane files the
worker under; `worker_id` defaults to `host:pid:suffix`.

Every request carries `Content-Type: application/json`, `X-Runbound-Service`,
`X-Runbound-Worker`, `X-Runbound-SDK` (the SDK version) and, when the token is
not empty, `Authorization: Bearer <token>`.

## The endpoints

The SDK calls seven endpoints on the plane and nowhere else.

| Call | Sent by | When | What it carries |
|---|---|---|---|
| `POST /v1/hello` | `plane.py` | at start, then every `control_plane_poll_s` (±10%) | the heartbeat: who this worker is and what it holds (below) |
| `GET /v1/policy` | `plane.py` | a hello reply names a `policy_version` other than the one held | `?service=<service>` and nothing else |
| `POST /v1/events` | `plane.py` | in the background, about once a second | `batch_id`, `service`, `worker_id`, `sent_at`, `dropped`, and four lanes: `events`, `anomalies`, `exits`, `changes` |
| `POST /v1/quota/take` | `quota.py` | a call on a tool with a quota and no lease left | `tool`, `service`, and `key` (the sha256 of the session key) for a per-key quota |
| `POST /v1/approvals` | `approvals.py` | a call that needs a person | `tool`, `request_id`, `args`, `mode`, `timeout_s`, `service`, `worker_id`, `run_id`, `key` when keyed, `seal` when deferred and a secret is set |
| `GET /v1/approvals/{id}` | `approvals.py` | a sync approval waiting for its answer | `?wait_s=` (at most 25): a long poll |
| `POST /v1/approvals/{id}/report` | `approvals.py` | after an approved call ran | `outcome` (`executed`, `failed`, `tampered`, `clock_skew`), `worker_id`, and `result_digest` (a sha256 of the result) for an executed call |

The hello, the policy fetch and the events batch run on background threads,
never on a tool call. The other four are made from the call that needs the
answer: a quota take when no leased unit is left, and the approval routes. The
approval request is the only one that carries a tool's arguments, because a
person has to see what they approve. Spans for OpenTelemetry go to
`otel_endpoint`, not to the plane; see [OpenTelemetry](opentelemetry.md).

Timeouts: hello 2 s, policy 2 s, events 5 s, quota take 3 s, approval create
and report 5 s, the long poll `wait_s + 5` s.

## The hello

The first hello goes out when `init()` starts the link, then one every
`control_plane_poll_s` seconds (default 5) times a random factor between 0.9
and 1.1, so a fleet does not say hello in lockstep after a plane restart. A
reply with `poll_s > 0` sets the interval. The pace is the same in every link
state.

What a hello carries:

| Field | What it is |
|---|---|
| `service`, `worker_id`, `sdk_version` | who is speaking |
| `policy_version_seen` | the org policy version this worker holds |
| `active` | keyed `runbound.session()` blocks open in this process |
| `coverage` | decorated tools and their names, tool calls seen, keyed sessions seen, tools per capability class |
| `halt_ack` | whether this worker is enforcing a halt |
| `tools_hash` | the hash of the tool report, on every hello |
| `tools` | the tool report itself (`runbound.tools()`), only when its hash has not been acknowledged |
| `rows_hash` | the hash of the console rows held, when any are |
| `quota_held` | units this worker still holds per tool in the current window, when any |
| `renew_claims` | ids of approved deferred requests this worker is executing (at most 20), which extends their claims |

What the SDK reads from the reply: `halt` and `halt_mode`, `policy_version`,
`poll_s`, `notice`, `tools_known` (`false` makes the next hello resend the tool
report), `fleet_state`, `quota_leases` (`tool`, `window_start`, `window_end`,
`granted`, ...), `approved` (deferred requests to run, each verified against
its seal before anything runs) and `claims_lost` (ids no longer this worker's).

A reply with `fleet_state: "unavailable"` means the plane could not read its
own state: the SDK keeps the halt and policy version it held, applies no
leases, and the link reads `degraded`. Any other reply applies the halt, fetches
the policy when the version changed, applies the leases, and sends any queued
approval reports.

## The policy fetch

`GET /v1/policy?service=<service>` answers with an envelope (`version`,
`dry_run`, `policy`, `rows`, `rows_hash`); a bare body is read as the policy
itself. The console rows are replaced only when `rows_hash` differs from the
one held, because `policy_version` also moves for other reasons. The result is
written to the [policy cache](plane-loss.md#the-policy-cache). A fetch that
fails keeps the policy held, and the next hello tries again.

## The events batch

A background thread posts `POST /v1/events` about once a second, or at once
when 200 records are waiting. A batch holds up to 200 records: `changes` first,
then `anomalies`, then `events`.

| Lane | What it holds |
|---|---|
| `events` | per-call records (`tool_call`, `tool_result`, `tool_error`, ...). With `otel_endpoint` set, the per-call kinds go to spans instead and leave this lane. |
| `anomalies` | refusals and other verdicts, with how the SDK reacted. |
| `exits` | always an empty list in this release. |
| `changes` | runtime changes, such as the applied `policy_version`; sent before the other lanes. |

`batch_id` is 32 hex characters and stays the same when a batch is resent.
`dropped` is the number of records lost so far in this process.

With `export_events=False` nothing is queued and no batch is sent; the hello
and the policy fetch go on. A session key travels only as its sha256 hash
unless `send_session_keys=True`. No lane carries a prompt, a tool argument or a
tool result; see [Privacy](../reference/privacy.md).

Each lane holds at most 10,000 records; past that the oldest is dropped and
counted in `dropped`. A batch the plane does not accept is put back at the
front of its lanes, in order, and retried with a backoff that starts at 0.5 s
and doubles up to 10 s. At interpreter exit the queue is drained once, for up
to 3 s.

## When calls fail

- **Never raised.** A failed hello, fetch or batch is caught, counted, and
  logged at most once a minute (`runbound: control plane call ... failed (N in a
  row); using local state`). Tool calls keep using what the worker holds.
- **A missed hello** counts toward `degraded`; 60 s without an answered hello is
  `unavailable`. What each mode allows is in [Plane loss](plane-loss.md).
- **A rejected key** (HTTP 401 or 403) is logged once. After it, the hello, the
  fetch and the batches stop opening sockets, and the queued records are
  dropped and counted.
- **A quota take** that fails, other than a 400 or 404, refuses with
  `quota_plane_unavailable` unless the row's `fail_mode` is `open`. A 400 or 404
  means the plane has no such quota, and the call runs.
- **An approval request** is retried with a backoff from 0.25 s to 5 s until its
  deadline; a transport error is never read as a denial. 429 refuses with
  `quota_exceeded`; 409, 400, 401, 403, 404 or 422 refuses with
  `approval_unavailable`. A report is tried 4 times, then queued on disk and
  sent on the next connected hello.

---
