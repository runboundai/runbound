# Plane loss

[← Docs](../README.md)

With a control plane connected, the last policy it served is kept on disk.
When the plane goes away the worker keeps enforcing that cache; it never
loosens because the plane is gone. Tools that can be undone keep running.
Tools whose effect cannot be undone stop after a short grace period, with a
retryable refusal. A process with no plane configured is not affected by
anything on this page.

## The four modes

`runbound.plane_status().mode` is one of these when a plane is configured
(`local` when none is):

| Mode | When |
|---|---|
| `connecting` | from `init()` until the first hello is answered. |
| `connected` | the last hello was answered. |
| `degraded` | 3 hellos in a row went unanswered, or one reply said the plane could not read its own state (`fleet_state: "unavailable"`). Misses during `connecting` count too. |
| `unavailable` | 60 s since the last answered hello, or since `init()` when none was answered. This is time, not a count, and it wins over `degraded`. |

The first hello goes out at once, then every `control_plane_poll_s` seconds
(default 5; the plane can change it) with ±10% jitter. Hellos continue at that
pace in every mode; there is no separate probe. One answered hello resets the
miss count and, unless it reports `fleet_state: "unavailable"`, returns the
link to `connected`.

## What keeps running

| Tool classes | While the plane is away |
|---|---|
| `read`, `write` (with another tag), `external` | run in every mode, on the cache of any age, once the worker has had a first answered hello or loaded a sealed cache. Before that, nothing runs: they refuse `policy_unavailable` (retryable). |
| a bare `write` (no other tag) | unclassified for fail mode: runs on a valid cache, refuses `plane_unavailable` once the worker holds no policy within 24 hours. |
| `financial`, `destructive`, `privileged`, or no class at all | run until `consequential_grace_s` has passed since the last answered hello (or since `init()`), then refuse with `reason="plane_unavailable"`. Before the first hello is answered they also need a cache confirmed within the last 15 minutes; with none they refuse at once. The same holds for a worker whose hellos are answered but that has not been served the policy the plane announced (every policy fetch failed) and holds no cache: they refuse until the first policy arrives. The worker reads its policy on its first answered hello, whatever version that hello announced, so these tools are released only with the policy (and the service defaults) in hand; a service that really has no policy is served an empty one and gates nothing. |

`consequential_grace_s` is an `init()` option from 15 to 60 seconds, default 60,
so with the default these tools stop exactly when the mode becomes
`unavailable`. A shorter grace stops them earlier, while the mode may still read
`connected` or `degraded`.

**A service with enforced service defaults** (every new service starts with the
standard defaults enforced) does not use the grace: it runs on its sealed policy
for the **offline window**, counted from the last *policy confirmation*. Inside the
window a destructive tool that a console row **allows** keeps running while the
plane is away; one with **no row** is decided by the defaults, which ask a person,
so it is refused `approval_unavailable` because nobody can be asked. Give a tool an
explicit row if it must stop, or keep running, on plane loss. Once the window has
passed, every `financial`, `destructive`, `privileged` and unclassified tool (a
`write` that is also `destructive` included; a bare `write` stays under its own rule)
is refused `plane_unavailable`: expiry only ever refuses more, and `read` / `external`
tools are never affected.

- **The window.** The plane serves it (`service_defaults.offline.execution_s`, 60 to
  86 400 seconds). A new service gets 15 minutes. An existing service keeps 24 hours
  until an operator changes it in the console. A worker that cannot read what it was
  served uses 15 minutes, never longer; a worker served nothing (an older plane) behaves
  exactly as before, 24 hours. `init(offline_execution_s=)` may only *shorten* the
  served window; a larger value is ignored with a warning.
- **What renews it.** Only a policy confirmation: an authenticated hello that the plane
  did not flag as degraded, after which the worker holds the policy the plane announced
  (already, or fetched in that same hello). A hello whose fetch failed, or that carried
  `plane_degraded`, does not.
- **A restart does not renew it.** The deadline survives a restart: the worker resumes
  from the `confirmed_at` in its cache (an older cache without it uses `written_at`),
  never from boot time. A confirmation dated in the future by more than 5 seconds, or a
  wall clock that moved backwards, counts as expired.
