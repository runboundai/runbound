# What happens when something trips — every choice, in one place

[← Docs](../README.md)

Nothing here is decided for you silently. These settings cover every behavior,
each with a documented default, and this table is the whole menu. One thing
holds for every "alerted" below, so it is said once here instead of forty
times: the anomaly reaches your own process first, by the route `on_anomaly`
names — `"raise"` gives your handler a `GuardrailTripped`, `"callback"` calls
your function, `"warn"` writes a WARNING line. Those are alternatives, not a
set: `raise` and `callback` hand the anomaly to your code *instead of* logging
it; only `warn` logs. None of the three ever posted anywhere — that is not
what "alerted" means here. Once you are connected to a control plane, hosted
or self-hosted (see [Fleet mode](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane)),
the same anomaly also reaches it as telemetry, and "alerted" is what happens
next: whether that becomes a Slack message, a page, or a webhook POST is an
[alert route](#alerting) you configure on your runbound dashboard, not a
keyword on this call.

| Setting | Options | Default | What each option does |
|---|---|---|---|
| `on_anomaly` | `"warn"` / `"raise"` / `"callback"` | `"warn"` | The reaction to a critical anomaly. **warn**: log a warning, the agent keeps running. **raise**: raise `GuardrailTripped` on the agent's thread (`exc.anomaly` is the full `Anomaly`; `try/finally` still runs). **callback**: call your `callback(anomaly)` — your own kill switch; if it raises, that is logged and swallowed. |
| `on_trip` | `"latch"` / `"once"` | `"latch"` | What a critical trip does to the session **afterwards**. **latch**: the session stays stopped — every later call is refused (raise / callback again, no re-alert), and under `"raise"` even entering `session(key)` raises, so a blocked key costs zero model calls until `clear()` or `latch_ttl_seconds`. **once**: stop that one call only; the next call is evaluated afresh (a caught exception lets the caller continue — pick this only if you handle blocking yourself). |
| `latch_ttl_seconds` | `None` / seconds | `None` | Only matters with `on_trip="latch"`. **None**: the latch is permanent until `clear()`. **A number**: the latch expires that many seconds after it was set — every detector is re-armed and the session's next event is judged fresh, on the same cumulative counters. This **re-admits, it does not reset**: a session still over budget re-trips immediately, with the same detector; only `clear()` zeroes the counters themselves. Opt-in — nothing expires unless you set it. |
| `on_spike` | `"notify"` / `"trip"` / `"limit"` | `"notify"` | What a *confirmed* spike does (a first spike is always notify-only). **notify**: log and alert, never stop — thinking mode alone is not an incident. **trip**: treat it as critical and follow `on_anomaly` / `on_trip`. **limit**: climb [the spike ladder](../guides/spike-detection.md#many-callers-behind-one-service-on_spikelimit) instead of slamming the door — a confirmed spike costs the session an allowance of `spike_limit_calls` abnormal calls, and only an exhausted allowance closes it, with a cooldown and a strike (requires `on_trip="latch"`, which enforces the cooldown). Explicit hard caps (`max_call_seconds`, `max_tokens_out_per_call`) always trip regardless — you set that number on purpose. |
| `on_loop` | `None` / `"break"` / `"throttle"` / `"escalate"` | `None` | The reaction to a loop only (details [below](#when-a-loop-is-detected)). **None**: follow `on_anomaly`. **break**: raise immediately, whatever `on_anomaly` says. **throttle**: sleep before each repeat, never raise — a blocking `time.sleep()` on a sync call, and under a running event loop the engine hands the delay to the async wrapper instead, which `await asyncio.sleep()`s it, so the loop is never blocked either way. **escalate**: warn first, raise at `loop_hard_threshold`. |
| `on_provider_failure` | `"notify"` / `"open"` | `"notify"` | What a provider that keeps failing does to your calls. **notify**: count the failures and alert once when `circuit_failure_threshold` of them land inside `circuit_window_seconds` — nothing is ever blocked. **open**: also refuse calls — the wrapped client raises `CircuitOpen` (a `GuardrailTripped`, with `.provider`) **before** touching the provider for `circuit_cooldown_seconds`, then lets exactly one probe through; a successful probe closes the circuit. Your app catches it and picks its own fallback — [we never route](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). The circuit is per provider and process-wide, so it stops nobody's session and latches nothing. |
| `max_active_sessions`, `max_session_depth`, `max_child_sessions` | `None` / a number | `None` | The fan-out limits. A `session()` block that would take the run past one of them raises `GuardrailTripped` (detector `fanout`) **at the door, before its body runs**. **This row ignores `on_anomaly`** — like the per-call caps, these are numbers you stated — and it **latches nothing**: what was wrong is the shape of the run, not this key, so the next block is judged on its own. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |
| `max_inflight_calls` | `None` / a number | `None` | How many calls to one endpoint may be in flight at once. The call that would take a provider label past it raises `GuardrailTripped` (detector `inflight`) **before the request goes out**. **This row ignores `on_anomaly`** — the number is one you stated — and **latches nothing**: the moment a slot frees up the next call goes through. Alerted once per endpoint. See [Self-hosted models](../guides/self-hosted-models.md#self-hosted-models). |
| `budget_admission`, `admission_output_tokens` | `bool`, `int` | `False`, `1024` | **Opt-in.** When `True`, `Engine.admit` also refuses a call **before it goes out** whose *estimated* cost would push `budget_usd` past its limit — `GuardrailTripped` (detector `budget`, `details["rule"] == "admission"`). **This row ignores `on_anomaly`** and **never latches**: an estimate is not a wall, and a cheaper call may still fit. An unknown model skips the estimate (warned once per model) rather than refuse a cost nobody stated. Alerted once per session. `admission_output_tokens` is the assumed output size when a request states no `max_tokens` / `max_completion_tokens` / `max_output_tokens` cap. With `budget_admission` left off, nothing here runs. See [Admission](detectors.md#admission-an-opt-in-pre-call-budget-check-budget_admission). |
| `max_steps`, `max_events` | `None` / a number | `None` | Unlike the two rows above, both are ordinary detectors (`steps`, `events`): `critical`, follow `on_anomaly` and `on_trip` like `budget` does. **They count different things, not the same thing at two thresholds**: `max_steps` counts model turns (`llm_call` events) only, `max_events` counts every recorded event. Set both if you want an independent wall on each. See [max_steps and max_events](detectors.md#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `max_session_seconds`, `max_session_lifetime_seconds` | `None` / seconds | `None` | The two wall clocks. Also ordinary detectors (`timeout`), like the row above: `critical`, follows `on_anomaly` and `on_trip` like `budget` does. `max_session_seconds` measures the *run* — reset on every `session(key)` entry — and fires with `details["scope"] == "run"`; `max_session_lifetime_seconds` measures since the session's first-ever creation and fires with `details["scope"] == "lifetime"`. Each fires once per session, independently of the other. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |
| `on_halt` | `"raise"` / `"warn"` | `"raise"` | What an org-wide halt from [the control plane](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane) does to this worker. **raise**: every guarded `session()` block is refused **at the door** with `GuardrailTripped` (detector `halt`) — that is what a kill switch is for. **warn**: nothing is refused; the halt is logged at most once a minute, so you can prove the switch reaches your workers before you let it stop them. **This row ignores `on_anomaly`** and **latches nothing**: by default the halt lifts by itself 60 s after the last contact with the plane — see `stale_halt` below for the other choice. |
| `stale_halt` | `"release"` / `"hold"` | `"release"` | What an **enforced** halt does while the plane link itself goes degraded (not the same question as whether to enforce a halt at all — that is `on_halt`). **release**: the halt stops being enforced 60 s after the last successful contact with the plane, so a dead plane cannot keep a fleet stopped forever. **hold**: the halt stays enforced past that window, until a heartbeat explicitly says otherwise — pick this when a false "all clear" costs you more than a stuck kill switch. `plane_status().halt_stale_s` reports how long a currently-enforced halt has been stale. |
| `on_plane_loss` | `"guard_locally"` / `"refuse"` | `"guard_locally"` | What entering a `session()` block does when the plane could not answer the entry question at all (timeout, error, a degraded link with no fresh cached decision) — a different moment from an *answered* refusal, which is always honored regardless of this setting. **guard_locally**: fall back to local detection alone, today's behavior. **refuse**: refuse the entry itself (detector `plane`, `GuardrailTripped`) rather than guess — latches nothing, costs no strike, and the very next entry asks the plane again. An invalid token is a configuration error, not plane loss, and guards locally under **both** settings (logged at most once a minute) — this option is only about a plane that could not be reached or answer, not one that rejected your credentials. |
| Fleet budget (`budget_usd` with a control plane) | — | as `budget_usd` | Nothing extra to configure. In fleet mode the entry answer carries what the rest of the fleet has already spent under this key, and the `budget` detector counts local spend **plus** that offset — so the reaction is exactly the `budget` reaction (`on_anomaly`, then `on_trip`), and the worker trips on the same turn a single worker with those numbers would. The anomaly says where the money went: `details["fleet_spend_offset_usd"]`. Same for `max_total_tokens` and `details["fleet_tokens_offset"]`. |
| Remote latch | — | always on with a plane | A latch **another worker** set is adopted here as if this worker had set it, with whatever is left of the fleet's ttl as this session's expiry and `details["origin"] == "fleet"` on the anomaly. From there it behaves like any local latch: under `on_anomaly="raise"` even *entering* `session(key)` raises, and under `"warn"` / `"callback"` the block runs and the reaction is re-applied on its first event. `is_tripped(key)` reports the real reason — the other worker's — and `runbound.clear(key)` clears it everywhere, not just here. |
| Org policy `dry_run` | set by the plane, not by you | off | An org [action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do) the plane marks `dry_run` is **logged and alerted and never blocks**: the anomaly is `warn`-severity, reads "Policy dry-run: would block tool …", and carries `details["dry_run"] is True`. It is how a platform team rolls a rule out across a fleet. **Your local `tool_policy` rules are untouched by it** and keep blocking exactly as they did. |
| Fleet circuit | follows `on_provider_failure` | `"notify"` | The plane can open or close a provider circuit on **every** worker at once, so one outage is discovered once for the fleet. What an open circuit *does* here is unchanged and still yours: **notify** alerts and lets every call through, **open** raises `CircuitOpen` before the call. Like a local circuit, it stops no session and latches nothing. |
| `tool_policy.on_violation` | `"block"` / `"block_and_latch"` / `"dry_run"` | `"block"` | What a tool call that breaks [your action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do) does. **block**: refuse that one call — `PolicyViolation` (a `GuardrailTripped`) is raised on the agent's thread, the tool body never runs, and the session keeps going. **block_and_latch**: refuse it *and* stop the session, honoring `on_trip` (under `"once"`, only that call). **dry_run**: let the call run, and log and alert what would have been refused — how you roll a policy out. **This row ignores `on_anomaly`**: the rule is one you stated about your own agent, so it is enforced whether or not detectors are set to stop anything. |

Which detectors can latch a session: `budget`, `steps`, `events`, `error_storm`,
`timeout`, a `loop` under `"break"` / escalate-critical, a hard cap, a `spike`
only under `on_spike="trip"` or the ladder's `on_spike="limit"` (where the latch
is what serves the cooldown), and `policy` under
`on_violation="block_and_latch"`. `velocity` is warn-severity and never stops
anything, and a `fanout` refusal, an `inflight` refusal, a fleet `halt` and an
open `circuit` stop the block and the call in front of them without latching the
session at all. A **remote** latch is the one exception that arrives from
outside: it is a latch another worker made and this one adopts, ttl included.

**The plane can also refuse a session in its own words, with no local anomaly
behind it at all** — an org daily budget already spent, or an entry refused
under `on_plane_loss="refuse"`. Until Wave 24 the SDK only read the *facts* a
plane decision carried (spend offsets, strikes, a latch) and never its
`allow` field, so a plane that said "no" was silently overruled by the worker
and the call went out anyway. It no longer is: a refused decision is honored
at the door, before any provider call. The two cases carry different
`detector`s, both with `details["origin"] == "plane"`: an entry refused
because the plane could not be reached at all (`on_plane_loss="refuse"`) is
detector `plane`, and resolves against the `"plane"` key in a
[refusals profile](#what-the-caller-sees--your-words-your-status) (built-in
fallback: HTTP 503, "The assistant is temporarily unavailable."); an org daily
budget already spent is detector `budget` with `details["rule"] ==
"org_budget"` — it is a budget like any other, just decided by the plane —
and resolves against the `"budget"` key (built-in fallback: the `"default"`
profile, HTTP 429, unless you set one). Either way — like a remote latch — it
latches nothing extra and costs no local strike; if the decision also carries
a latch (a real fleet fact, `origin="fleet"`), that is adopted first and
behaves exactly like the remote-latch row above.

`on_anomaly="callback"` requires a `callback`, and setting a `callback` without
it is rejected at `init()` — a kill switch that would never be called is a
configuration bug, not a warning.

Only the most severe anomaly drives the reaction (`critical` over `warn`), but
**every** anomaly is alerted before any reaction runs, so an escalation is never
lost to the exception that stops the agent. Alerts go out once per session per
detector per severity, never on a latched session's re-refusals — and a policy
refusal keys on the rule and the tool too, so a second forbidden tool is news
while the same refusal on every retry is not.

## When a loop is detected

A loop is the one failure mode where stopping the run is not always what you
want — sometimes the agent is repeating itself but is still making progress, and
sometimes you would rather slow it down than kill it. `on_loop` sets the
reaction for the `loop` detector only; every other detector keeps following
`on_anomaly`.

| `on_loop` | What happens on a repeat |
|---|---|
| `None` (default) | Nothing changes: the loop follows `on_anomaly`, and fires once per session. |
| `"break"` | Raises `GuardrailTripped` as soon as the loop threshold is reached, even if `on_anomaly` is `"warn"`. Use it when a loop is always a bug. |
| `"throttle"` | Sleeps before the repeated tool runs, doubling from `throttle_base_seconds` on each further repeat, up to `throttle_max_seconds`. Never raises. On a sync call this is a blocking `time.sleep()` on the agent's thread. Under a running event loop the sleep is instead `await`ed by the async tool wrapper (and, for a model-requested loop, by the async client wrapper) before the body runs — the delay travels through a `contextvar` from wherever the engine decided it, so the event loop is never blocked either way — except a synchronous tool called directly on the event-loop thread, which has nothing to await with and is not throttled. Use it to stop a burn while the run finishes. |
| `"escalate"` | Logs a warning on each repeat, then raises `GuardrailTripped` once the count reaches `loop_hard_threshold` (default `2 * loop_threshold`). Use it when a few repeats are normal and many are not. |

```python
runbound.init(loop_threshold=3, on_loop="escalate",
                loop_hard_threshold=8,
                on_anomaly="warn")   # still applies to budget, velocity, steps
```

## Tools that are supposed to repeat

Some tools are *meant* to run identically, over and over — polling a job until
it finishes, checking a status, refreshing a token. That is not a loop; it is
the tool doing its job, and without an exemption it would trip the loop
detector on schedule.

```python
@runbound.tool(repeatable=True)
def poll_job_status(job_id: str) -> str:
    ...
```

`repeatable=True` (equivalently, listing the tool's name in
`loop_ignore_tools` at `init()`, for a tool you cannot decorate) marks the
call `loop_exempt` for both the executed (`tool_call`) and the
model-requested (`tool_request`) event, so it never feeds the loop window —
whoever runs it. It still counts everywhere else: `runbound.tool_calls()`
tallies it and any `max_calls` in your [action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do)
still enforces its cap. Use it to mark polling, not to tune a real loop away —
a tool that is actually stuck still needs `max_calls` or a wall clock to stop
it.

Under `"throttle"` and `"escalate"` the loop detector reports on every repeat
rather than once, because the reaction has to be applied every time. Your
observers — and, once connected, the plane — are still told only once per
session per detector, so a throttled loop does not turn into a notification
storm.

## Alerting

Slack, PagerDuty, Opsgenie and a signed webhook are not SDK code (0.3.0):
`runbound/alerts.py` sends none of them. What it keeps is the receiver-side
signature check (below) and the bookkeeping that lets outbound telemetry
threads drain at process exit — nothing here builds an alert body or opens a
socket to Slack, PagerDuty or anyone else any more. The division of labour:
*the SDK detects, stops, refuses and reports; the plane routes and delivers.*
`on_anomaly="callback"` is the free, forever answer for anyone who wants to
notify themselves without a token or a plane at all — it hands the anomaly to
your own function, in your own process.

Once a plane is connected — hosted (a bare `token`) or self-hosted
(`control_plane_url`, `token=""` if it needs no auth) — configure where
anomalies go as an alert route on your runbound dashboard: routing by
severity, detector or service, fleet-wide dedup, delivery history and a Test
button against the real adapter, none of which a truthiness check running
inside your own process could ever provide. Which adapters a route may use
is gated by plan and enforced with a 403, not a suggestion — see
[the control plane docs](https://runbound.co/docs/control-plane) for
the endpoints, the adapter list, and the webhook body your receiver gets.

The plane's webhook adapter matches what used to be sent from here in
everything that signs and verifies a delivery: same `version`, same
envelope, same `X-Runbound-Timestamp` / `X-Runbound-Signature` headers,
same signing string. Two fields in the body did change: `session.id` used to
be this process's own session id and is now the sha256 key hash — the plane
has no notion of a per-process id, since any worker can serve the same
session — and `session.key` (what `send_session_keys=True` used to add
straight to the body) does not exist here at all; a raw key now reaches you,
if you opt in, only through your own `link_template`. If your receiver keyed
on `session.id` as an opaque per-process value or read `session.key`, update
it; the signature check itself is unchanged — verify a delivery the same way
you always did:

```python
from runbound import verify_webhook_signature

@app.post("/hooks/runbound")
def hook():
    body = request.get_data()          # the raw bytes, unparsed
    if not verify_webhook_signature(SECRET,
                                    request.headers["X-Runbound-Timestamp"],
                                    body,
                                    request.headers["X-Runbound-Signature"]):
        return "bad signature", 400
    ...
```

It compares in constant time and rejects a timestamp more than five minutes from
your clock in either direction, so a captured delivery cannot be replayed later.
It returns `False` rather than raising for anything malformed — a timestamp that
is not a number, a missing header, a body that has been touched. Verify the
*raw* body: re-serializing the parsed JSON will not reproduce the bytes that
were signed.

## What the caller sees — your words, your status

runbound raises `GuardrailTripped` — it never writes the sentence your caller
reads. Without this, every app ends up inventing its own HTTP status and
copy for a refusal, one `except` block at a time, and a customer who wants to
change either has to ship code. `exc.refusal` fixes that: it carries the
status and the message *you* set, resolved fresh on every access.

```python
runbound.init(refusals={
    "default": {"status": 429, "message": "This assistant can't continue this conversation right now. Please try again later."},
    "budget":  {"status": 402, "message": "This conversation is over today's spending limit."},
    "policy":  {"status": 403, "message": "That action isn't allowed."},
})
```

A profile is a dict of `{key: {"status": int, "message": str}}`. Keys are
`"default"` or a detector name exactly as the engine emits it — grepping
`detector=` across the package turns up `budget`, `loop`, `spike`,
`velocity`, `steps`, `events`, `error_storm`, `timeout`, `fanout` and `inflight`, plus
`policy` (`PolicyViolation`), `circuit` (`CircuitOpen`), `halt` (an org-wide
halt), `plane` (an entry refused under `on_plane_loss="refuse"`) and `fleet` (a latch relayed from another worker whose own detector
could not be read). **Per-call hard caps** (`max_call_seconds`,
`max_tokens_out_per_call`, `max_cost_per_call_usd`) report through `spike` —
there is no `cap` key. Either field of an entry may be omitted; a missing
`status` or `message` falls through to `"default"`, then to `BUILTIN` below.
`message` may contain `{retry_after_s}` and `{detector}`; formatting is safe
by construction — an unknown placeholder is left verbatim and a malformed
template is returned unformatted, never raised.

Precedence, highest first, checked **field by field** — a plane profile that
only overrides `status` does not blank out a local `message`:

1. the plane's profile (org merged under service), this detector's entry
2. the plane's profile, its `"default"` entry
3. the local profile (`GuardrailConfig.refusals`), this detector's entry
4. the local profile, its `"default"` entry
5. `BUILTIN`

`BUILTIN` — the fallback with no configuration at all:

| Key | Status | Message |
|---|---|---|
| `default` | 429 | This assistant can't continue this conversation right now. Please try again later. |
| `policy` | 403 | That action isn't allowed. |
| `circuit` | 503 | The assistant is temporarily unavailable. |
| `halt` | 503 | The assistant is paused for maintenance. |

`exc.refusal.headers` is `{"Retry-After": "<seconds>"}`, rounded up, whenever
the anomaly's session is under a latch or cooldown with a known remaining
time, else `{}`. `exc.refusal.body()` is `{"refused": True, "detector": ...,
"message": ..., "retry_after_s": ...}` — everything a handler needs to answer
with directly:

```python
# FastAPI
except GuardrailTripped as exc:
    r = exc.refusal
    return JSONResponse(status_code=r.status, headers=r.headers,
                        content={**r.body(), "reply": r.message})
```

```python
# Flask
except GuardrailTripped as exc:
    r = exc.refusal
    resp = jsonify({**r.body(), "reply": r.message})
    resp.status_code = r.status
    resp.headers.update(r.headers)
    return resp
```

**Fleet mode:** set a profile once, on the plane, through the admin API
(`PUT /v1/admin/refusals` org-wide, `PUT /v1/admin/services/{service}/refusals`
per service — see [the control plane docs](https://runbound.co/docs/control-plane))
and the change reaches every worker within `control_plane_poll_s`, no
redeploy. The last profile a worker saw survives a plane outage, exactly
like a policy rollout. `coverage()["refusals"]` reports which tier is
currently answering — `"plane"`, `"local"`, or `"default"`.

---
