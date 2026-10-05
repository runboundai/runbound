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

One more thing holds for every refusal **at the door** below — before a call
or a tool action happens, never a post-call detector trip: **`exc.decision`
is a `Decision`** — the verdict, which boundary of
[the execution envelope](../concepts/how-it-works.md#admit-execute-reconcile-record)
was hit (`"steps"`, `"time"`, `"tokens"`, `"money"`, `"blast_radius"`,
`"posture"`, `"capability"`, `"circuit"`, `"policy"`, `"concurrency"`), the
detector, and the numbers behind it — alongside `exc.anomaly`. It is the same
object on the wire, at `anomaly.details["decision"]`. A post-call trip from
one of [the detectors](detectors.md) (`loop`, the ordinary `budget`,
`steps`, `timeout`, `spike`, `error_storm` walls, once a call already went
out) does not carry one — `exc.decision` is `None` there.

| Setting | Options | Default | What each option does |
|---|---|---|---|
| `on_anomaly` | `"warn"` / `"raise"` / `"callback"` | `"warn"` | The reaction to a critical anomaly. **warn**: log a warning, the agent keeps running. **raise**: raise `GuardrailTripped` on the agent's thread (`exc.anomaly` is the full `Anomaly`, `exc.decision` the `Decision` behind it; `try/finally` still runs). **callback**: call your `callback(anomaly)` — your own kill switch; if it raises, that is logged and swallowed. |
| `on_trip` | `"latch"` / `"once"` | `"latch"` | What a critical trip does to the session **afterwards**. **latch**: the session stays stopped — every later call is refused (raise / callback again, no re-alert), and under `"raise"` even entering `session(key)` raises, so a blocked key costs zero model calls until `clear()` or `latch_ttl_seconds`. **once**: stop that one call only; the next call is evaluated afresh (a caught exception lets the caller continue — pick this only if you handle blocking yourself). |
| `latch_ttl_seconds` | `None` / seconds | `None` | Only matters with `on_trip="latch"`. **None**: the latch is permanent until `clear()`. **A number**: the latch expires that many seconds after it was set — every detector is re-armed and the session's next event is judged fresh, on the same cumulative counters. This **re-admits, it does not reset**: a session still over budget re-trips immediately, with the same detector; only `clear()` zeroes the counters themselves. Opt-in — nothing expires unless you set it. |
| `on_spike` and the ladder's own tuning | `"notify"` / `"trip"` / `"limit"`, plus nine tuning keywords — all free, local `init()` keywords | `"notify"` | What a *confirmed* spike does (a first spike is always notify-only). **notify**: log and alert, never stop — thinking mode alone is not an incident. **trip**: treat it as critical and follow `on_anomaly` / `on_trip`. **limit**: climb [the spike ladder](../guides/spike-detection.md#many-callers-behind-one-service-the-abuse-ladder) instead of slamming the door — a confirmed spike narrows the session to [`restricted`](../guides/policy.md#classify-by-capability) (its riskier tools stop acting, its model calls go on) with an allowance of abnormal model calls, a session that behaves again heals back to `full`, the closed rung sets `stopped`, and only an exhausted allowance closes it, with a cooldown and a strike (requires `on_trip="latch"`, which enforces the cooldown). Explicit hard caps (`max_call_seconds`, `max_tokens_out_per_call`) always trip regardless — you set that number on purpose. A connected plane can only escalate `on_spike` (`notify < trip < limit`), lengthen the cooldown, or otherwise tighten the nine tuning knobs — never loosen any of them. |
| `on_loop` | `None` / `"graded"` / `"break"` / `"throttle"` / `"escalate"` | `None` | The reaction to a loop only (details [below](#when-a-loop-is-detected)). **None** / **graded**: log, then page, then contain through the spike ladder (`loop_threshold`, `loop_alert_threshold`, `loop_contain_threshold`). **break**: raise immediately, whatever `on_anomaly` says. **throttle**: sleep before each repeat, never raise — a blocking `time.sleep()` on a sync call, and under a running event loop the engine hands the delay to the async wrapper instead, which `await asyncio.sleep()`s it, so the loop is never blocked either way. **escalate**: warn first, raise at `loop_hard_threshold`. |
| `on_provider_failure` | `"notify"` / `"open"` | `"notify"` | What a provider that keeps failing does to your calls. **notify**: count the failures and alert once when `circuit_failure_threshold` of them land inside `circuit_window_seconds` — nothing is ever blocked. **open**: also refuse calls — the wrapped client raises `CircuitOpen` (a `GuardrailTripped`, with `.provider`) **before** touching the provider for `circuit_cooldown_seconds`, then lets exactly one probe through; a successful probe closes the circuit. Your app catches it and picks its own fallback — [we never route](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). The circuit is per provider and process-wide, so it stops nobody's session and latches nothing. |
| `max_active_sessions`, `max_session_depth`, `max_child_sessions` | `None` / a number | `None` | The fan-out limits. A `session()` block that would take the run past one of them raises `GuardrailTripped` (detector `fanout`) **at the door, before its body runs**. **This row ignores `on_anomaly`** — like the per-call caps, these are numbers you stated — and it **latches nothing**: what was wrong is the shape of the run, not this key, so the next block is judged on its own. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |
| `max_actions_per_run` | a number, free and local | unset | How many `@runbound.tool` calls one run may execute. Under `envelope=True` (the default), the `(cap + 1)`th call raises `GuardrailTripped` (detector `fanout`, `details["rule"] == "actions"`) **before its body runs**. **This row ignores `on_anomaly`** and **latches**, exactly like `max_steps` — a run that has spent its action budget stays stopped, the same way a step-limited one does. `envelope=False` disables it entirely, since it is an envelope control. A connected plane can only lower the cap, or state one from scratch where you leave it unset, never raise it. |
| `max_steps`, `max_session_seconds`, `max_total_tokens` under `envelope=True` | on by default | — | With the default `envelope=True`, these three walls are also checked **at the door**: the `(max_steps + 1)`th model call, a call made after the run clock has already run out, or a call with a *stated* output cap that would push `max_total_tokens` over. **These are not new controls — they honor `on_anomaly` exactly as the wall behind them always has.** `"raise"`: raise `GuardrailTripped` before the request goes out, one call earlier than 0.3.0's post-call walls, and latch there (steps, run time) or not (tokens — a projection says nothing about the next call, the same reasoning as the money reservation); `details["rule"] == "envelope"` so it never dedupes away a later trip by the same wall. `"warn"` and `"callback"` both: log/alert at the door and let the call through — neither latches and neither calls your handler *here*. The call's own event is still recorded, and the wall behind the door detects the same crossing on it for real and reacts exactly once — under `"callback"` that is where your handler actually runs and the session latches, one call later than the door, precisely as it always has. An uncapped call is not checked for tokens at the door at all. `envelope=False` restores 0.3.0 exactly: none of this runs, and only the post-call walls fire. |
| `max_inflight_calls` | `None` / a number | `None` | How many calls to one endpoint may be in flight at once. The call that would take a provider label past it raises `GuardrailTripped` (detector `inflight`) **before the request goes out**. **This row ignores `on_anomaly`** — the number is one you stated — and **latches nothing**: the moment a slot frees up the next call goes through. Alerted once per endpoint. See [Self-hosted models](../guides/self-hosted-models.md#self-hosted-models). |
| `budget_admission`, `admission_output_tokens` | `"capped"` / `True` / `False`, `int` | `"capped"`, `1024` | A call's worst case is checked **before it goes out**. **`"capped"`** (the default since 0.4.0): only a request that states its own output cap — the cap at the output rate plus the input estimate — refused with `GuardrailTripped` (detector `budget`, `details["reason"] == "reservation"`) when it would cross `budget_usd`; a request with no cap takes the post-call path. **`True`**: also estimate a missing cap with `admission_output_tokens` (`details["rule"] == "admission"`). **`False`**: nothing is checked before a call goes out. **This row ignores `on_anomaly`** and **never latches**: a cheaper call may still fit. An unknown model skips the check (warned once per model). Alerted once per session. See [Admission](detectors.md#admission-a-budget-that-is-not-crossed-budget_admission). |
| The soft line | fraction (`budget_soft`), `"notify"` / `"safe_mode"` (`on_budget_soft`) — free and local | unset, `"notify"` | A line under `budget_usd`. Once one is set: the first call that takes the session past it — strictly greater, like the wall — gets one `warn` anomaly (detector `budget`, `details["limit_hit"] == "budget_soft"`, `soft_at_usd`). **notify**: that is all. **safe_mode**: the session is also narrowed to [`restricted`](../guides/policy.md#classify-by-capability) with `source="budget_soft"`, widened once spend is back under the line or by `clear(key)`. **This row ignores `on_anomaly`** and **never latches**. A call that crosses the soft line and the budget at once trips the budget, and the soft line stays quiet. A connected plane can only lower the fraction or escalate the reaction, or state a line from scratch where you leave `budget_soft` unset, never loosen one you configured. |
| `max_steps`, `max_events` | `None` / a number | `None` | Unlike the two rows above, both are ordinary detectors (`steps`, `events`): `critical`, follow `on_anomaly` and `on_trip` like `budget` does. **They count different things, not the same thing at two thresholds**: `max_steps` counts model turns (`llm_call` events) only, `max_events` counts every recorded event. Set both if you want an independent wall on each. See [max_steps and max_events](detectors.md#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `max_session_seconds`, `max_session_lifetime_seconds` | `None` / seconds | `None` | The two wall clocks. Also ordinary detectors (`timeout`), like the row above: `critical`, follows `on_anomaly` and `on_trip` like `budget` does. `max_session_seconds` measures the *run* — reset on every `session(key)` entry — and fires with `details["scope"] == "run"`; `max_session_lifetime_seconds` measures since the session's first-ever creation and fires with `details["scope"] == "lifetime"`. Each fires once per session, independently of the other. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |
| `on_halt` | `"raise"` / `"warn"` | `"raise"` | What an org-wide halt from [the control plane](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane) does to this worker. **raise**: every guarded `session()` block is refused **at the door** with `GuardrailTripped` (detector `halt`) — that is what a kill switch is for. **warn**: nothing is refused; the halt is logged at most once a minute, so you can prove the switch reaches your workers before you let it stop them. **This row ignores `on_anomaly`** and **latches nothing**: by default the halt lifts by itself 60 s after the last contact with the plane — see `stale_halt` below for the other choice. |
| `stale_halt` | `"release"` / `"hold"` | `"release"` | What an **enforced** halt does while the plane cannot currently vouch for it (not the same question as whether to enforce a halt at all — that is `on_halt`), whether that is because the link itself went degraded or because the plane is still answering but has lost its own fleet state (its live store, most often Redis — [fleet mode](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane) has the full story). **release**: the halt stops being enforced 60 s after the plane was last able to vouch for it, so a dead plane — or one that is up but has lost its own state — cannot keep a fleet stopped forever. **hold**: the halt stays enforced past that window, until a heartbeat explicitly says otherwise — pick this when a false "all clear" costs you more than a stuck kill switch. The clock starts at whichever of the two actually happened: the last successful contact for a dead link, or the moment the plane's own state was first found unavailable for a live one — the link can keep answering heartbeats the whole time, so the two are not the same clock. `plane_status().halt_stale_s` reports how long a currently-enforced halt has been stale by that same clock. |
| `on_plane_loss` | `"guard_locally"` / `"refuse"` | `"guard_locally"` | What entering a `session()` block does when the plane could not answer the entry question at all (timeout, error, a degraded link with no fresh cached decision) — a different moment from an *answered* refusal, which is always honored regardless of this setting. **guard_locally**: fall back to local detection alone, today's behavior. **refuse**: refuse the entry itself (detector `plane`, `GuardrailTripped`) rather than guess — latches nothing, costs no strike, and the very next entry asks the plane again. An invalid token is a configuration error, not plane loss, and guards locally under **both** settings (logged at most once a minute) — this option is only about a plane that could not be reached or answer, not one that rejected your credentials. |
| Fleet budget (`budget_usd` with a control plane) | — | as `budget_usd` | Nothing extra to configure. In fleet mode the entry answer carries what the rest of the fleet has already spent under this key, and the `budget` detector counts local spend **plus** that offset — so the reaction is exactly the `budget` reaction (`on_anomaly`, then `on_trip`), and the worker trips on the same turn a single worker with those numbers would. The anomaly says where the money went: `details["fleet_spend_offset_usd"]`. Same for `max_total_tokens` and `details["fleet_tokens_offset"]`. |
| Remote latch | — | always on with a plane | A latch **another worker** set is adopted here as if this worker had set it, with whatever is left of the fleet's ttl as this session's expiry and `details["origin"] == "fleet"` on the anomaly. From there it behaves like any local latch: under `on_anomaly="raise"` even *entering* `session(key)` raises, and under `"warn"` / `"callback"` the block runs and the reaction is re-applied on its first event. `is_tripped(key)` reports the real reason — the other worker's — and `runbound.clear(key)` clears it everywhere, not just here. |
| Org policy `dry_run` | set by the plane, not by you | off | An org [action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do) the plane marks `dry_run` is **logged and alerted and never blocks**: the anomaly is `warn`-severity, reads "Policy dry-run: would block tool …", and carries `details["dry_run"] is True`. It is how a platform team rolls a rule out across a fleet. **Your local `tool_policy` rules are untouched by it** and keep blocking exactly as they did. |
| Controls — per-detector `action`/`mode`, limits, `capabilities`, `envelope`, `circuit_rate`, `circuit_posture`, `loop_shapes`, `budget_soft`, `max_actions_per_run`, spike | set by your code, tightened by the plane | your own configuration | A dashboard-delivered override, per detector name (`loop`, `budget`, `spike`, `steps`, `timeout`, `error_storm`, `events`, `velocity`, `circuit`, …), tightened in **both** directions against this detector's own code baseline — `circuit` reads `on_provider_failure`, `loop` reads `on_loop`, `spike` reads its own resolved mode (`on_spike`), `velocity` (always `severity="warn"`) can never stop at all, every other name reads `on_anomaly`. A plane `action: "notify"` or `mode: "shadow"` that would **loosen** that baseline is refused: the detector keeps stopping exactly as your own code said, and the refused field is reported (`detectors.<name>.action`/`.mode` — see `runbound.coverage()`), so a raise-mode worker's walls — money included — cannot be switched off from a dashboard. A plane `action: "stop"`, `mode: "enforce"` that **tightens** past a `"notify"` baseline is applied for real only when this worker `can_stop` — `on_anomaly in ("raise", "callback")`; under `on_anomaly="warn"` it is **not applied at all** (forcing a `GuardrailTripped` nothing catches is exactly the bug fixed for the local case, not a remote feature) and is reported instead with `"reason": "cannot_stop"`, so the badge and the report agree. `mode: "shadow"` never stops anything either way. A `warn`-severity anomaly (a spike still watching, an escalating loop not yet confirmed, `velocity`, always) is never forced to stop by any control. The same body's `limits` (`budget_usd`, `max_steps`, `max_events`, `loop_threshold`, the three per-call caps), `capabilities`, `envelope`, `circuit_rate`, `circuit_posture`, `loop_shapes`, `budget_soft`, `max_actions_per_run` and spike can only ever **tighten** what you configured, or state one from scratch where you configured nothing — a looser plane value is refused, kept as your own, and reported back (`runbound.controls_merge`). `posture` rides `HelloReply.posture` instead, the same tighten-only path. Every field above is also a real, free, local `init()` keyword. See [Fleet mode](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane). |
| Fleet circuit | follows `on_provider_failure` | `"notify"` | The plane can open or close a provider circuit on **every** worker at once, so one outage is discovered once for the fleet. What an open circuit *does* here is unchanged and still yours: **notify** alerts and lets every call through, **open** raises `CircuitOpen` before the call. Like a local circuit, it stops no session and latches nothing. |
| Posture (safe mode) | `session.enter_safe_mode(posture=…)`, `runbound.enter_safe_mode(posture=…)`, the limited and closed rungs of the ladder (`on_spike="limit"`), a budget's soft line, or the control plane | `full` | A run that gets hot keeps thinking and loses the right to act. Five postures — `full`, `restricted`, `read_only`, `no_side_effects`, `stopped` — say which capability classes may run. A `@runbound.tool` is refused **before its body runs** when the posture denies **any one** of the classes it declares, and an **unclassified tool is refused by every posture but `full`**: `SafeModeViolation` (a `PolicyViolation`; detector `safe_mode`; a `warn` anomaly whose `details` name `posture`, `denied_class`, `effects`, `reason` and `source`). Model calls go on. A class rule (`{class: "deny"｜"approve"}`) refuses a class whatever the posture — free and local (`capabilities=`), and a connected plane's own class rules can only tighten yours further. **This row ignores `on_anomaly`** and **latches nothing**; a tool with no `@runbound.tool` is never refused by it. Widened by `exit_safe_mode()`, by `clear(key)` for a session, or — for the ladder's own narrowing — by the session healing; an automatic driver never lifts a manual posture, and the plane can only tighten. See [Classify by capability](../guides/policy.md#classify-by-capability). |
| `tool_policy.on_violation` | `"block"` / `"block_and_latch"` / `"dry_run"` | `"block"` | What a tool call that breaks [your action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do) does. **block**: refuse that one call — `PolicyViolation` (a `GuardrailTripped`) is raised on the agent's thread, the tool body never runs, and the session keeps going. **block_and_latch**: refuse it *and* stop the session, honoring `on_trip` (under `"once"`, only that call). **dry_run**: let the call run, and log and alert what would have been refused — how you roll a policy out. **This row ignores `on_anomaly`**: the rule is one you stated about your own agent, so it is enforced whether or not detectors are set to stop anything. |

Which detectors can latch a session: `budget`, `steps`, `events`, `error_storm`,
`timeout`, a `loop` under `"break"` / escalate-critical, a hard cap, a `spike`
only under a plane-delivered `spike.mode` of `"trip"` or `"limit"` (where the
latch is what serves the cooldown), and `policy` under
`on_violation="block_and_latch"`. `velocity` is warn-severity and never stops
anything, and a `fanout` refusal, an `inflight` refusal, a fleet `halt`, an
open `circuit` and a `safe_mode` refusal stop the block or the call in front of
them without latching the session at all. A **remote** latch is the one exception that arrives from
outside: it is a latch another worker made and this one adopts, ttl included.

**The plane can also refuse a session in its own words, with no local anomaly
behind it at all** — an org daily budget already spent, or an entry refused
under `on_plane_loss="refuse"`. An earlier revision of the SDK only read
the *facts* a plane decision carried (spend offsets, strikes, a latch) and never its
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

**By default a loop is graded.** 3 is worth a line, 6 is worth a person, 9 is a
runaway. The same call repeated `loop_threshold` times (3) is a `warn` in your
log and in `runbound.events()`, reacted `notify`: it pages nobody, on any
default route. At `loop_alert_threshold` (6, twice `loop_threshold`) it is a
`critical`, still reacted `notify`: it pages your alert routes and stops
nothing. At `loop_contain_threshold` (9, three times) the loop is handed to the
[spike ladder](../guides/spike-detection.md): the session is limited to the
`restricted` posture (reads and writes still run, external, financial and
destructive tools are refused before they execute), each further repeat spends
the ladder's allowance, and the last closes the session for the cooldown, counts
a strike and lets the key back in across the fleet when it is served, exactly as
a spike does. The anomalies keep detector `loop`. It covers the `repeat`,
`sequence` and `retry` shapes, counting each shape's own repeats. Each rung
fires once per loop, on the event that reaches it, and a loop that ends re-arms.
A normal model call between two repeats does not heal a session a loop limited.

Rung 3 needs the ladder to be able to act: a keyed session, `on_spike="limit"`
with spike detection on, and `on_anomaly="raise"`. When it cannot (or the control
plane's Controls say to notify only), the ninth repeat is one more `critical`
notice that says why it was not contained, and nothing is stopped.

| `on_loop` | What happens on a repeat |
|---|---|
| `None` (default) or `"graded"` | Log at `loop_threshold`, page at `loop_alert_threshold`, contain through the spike ladder at `loop_contain_threshold`, as above. Before 0.7.0 `None` meant "follow `on_anomaly`", which latched the session at the threshold with no way back; that is now `"break"`. |
| `"break"` | Raises `GuardrailTripped` as soon as the loop threshold is reached, even if `on_anomaly` is `"warn"`, and latches the session. Use it when a loop is always a bug and you want the old behaviour. |
| `"throttle"` | Sleeps before the repeated tool runs, doubling from `throttle_base_seconds` on each further repeat, up to `throttle_max_seconds`. Never raises. On a sync call this is a blocking `time.sleep()` on the agent's thread. Under a running event loop the sleep is instead `await`ed by the async tool wrapper (and, for a model-requested loop, by the async client wrapper) before the body runs — the delay travels through a `contextvar` from wherever the engine decided it, so the event loop is never blocked either way — except a synchronous tool called directly on the event-loop thread, which has nothing to await with and is not throttled. Use it to stop a burn while the run finishes. |
| `"escalate"` | Logs a warning on each repeat, then raises `GuardrailTripped` once the count reaches `loop_hard_threshold` (default `2 * loop_threshold`). Use it when a few repeats are normal and many are not. |

```python
# The graded default, with the rungs moved:
runbound.init(loop_threshold=4, loop_alert_threshold=8, loop_contain_threshold=12,
              on_spike="limit", on_anomaly="raise")

# The behaviour before 0.7.0, or any legacy policy, by naming it:
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
@runbound.tool(polling=True)
def poll_job_status(job_id: str) -> str:
    ...
```

`polling=True` (equivalently, listing the tool's name in
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
`on_anomaly="callback"` is the free, local answer for anyone who wants to
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

The plane's webhook adapter signs each delivery from a route that has a secret with an
`X-Runbound-Timestamp` / `X-Runbound-Signature` pair over one signing string (without one,
only the timestamp header is sent).
In the body, `session.id` is the sha256 key hash — the plane has no notion of
a per-process id, since any worker can serve the same session — and there is
no `session.key` field; a raw key reaches you, if you opt in, only through the `{key}` in your service's link template (a per-service
dashboard field). Verify a delivery like this:

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

## The refusal table

`runbound.GuardrailTripped` (and its public name, `runbound.ExecutionRefused`
— the identical class, forever) is a structured, retry-aware dependency
failure: never a fake success, never a swallowed error, never a business
decision made silently on your behalf. One table answers, for every refusal,
whether the provider was actually reached, what your own code receives, and
whether a retry could plausibly help — see
[Handling refusals](../guides/handling-refusals.md) for the full guide this
table is drawn from.

| Refusal | `exc.reason` | Provider called? | What your app receives | Retryable? |
|---|---|---|---|---|
| A dollar or token budget, before the call | `budget` / `tokens` | No | `exc.reason == "budget"` (or `"tokens"`), `exc.provider_called is False` | No — waiting does not put more money in the budget |
| A dollar budget crossed *after* the call returned | `budget` | **Yes** | `exc.provider_called is True`; the call happened and its usage is billed, but its result is withheld | No |
| `max_steps` / `max_session_seconds` at the door | `steps` / `time` | No | The run's own shape is over; nothing to wait out | No |
| A `stopped` posture | `posture` | No | No provider call ever begins | No |
| `restricted` / `read_only` / `no_side_effects` refusing a tool | `posture` | No (this is a tool call, not a model call) | The action is refused; the run keeps thinking | No |
| A capability class rule (`init(capabilities=...)`) | `policy` | No | Same shape as a posture refusal | No |
| Your own action policy (`blocked`, `max_calls`, a `constraint`) | `policy` | No | `exc.violation.rule` names which one | No |
| An approval nobody can answer | `approval` | No | Fail-closed: a gate that cannot answer must not wave the call through | No |
| `max_actions_per_run` | `blast_radius` | No | The run's own action budget is spent | No |
| An org-wide halt (kill switch) | `halt` | No | Every guarded block is refused at the door | No |
| The control plane could not be reached at all (`on_plane_loss="refuse"`) | `plane` | No | The very next entry asks the plane again | Yes — the link may already have recovered |
| A provider circuit that is open | `circuit` | No | `exc.retry_after` (`CircuitOpen`'s own decaying snapshot) | Yes |
| `max_inflight_calls` | `concurrency` | No | A slot frees up the moment another call closes out | Yes (`retry_after` is a heuristic poll interval, not a measured cooldown) |
| A loop, a retry storm, an error storm, a confirmed spike | `loop` / `error_storm` / `spike` | Varies — usually yes, once, on the call that confirmed it | The run's own behavior is the problem; retrying the same shape reproduces it | No |

`exc.retryable` is exactly the "Retryable?" column, and `runbound.
is_retryable(exc)` is the same answer as a free function for a retry loop's
own predicate — see the next doc for what to do with it.

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
`policy` (`PolicyViolation`), `safe_mode` (`SafeModeViolation`), `circuit` (`CircuitOpen`), `halt` (an org-wide
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
| `safe_mode` | 403 | That action isn't available right now. |
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
and the change reaches a worker at its next poll (`control_plane_poll_s` apart), no
redeploy. The last profile a worker saw survives a plane outage, exactly
like a policy rollout. `coverage()["refusals"]` reports which tier is
currently answering — `"plane"`, `"local"`, or `"default"`.

---