- **A call right after a restart can wait, briefly.** The first hello goes out at once but is answered a moment after `init()` returns. After a restart, a consequential call whose cached policy confirmation has expired can wait for the first policy confirmation, within the first three seconds after boot. The call is then evaluated against the policy the worker holds, including restrictions received during that wait. If no valid confirmation arrives, the call remains refused. Waiting and restarting do not renew the offline window. Set `confirm_wait_s=0` to disable the wait; the maximum setting is 10 seconds. Async calls do not block the event loop while waiting. The same wait covers a brand-new worker's first call, before it has received any policy at all (no cache, no answered hello): it waits for the first answer, within the same allowance, and is then judged by the policy that answer brought; with no answer in time it is refused as before. Only an authenticated policy confirmation accepted by the worker renews the window; a merely answered or degraded hello does not, and no tool body runs on the strength of the wait alone. The three seconds are one allowance counted from boot, not a fresh one for every call. The span of a call that waited carries `runbound.waited_ms`.
- **What you can see.** `plane_status().offline_window_s` and `offline_deadline` (an
  estimate: the last confirmation plus the window), and the hello carries
  `offline_window_s` so the console can list each worker's window.

A tool's classes here include a console row's `risk_class` (see
[Per-tool policy](policy.md#console-rows)), so a row can make a `read` tool
`financial` for this purpose. A row never goes the other way: on a tool that
declares no class, a `read`, `write` or `external` row leaves it unclassified,
so it still refuses here. A `plane_unavailable` refusal is
`retryable`, at the `fleet` level, and the body has not run.

In `degraded` everything above still runs. Only two things depend on a live
link, in any mode other than `connected`:

- **Quotas past the lease.** Units already leased keep being spent. Past them,
  every class is refused with `quota_plane_unavailable` while the row's
  `fail_mode` is `closed` (the default), and a take the plane answers with 400 or
  404 counts the same way; `open` admits `limit // 10` calls (at
  least 1) per `control_plane_poll_s` seconds.
- **New approvals.** A new request is refused with `approval_unavailable` ("the
  control plane link is degraded"). A sync approval that is already waiting
  keeps polling and rides the outage out to its own deadline. Approved deferred
  requests only arrive with a hello, and an outcome report that cannot be
  delivered is queued on disk (below) and sent on the next connected hello.

A halt the plane stated holds until the plane lifts it: the plane going quiet,
reporting `fleet_state: "unavailable"`, or a restart of the worker while the plane is down does not release it
(the halt is kept in the policy cache).

## The policy cache

What is kept: the org policy body, its `policy_version` and dry-run flag, the
console rows and their `rows_hash`, `served_at` and `written_at`, plus
`key_id` and `mac`. Never the token, a tool argument or a result. Rules you
declare in code are never in the cache: they come from your code every time.

Where:

1. `init(cache_dir=...)`, else `RUNBOUND_CACHE_DIR`;
2. else the platform cache directory: `~/Library/Caches` on macOS,
   `%LOCALAPPDATA%` on Windows, `XDG_CACHE_HOME` or `~/.cache` elsewhere;
3. else the system temp directory;
4. else memory only, with one warning. `XDG_RUNTIME_DIR` is never used.

The file is `<dir>/runbound/<first 12 hex of sha256(plane_url)>/<service>.json`.
That last directory is created `0700` and the file is `0600`.

How it is written: to a temp file in the same directory, flushed, `fsync`ed and
moved into place with `os.replace` (and the directory `fsync`ed on POSIX). No
locks: a reader sees the old file or the new one. A write never replaces a
cache that holds a newer `policy_version`, so replicas of a service can share
the directory.

When it is written: every time the plane serves a new policy, and on a hello
that confirms the same policy, at most once a minute. So `written_at` means
"last confirmed by the plane". A worker that has never been served a policy
writes nothing, and neither does an answer in which the plane says it could not
read its rows: the worker keeps what it holds and asks again.

When it is read: at `init()`. A restarted worker enforces the cached policy
and rows **before its first hello**; `plane_status().applied_policy_version`
shows the cached version.

The `mac` is `HMAC-SHA256` over the body, keyed from the token. A file that was
edited by hand, truncated, written for another service or with another token is
ignored as if absent, with one warning (`runbound: ignoring the policy cache at
... (edited, truncated or another key's)`). This is tamper-evidence, not a
boundary: anyone who can read the token can compute the MAC.

Next to the cache, `<service>.reports.jsonl` (`0600`) holds deferred-approval
outcome reports the plane could not take yet: an approval id, an outcome, a
worker id and, for an executed call, a digest of its result; never an argument
or a result.

## `plane_status()`

It reads state already held and never opens a socket, so a health endpoint can
call it.

| Field | Meaning |
|---|---|
| `mode` | `local`, `connecting`, `connected`, `degraded` or `unavailable`. |
| `last_contact_age_s` | seconds since the last answered hello; `None` before one. |
| `consecutive_failures` | hellos missed in a row. |
| `reason` | `"heartbeat failures"` after a missed hello, `"fleet state unavailable"` after such a reply, else `None`. |
| `cache_age_s` | seconds since the cache this worker runs on was written; `None` with no cache. |
| `stale` | `True` when the cache is older than 24 hours and the mode is not `connected`. A cache that old at `init()` is also logged once. |
| `applied_policy_version` | the org policy version this worker enforces (from the plane or the cache); `None` if none. |
| `halt_stale_s` | seconds since the last answered hello while a halt is enforced (informational: the halt is enforced regardless); `None` when no halt is. |
| `notice` | the plane's last `notice` text, if any. |
| `offline_window_s` | the offline window this worker applies, in seconds; `None` for a service without enforced defaults (the 24-hour legacy window reads `86400`). |
| `offline_deadline` | an estimate, in epoch seconds, of when consequential execution on the last policy confirmation ends; `None` without a window or a confirmation. |
| `plane_degraded` | what the plane last said is wrong with itself while still answering (`"ledger_unreachable"`: it cannot reach its database); `None` when it said nothing or said it is fine. Informational: no decision changes on it, and `mode` stays `connected`. Each change is one `plane_degraded` entry in `runbound.events()`. |

## Stopping a worker without losing its last spans

A worker stopped by SIGTERM (a container stop, a redeploy) skips Python's exit hooks, so the spans still queued, including any recorded while the plane was away, are lost. Call `runbound.shutdown()` from your SIGTERM handler or a `preStop` hook; it posts what is queued within three seconds and stops the heartbeat. See [OpenTelemetry](opentelemetry.md#delivering-the-queue-before-the-process-ends). `flush` drains to a reachable ingest; spans queued during an outage are lost if the worker exits before it reconnects. There is no disk spool in this release, and the SDK does not claim lossless telemetry.

## A worker that stopped is not a worker that disconnected

A job, a cron task or a short-lived worker that finishes normally tells the plane it stopped (`init(goodbye_on_exit=True)`, the default): the console lists it as **stopped (clean exit)**, not as disconnected, and a stopped worker does not hold a Promote or a Restore waiting for an acknowledgement. If the same worker name starts again it simply reads live. `runbound.shutdown()` says it too. The goodbye is one request of at most 1.5 seconds and never raises; an older plane that does not know it answers 404 and the SDK ignores that.

A worker that **crashes**, is killed (SIGKILL, an out-of-memory kill) or is stopped by a **default SIGTERM** says nothing, because Python skips its exit hooks there: the plane sees it fall silent and shows it as disconnected, which is correct, and it needs the acknowledgement. For a container, call `runbound.shutdown()` in your SIGTERM handler or `preStop` hook (see [OpenTelemetry](opentelemetry.md#delivering-the-queue-before-the-process-ends)) and the stop is recorded as a clean one.

## How to test it

Point a worker at a port where nothing listens, with its own cache directory:

```python
import runbound

runbound.init(
    token="rb_test",
    control_plane_url="http://127.0.0.1:9",  # nothing listens here
    service="checkout",
    cache_dir="/tmp/runbound-cache-test",
)


@runbound.tool(effects={"read"}, reviewed=True)
def lookup_order(order_id: str) -> dict:
    return {"order": order_id}


@runbound.tool(effects={"financial"}, max_calls=1)
def issue_refund(order_id: str) -> str:
    return "refunded"


print(runbound.plane_status().mode)  # connecting
print(lookup_order("A1"))  # a read tool runs
try:
    issue_refund("A1")  # no hello answered and no fresh cache
except runbound.ExecutionRefused as exc:
    print(exc.reason, exc.retryable)  # plane_unavailable True
```

To test the cache itself, run a worker against a real plane until it has
fetched a policy (`applied_policy_version` is set), then look at the file:

```bash
ls -l /tmp/runbound-cache-test/runbound/*/
# -rw-------  ...  checkout.json
```

Stop the plane and start the worker again with the same `token`,
`control_plane_url`, `service` and `cache_dir`:

- `plane_status().mode` is `connecting`, then `degraded` after three missed
  hellos, then `unavailable` 60 s after start;
- `plane_status().applied_policy_version` is the cached version, and the cached
  rules refuse what they refused before, from the first call;
- `read`, `write` and `external` tools keep running; consequential tools run
  only if the cache was confirmed within 15 minutes, and stop with
  `plane_unavailable` once the grace has passed;
- change one byte of `checkout.json` and start again: the cache is ignored with
  one warning, and the worker behaves as if it had none.

`tests/integration/test_plane_loss_real.py` does this against a real plane:
it kills the plane, waits for `degraded`, then for the refusal.

---
