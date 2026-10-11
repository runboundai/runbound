# Security policy

## Reporting a vulnerability

Please report security issues privately, not as a public GitHub issue: email
**runboundai@gmail.com** with a description of the issue and, if you
have one, a minimal reproduction. That address is a stand-in until a
dedicated security contact exists; it is read by the same people either way.
We will acknowledge your report within a few business days and keep you
updated as we work on a fix.

We follow a **90-day disclosure window**: we ask that you give us 90 days
from the initial report to investigate and ship a fix before any public
disclosure, and we will tell you as soon as a fix is available so that
window can close early where it makes sense for everyone.

## Supported versions

| Version | Supported |
|---|---|
| `1.0.0a2` and later releases of the 1.0 alpha line | Yes: security fixes are made against the latest |
| The 0.x line (anything before `1.0.0a2`; `1.0.0a1` was never published) | No: a different product, with no support branch |

`python -c "import runbound; print(runbound.__version__)"` prints your
version. Upgrade before filing anything that is not itself a security report.

## Threat model

runbound runs in your process, on the calling thread, with no sandbox. It
decides whether a decorated tool's body runs, not what the body does. Each
statement below names the module or test to check it against.

### What leaves the process

With no `token` and no `control_plane_url` (the default), the SDK makes no
control-plane call (`runbound/config.py`, `runbound/shared.py`). Once
connected, every request goes to the plane URL you configured, `token` as the
Bearer key (`runbound/plane.py`):

- **hello** (`POST /v1/hello`, every `poll_s`): service, worker id
  (`<hostname>:<pid>:<6 hex>` unless you set `worker_id`), SDK version, the
  policy version and `rows_hash` held, open keyed sessions, coverage counts and
  decorated tool names, the halt ack, quota units held, approval ids being
  executed, and the **tool report**: per tool its name, parameter names,
  annotations and required flags (never a default value), module, the first
  docstring line, declared rules (a predicate as `module:qualname`) and classes.
- **policy** (`GET /v1/policy?service=...`): the service name.
- **events** (`POST /v1/events`, while `export_events=True`): tool names,
  timings, an argument digest salted per process (it matches only within one
  process), a failure's exception *class*, refusals with tool, rule and reason,
  and runtime changes. A session key travels as `sha256(key)` and is redacted
  out of messages and details (`tests/test_privacy_wire.py`); session tags and
  the reason your hook passes to `hooks.refuse(...)` travel as written.
- **approvals** (`POST /v1/approvals`, `GET /v1/approvals/{id}`,
  `POST /v1/approvals/{id}/report`): tool, request id, the **final arguments**
  (a person has to see what they approve), mode, timeout, service, worker id,
  run id, key hash, and a deferred tool's seal. A report carries the outcome,
  the worker id and, for `executed`, a SHA-256 of the JSON-encoded result.
- **quota take** (`POST /v1/quota/take`): tool, service, and the key hash for a
  per-key quota (`runbound/quota.py`).

Separately, with `init(otel_endpoint=...)` and no OpenTelemetry provider of
your own, spans are posted to that endpoint (`token` as the Bearer key) with
names, ids, digests, sizes, result flags, a refusal's reason and `error.type`
(`runbound/spans.py`, `runbound/otlp.py`).

**Never sent:** a tool's result, an exception's message, prompt or model text,
the approval secret, and the raw session key (unless `send_session_keys=True`).
The token is never written to disk. Arguments leave in one place only: an
approval request. Two caveats. With your own `TracerProvider`, a body exception
goes to the span's `record_exception`, which by OpenTelemetry's convention
records its message and stack trace in *your* pipeline. And a plane row can
require approval for any tool, so a plane you do not trust can have the
arguments of the tools it puts behind approval sent to it.

### The policy cache

The last policy the plane served is written atomically (temp file, `fsync`,
`os.replace`) to `<dir>/runbound/<sha256(plane_url)[:12]>/<service>.json`,
directory `0700`, file `0600`; an older `policy_version` never overwrites a
newer one (`runbound/cache.py`, `tests/test_cache.py`). It holds the policy,
rows, their hash, timestamps, `key_id` (`sha256(token)[:16]`) and a `mac`
(HMAC-SHA256 under a key derived from the token). A hand-edited, truncated,
other-service or other-token file is ignored as if absent, with one log line.
That is **tamper-evidence, not a boundary**: anyone who can read the token can
write a file that verifies. Rules declared in code never come from the cache,
so a forged file can only change what the plane served, which never loosens a
code rule. Undelivered approval reports go to `<service>.reports.jsonl` beside
it (`0600`; approval id, outcome, digest, worker id; never an argument or a
result).

### The approval seal

A deferred request carries `{"v": 1, "not_after", "mac"}`: HMAC-SHA256 under
the per-service secret (`init(approval_secret=)` or `RUNBOUND_APPROVAL_SECRET`)
over the canonical JSON of `{v, service, tool, request_id, args, not_after}`.
The secret is never sent to the plane and is not in the seal
(`tests/test_approvals.py`). A worker checks the seal of an approved request
**before any hook runs**. A MAC that does not match runs nothing and is
reported `tampered` (`approval_tampered`); a valid MAC past `not_after` plus
300 s runs nothing and is reported `clock_skew`. With no secret nothing is
sealed, and only the requesting worker runs the request, with the arguments it
holds in memory. The execution runs the full hook chain, so a policy that
tightened after approval still refuses it.

### When the plane goes away

`read`, `write` and `external` tools keep running on the cache. `financial`,
`destructive`, `privileged` and unclassified tools refuse `plane_unavailable`
once `consequential_grace_s` (default and maximum 60 s) has passed since the
last answered hello, and at start-up unless the cache was confirmed within 15
minutes. A new approval needs a connected link (`approval_unavailable`); with
no plane at all an approval tool never runs. A quota past its lease refuses
`quota_plane_unavailable` unless its row says `fail_mode: open`. See
[Plane loss](docs/guides/plane-loss.md).

### What a hostile or compromised plane could, and could not, do

**It could:** halt a service (with the default `on_halt="raise"` every guarded
tool refuses; a halt lapses 60 s after the plane stops answering, but a plane
that keeps answering can hold it); serve rows and an org policy that only
tighten: block a tool, lower a session cap, add a quota, require approval,
raise a risk class (`runbound/policy_rows.py`, `merge` in `runbound/policy.py`);
refuse quota takes and deny approvals; approve a real pending request; offer
a sealed request again until its `not_after` passes (use
`runbound.current_request()` as the tool's idempotency key); and read what is
listed above, including every approval request's arguments.

**It could not:** change what a sealed deferred request runs (it lacks the
secret, and an unsealed request runs only from the requesting worker's memory);
loosen a rule declared in code or in `init()` (rows fold most restrictive wins,
field by field, and callables never come from the plane); or make the SDK send
a result, an exception message or prompt text: no request has a field for one.

### Limits

- **A timed-out hook cannot be killed.** A user hook that outlives
  `hook_timeout_s` runs on in its daemon thread after the call moves on
  (`runbound/hooks.py`). A hook that can hang should bound its own work.
- **A decorated tool's body is not sandboxed.** It runs in your process with
  your privileges. runbound refuses it before it starts; it does not watch it.
- **An undecorated tool is not guarded.** `runbound.coverage()` and
  `runbound.assert_guarded()` show what is.

More: [Privacy](docs/reference/privacy.md), [Guarantees](docs/reference/guarantees.md).
