# Fleet mode — one truth across all your workers (control plane)

[← Docs](../README.md)

Everything above happens inside one process. That is exactly right until you
run more than one of them — and then every number quietly becomes a number
*per replica*:

- **The budget multiplies.** `budget_usd=5.0` across eight workers is a $40
  budget. The key you meant to cut off at $5 gets $5 from each of them.
- **A blocked key comes back on another worker.** A latch lives in the process
  that set it. The repeat-offender key you stopped hits the load balancer
  again, lands on a replica that has never heard of it, and carries on.
- **Nothing learned survives.** Strikes and cooldowns, `max_calls` tallies,
  provider circuits and fan-out counters are each rediscovered N times over,
  and forgotten at the next deploy.

**Fleet mode gives the fleet one memory.** Point the SDK at a control plane and
every worker asks the same question at the same two moments — a session opens,
a session closes — so the eight replicas serving `user:42` share one budget,
one latch, one strike count, one org policy and one set of provider circuits.
Detection itself does not move: the detectors still run in your process, on
your thread, with no model calls, exactly as they do now. The plane only tells
each worker what the *other* workers already know.

Those five are the whole list, and the list is honest: spike baselines, the
`max_calls` tally and the fan-out / in-flight counters are still per worker —
see [the limitations](../reference/guarantees.md#guarantees-and-limitations).

```python
import os
runbound.init(token=os.environ["RUNBOUND_TOKEN"],
                service="support-bot", budget_usd=5.0, on_anomaly="raise")
```

That is the whole change for the hosted case: a non-empty `token`, with no
`control_plane_url` anywhere — not passed, and not in `RUNBOUND_PLANE_URL`
— is `plane_mode == "hosted"` once the hosted plane is live, and you never have
to name `control_plane_url` yourself; until then a bare token logs one WARNING
and the process stays local (`plane_mode == "off"`). Set `control_plane_url` too, from either source, and that is
`"self_hosted"` instead, not hosted — it always needs a token, and
`token=""` is how you state a self-hosted plane with no auth, since an empty
token never invents a url. `service` names the fleet this process belongs
to; `worker_id` names this process inside it and defaults to
`hostname:pid:xxxxxx` (six random hex characters, so two replicas on one
host never share an id).

## When the plane is contacted — and when it is not

| Moment | What goes out | What it costs your request |
|---|---|---|
| `init()`, then every `control_plane_poll_s` (default **5 s**) | a heartbeat: service, worker id, SDK version, the policy version this worker has, its circuit states, its open session count | nothing — a daemon thread |
| **entering** a `runbound.session()` block | one entry question, on your thread | at most `control_plane_timeout_s` (default **150 ms**), and only on a cache miss: one key's answer is reused for `control_plane_cache_s` (default **5 s**), so a retry loop of blocks costs one request rather than a hundred |
| **leaving** a `runbound.session()` block | this block's spend / token / step delta, queued | nothing — a dict copy; a background thread posts it in batches |
| a **critical trip** | the trip, on your thread, so the other workers refuse this key on *their* next request | at most `control_plane_timeout_s`; a trip that does not get through falls back to the batch queue, ahead of ordinary events |
| a provider **circuit** opening or closing | queued on the priority lane | nothing |

**Never per model call, and never per tool call.** An auto-wrapped client, a
`wrap()`ped client, `@runbound.tool`, `@runbound.llm` and `record_call()`
open no sockets at all — at most they append to an in-memory queue. A run that
makes a thousand model calls inside one `session()` block talks to the plane
exactly once, and there is a test that asserts it with a spy on the client.

**A plane that is down changes nothing about your guarding.** Every call to it
is bounded, caught, and answered locally on failure:

- one warning a minute, not one per session;
- after **3 failed calls in a row** the link is *degraded* and stops calling
  altogether, spending one probe every **30 s** to find out when the plane is
  back;
- `runbound.plane_status().mode` reads `"degraded"`, so a health endpoint can
  say so out loud;
- a token the plane rejects (401/403) is terminal until the process is
  reconfigured — a bad token costs one request, not one per session;
- every detector, latch, cap and policy you configured locally keeps working —
  the single-process behavior the rest of this README describes.

Even the heaviest instruction a plane can give fails open: a fleet-wide halt is
enforced only while we are still hearing it, and stops being enforced **60
seconds** after the last successful contact. A control plane that dies must not
take the fleet down with it.

```python
# an unreachable plane, on purpose
runbound.init(control_plane_url="http://127.0.0.1:9", token="rb_live_x",
                budget_usd=0.01, on_anomaly="raise")

with runbound.session("user:1"):
    runbound.record_call("gpt-4o", tokens_in=5000, tokens_out=5000)
# GuardrailTripped: Budget exceeded: $0.0625 spent, limit $0.0100

runbound.plane_status()
# PlaneStatus(mode='degraded', last_contact_age_s=None, consecutive_failures=3, notice=None)
```

## What fleet mode adds

- **A bounded fleet budget, not a hard one.** The entry answer carries what
  the rest of the fleet has already spent under this key; the SDK folds it in
  as an offset, and the `budget` detector counts local spend **plus** that
  offset. In a single process the cap is exact — the check is a plain
  comparison against a running total. Across a fleet it is a *bound*: a
  **turn** is one `runbound.session()` block on one worker, entry to exit.
  A worker learns the fleet's spend only at block entry and reuses that
  answer for `control_plane_cache_s` (5 s default); other workers' spend
  reaches the plane through exit deltas posted in batches (about one
  second), so a worker can be stale for up to the cache window plus that
  batch latency. Worst-case overspend for one key is the sum over workers of
  (the spend of that worker's one in-flight block, plus anything it admitted
  while stale) — the full statement and the tests that assert it live in
  [INVARIANTS.md](../../INVARIANTS.md#budget). Keep blocks short (one request per
  block) and lower `control_plane_cache_s` to tighten the bound, at the cost
  of more entry requests. The worker that crosses the line trips
  on the same turn a single worker with those numbers would; the anomaly
  says where the money went: `details["fleet_spend_offset_usd"]` and
  `["fleet_tokens_offset"]`. `max_total_tokens` works the same way.
- **A shared latch and shared strikes.** A trip is reported synchronously, and
  the next worker to open that key adopts the latch as if it had set it itself —
  **within `control_plane_cache_s` (5 s) plus one turn**: a worker holding a
  cached answer for that key finishes the turn it is on, and the entry after
  the cache expires is the one that is refused. That window is the knob; lower
  it to tighten propagation at the cost of more entry requests.
  The latch arrives with what is left of its ttl, so the key is let back in
  when the *fleet's* cooldown runs out rather than when one worker happened to
  hear about it. Under `on_anomaly="raise"` that means the block is
  refused before it runs, which is what makes a blocked key cost zero model
  calls fleet-wide. The spike ladder's strike count travels with it, so a
  rollover earned on worker 3 tightens the allowance on worker 7.
- **Org-wide action policy.** The plane states one rule set per `service`, and
  the SDK merges it with your local `tool_policy` **most-restrictive-wins**:
  bans unioned, allow-lists intersected, `max_calls` the lower of the two —
  rule by rule in [the merge algebra](policy.md#an-org-policy-merged-with-yours-the-algebra).
  Callables never come off the wire, so your `constraints` and
  `approval_callback` stay exactly as you wrote them. A violation of an org rule
  reports `details["origin"] == "org"`. A new version is picked up on the
  heartbeat, on the poller's thread, with no redeploy.
- **Dry-run rollout.** An org policy the plane marks `dry_run` is logged and
  alerted and **never blocks** — how a platform team rolls a rule out across a
  fleet before turning it on. Your local rules are untouched by it and keep
  blocking.
- **Fleet circuits.** The heartbeat can open or close a provider circuit on
  every worker at once, so one provider outage is discovered once for the fleet
  instead of N times. What an open circuit *does* is still your
  `on_provider_failure`: `"notify"` alerts and lets calls through, `"open"`
  raises `CircuitOpen` before the call.
- **A fleet kill switch.** The plane can halt everything. `on_halt="raise"` (the
  default) refuses every guarded `session()` block with a `GuardrailTripped`
  carrying detector `halt`; `on_halt="warn"` lets the business run and says so
  at most once a minute — which is what you want while you are still proving the
  switch reaches your workers.

## What we send — hashes and counts, never content

Fleet mode is the first thing in runbound that opens a socket we own, so here
is all of it. Every outbound record is built from one module,
`runbound/plane_types.py`, which is what makes this table checkable by reading
a single file.

| Endpoint | When | Fields |
|---|---|---|
| `POST /v1/hello` | every `control_plane_poll_s` | `service`, `worker_id`, `sdk_version`, `policy_version_seen`, `circuits` (`{label: "open"\|"half_open"\|"closed"}`), `active` (open `session()` blocks), `coverage` (the counts `runbound.coverage()` shows), `tools_hash`, and `tools` — the tool report — only when that hash changed |
| `POST /v1/enter` | a `session()` block opens, on a cache miss | `key_hash`, `tags`, `service`, `worker_id`, `budget_usd`, `local_spend_usd`, `local_total_tokens` |
| `POST /v1/trip` | a critical trip latches a session, and every block refused at the door because a key is latched | `key_hash`, the anomaly (`ts_wall`, `key_hash`, `detector`, `severity`, `message`, scrubbed `details`, `reacted`, `anomaly_id`), `latch_ttl_s`, `strikes`, `generation`, `refused_at_door`, `anomaly_id`, and a `worker_id` the SDK leaves empty |
| `POST /v1/events` | batched in the background; the `exits` and `circuits` lanes always, the `events` and `anomalies` lanes while `export_events` is on | `service`, `worker_id`, `sent_at`, `dropped`, and four lanes — `events` (`ts_wall`, `kind`, `key_hash`, `step`, `tokens_in` / `tokens_out` / `tokens_reasoning`, `cost_usd`, `model`, `tool_name`, `args_hash`, `duration_s`, `error_class`, `priced`, `partial`, `tokens_estimated`), `anomalies`, `exits` (`key_hash`, `seq`, `spend_delta_usd`, `tokens_delta`, `steps_delta`, `tool_calls`, `events_delta`, `errors_delta`, `tokens_cached_delta`, `last_detector`, `trigger_message`, `trigger_age_s`), `circuits` (`label`, `state`, `failures`, `cooldown_s`) |
| `GET /v1/policy?service=…` | the heartbeat announced a new policy version | nothing but the service name |
| `POST /v1/clear` | `runbound.clear(key)` | `key_hash` |

The **tool report** is the one record built from your code rather than from
your traffic: for every `@runbound.tool` in the process, its name, its
parameters' names, each annotation rendered as a string, whether each parameter
has a default (never *what* the default is), the first line of its docstring,
its defining module and the `rules` its decorator stated — plus a
`decorated: false` entry for every tool name a model asked for that no
decorator declared, which is exactly the coverage gap worth seeing. Read it
yourself with `runbound.tools()`; it is the same list the heartbeat carries.

```python
[{"name": "issue_refund",
  "decorated": True,
  "params": [{"name": "user", "annotation": "str", "required": True},
             {"name": "amount", "annotation": "float", "required": True}],
  "doc": "Refund a customer.",
  "module": "acme.tools",
  "rules": {"max_calls": 1, "constraint": "acme.tools:under_500"}}]
```

A tool that states no rule reports `"rules": {}`. A `constraint` or
`require_approval` travels as `"module:qualname"` and **never as the function
itself**: your predicates run in your process, are never shipped, and are never
called anywhere else.

`tools_hash` — 16 hex characters of a sha256 over that list — rides every
heartbeat; the list itself rides only when the hash changed, or when the plane
answers that it has none for this worker. A deploy costs one payload; the five
seconds after it cost a hash. At most 500 tools are reported, sorted by name.

An `exits` entry is a delta — what one `session()` block added since this
worker's last report for that key — and its numbers keep the meaning stated
elsewhere in this README: `steps_delta` is model turns, `events_delta` the
raw count of everything recorded (a tool call included), `errors_delta` how
many `llm_error`/`tool_error` events, `tokens_cached_delta` how many of
`tokens_delta` were served from a provider's cache. `last_detector`,
`trigger_message` and `trigger_age_s` describe the anomaly that last stopped
the session — all three `None` for a session that never tripped, which is
every session under `on_anomaly="warn"`. `trigger_age_s` is this worker's own
monotonic clock, in seconds, because its clock and the plane's are not the
same one; the plane converts it to a timestamp on arrival, on its own clock.

What is **never** on that wire, because there is no field for it to travel in:

- **prompts and replies** — not read, not stored, not sent;
- **defaults, return values and the rest of a docstring** — the tool report says
  a parameter *has* a default, never its value, and carries a docstring's first
  line and nothing after it: the lines after the summary are where hostnames,
  credentials and customer examples live;
- **tool arguments** — only `args_hash`, the same salted sha256 digest the loop
  detector compares. The salt is a random value generated once per process, so
  the digest is an *equality token* good for spotting a repeat inside this
  process — not a fingerprint: the same call hashes differently in every
  process, on purpose, so two workers (or a leaked ledger) cannot correlate
  "who called what" by matching hashes across the fleet;
- **error messages** — only `error_class`, and only when the message *names* a
  class (`"RateLimitError: …"` → `RateLimitError`). A message with no such
  prefix sends `None` rather than a guess, because a guess would be a piece of
  the message, and the message may hold anything;
- **your session keys** — a key travels as `sha256(key)` and nothing else,
  unless you opt in with `send_session_keys=True`, which adds the raw key
  alongside the hash. Off by default. Detectors *do* name the key in their own
  `message` and `details["key"]`, because a local log with the key in it is the
  useful one — so the last thing done to an outbound anomaly, on the plane
  wire, is to replace every occurrence of the key with the first 12 characters
  of its hash and an ellipsis: `"3f2a9c1b04d7…"`. Two records about the same
  key still line up; neither carries the key itself — as long as
  `send_session_keys` is off. An `exits` entry's `trigger_message` is the same
  kind of sentence (it is, verbatim, the `message` of the anomaly that
  latched the session) and gets the same treatment before it leaves. Turn
  `send_session_keys` on and that redaction is skipped too: a detector's
  `message` and `details` — and an exit's `trigger_message` — now travel
  exactly as it was written, and the plane stores the raw key next to its
  hash. From there the plane's own alert adapters (Slack, PagerDuty, a
  webhook) do what you told them to: read the raw key into your own
  `link_template` wherever you wrote `{key}`, and pass a detector's
  unredacted `message`/`details` straight through like everything else in an
  alert. That is your choice about your own Slack, your own PagerDuty and
  your own endpoint — the hash is what ships unless you make it otherwise.

Your `tags` are the exception, and deliberately so: they are labels you chose,
so they travel exactly as you wrote them and are never redacted. Don't put a
raw key in a tag.

`tags` **do** travel, so a dashboard can group by them: at most 32 entries, keys
and values stringified and cut to 64 characters — label sessions with something
you are willing to see in a console. A detector's `details` dict is scrubbed to
JSON scalars: strings truncated to 256 characters, and anything that is not a
string, number, bool, `None`, list or dict is *dropped* rather than
stringified, because stringifying it is how content escapes. `link_template`
— the "open this session" link an alert carries — is not a setting here at
all any more (0.3.0): it is a per-service field on your runbound
dashboard, rendered by the plane when it builds a delivery, not by the SDK.

Telemetry is lossy on purpose: each lane is a bounded queue, the oldest record
is dropped when it is full, and `dropped` rides along on every batch as a
cumulative counter, so a plane that is down for an hour costs a fixed amount of
memory rather than an OOM. At interpreter exit the queue is drained once.
`export_events=False` turns the telemetry lanes off — no events and no
anomalies leave the process. **The fleet state still flows**: the exit deltas,
the circuit transitions and the trips travel on the same batches, because they
are how this worker's spend reaches the fleet's total, not telemetry about it.
A shared budget works the same with telemetry off.

## Reading the link

```python
runbound.plane_status()
# PlaneStatus(mode='connected', last_contact_age_s=1.2, consecutive_failures=0,
#             notice=None, entitlements={'plan': 'team', 'limits': {...}, 'denied': []})

runbound.fleet_status("user:42")
# {'fleet_spend_usd': 4.9, 'fleet_tokens': 900000, 'strikes': 1, 'generation': 3,
#  'halt': False, 'latched': True, 'policy_version': 4, 'age_s': 0.4,
#  'door_refusals': 12}

runbound.key_hash("user:42")
# 'ea3fd43be1e57d62e163dae19fc740bd6d660eec497235fd0ef859e2bd9fa328'
```

`plane_status()` reads `"local"` when no plane is configured (and before
`init()`), `"connected"` while it is answering, `"degraded"` when it is not,
and `"limited"` when the plane is answering but your plan is having entry
decisions made locally. `entitlements` is the plan the plane last stated —
`{"plan", "limits", "denied", "notice"}` — and `notice` is what it wants you to
read. **A plan limit never stops your guarding**: every detector, latch, cap
and policy keeps running exactly as it does with no plane at all, the
heartbeat keeps going, and the trips and exit deltas keep flowing so the
fleet's totals stay right. What a denial can take away is the plane's own
additions — telemetry (`events_denied`, `events_over_cap`) and the shared entry
answer (`workers_synced_exceeded`). The notice is logged at most once an hour.
`fleet_status(key)` is the other half of `session_status(key)`: that one is what
*this* worker knows about one key, this one what the whole fleet does — and
it answers `None` for a key this worker has not opened a block for in the last
few seconds. `door_refusals` is the one local number in it: how many blocks
*this* worker has turned away at the door while the key was latched — what the
latch is saving, which the plane also counts fleet-wide. Both read cached state
and never open a socket, so a health endpoint may poll them. `key_hash(key)` is the digest the plane knows a key by:
the join key between your own logs and anything the plane shows you.

The control plane is Runbound AI's hosted product, currently in early access
(see [pricing](https://runbound.co/pricing)); a self-hosted deployment
of the plane is available as an Enterprise option, licensed separately
rather than built from this repository. The SDK half above is the free,
MIT-licensed part of fleet mode, and it works against any server that
speaks those six endpoints — a real property, not a sales pitch: nothing in
the SDK cares whether the plane behind them is ours.

A human can run and watch the fleet too, wherever the plane is deployed: it
serves an admin dashboard at `/` — fleet overview, sessions, the
refused-actions ledger, policies, kill switch and more, on every plan. A
self-hosted deployment boots from `/setup` using a bootstrap key printed on
first run; the hosted product adds `/signup` once it is live. See
[the control plane docs](https://runbound.co/docs/control-plane) for
roles, CSRF, and the rest of the admin API the dashboard is built on.

---
