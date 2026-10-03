# Configuration reference

[← Docs](../README.md)

Every field below is a keyword argument to `runbound.init()`. Bad values raise
`ValueError` at `init()` time, and unknown options raise `TypeError` — a
misconfigured guard fails loudly at startup instead of quietly guarding
nothing in production. Five names 0.3.0 retired — `slack_webhook`,
`pagerduty_routing_key`, `webhook_url`, `webhook_secret`, `link_template` —
are the one exception: they still raise, but with a `ValueError` naming where
that setting actually lives now (an [alert route](reactions.md#alerting) or a per-service
field on your runbound dashboard), not a bare `TypeError: unexpected
keyword`.

**Every field below is free, local and yours forever — no account, no token,
no cloud dependency.** A control plane, when you connect one, can only
*tighten* the fields marked **yes** in the fourth column, or add a value from
scratch where you left one unconfigured, never loosen what you already set;
**no** means the plane has no say in it. The Default column is read from
`GuardrailConfig` and checked against it by a test, so it cannot drift. See
[Free SDK, connected plane](../concepts/free-and-connected.md) for the full
split and [API reference](api.md) for the rest of the public API (everything
besides `init()`).

The fields are grouped by what they are for: [budgets and limits](#budgets-and-limits),
[loops, spikes and failing providers](#loops-spikes-and-failing-providers),
[postures and tools](#postures-and-tools), [the plane](#the-plane),
[privacy](#privacy), and [other](#other).

## Budgets and limits

What a session, a run or a call may spend or take: dollars, tokens, steps, time, how many sessions, and the door that enforces them.

| Name | Type | Default | The plane can tighten it | Meaning |
|---|---|---|---|---|
| `budget_usd` | `float \| None` | `None` | yes | Dollar cap for the session. **Not crossed by a call whose request states its output cap** — the default `budget_admission="capped"` reserves that call's worst case before it goes out — and **otherwise stops the session after the call that crossed it**: strictly greater than the limit, and after that call returned, since usage exists only then. See [Admission](detectors.md#admission-a-budget-that-is-not-crossed-budget_admission). |
| `budget_window` | `str \| float \| None` | `None` | no | When the key's budget resets. `None` (the default): never, the key's whole lifetime. `"hour"`, `"day"` or `"month"`: a calendar boundary in UTC. A number: a rolling window of that many seconds. Governs every cumulative counter on the key (spend, tokens, the estimated-cost slice), which reset together. In memory only: a restart starts a fresh window. Needs `budget_usd`. See [Runs keyed by any id](../guides/runs.md). |
| `run_budget_usd` | `float \| None` | `None` | no | A dollar cap for one run (one `session()` entry), fresh on every entry and independent of the key's own `budget_usd`; the tighter of the two binds a call, and `decision.level` says `"run"` or `"key"`. See [Runs keyed by any id](../guides/runs.md). |
| `max_total_tokens` | `int \| None` | `None` | yes | Token cap for the session (input + output). Works for unpriced and local models. A control plane states the same limit as `budget_tokens` and can only tighten it, never loosen it. Enforced after every call, and refused before one only under `on_anomaly="raise"` (with the default `budget_admission`; under `"warn"` the door records and notifies and the wall stops the next call. See [Admission](detectors.md#admission-a-budget-that-is-not-crossed-budget_admission)). A service-level budget stated by a plane is applied here per session key. |
| `run_max_total_tokens` | `int \| None` | `None` | no | A token cap for one run, fresh on every entry, alongside the key's `max_total_tokens`. Must be positive or `None`. See [Runs keyed by any id](../guides/runs.md). |
| `budget_soft` | `float \| None` | `None` | yes | A soft line under `budget_usd`, as a fraction strictly between 0 and 1 (`0.8` is 80%). The first call that takes the session past it gets one `warn` anomaly (detector `budget`, `details["limit_hit"] == "budget_soft"`); nothing latches. Requires `budget_usd`. A plane can only lower this fraction (fire sooner), or state one from scratch when you leave it unset, never raise it. |
| `on_budget_soft` | `"notify"` \| `"safe_mode"` | `"notify"` | yes | What the soft line does. **`"notify"`**: the warning only. **`"safe_mode"`**: also put the session in [safe mode](../guides/policy.md#classify-by-capability) — tools stop acting, model calls go on — until spend is back under the line or `clear(key)`. A plane can only escalate `"notify"` to `"safe_mode"`, never the reverse. |
| `budget_admission` | `"capped"` \| `bool` | `"capped"` | no | Reserve a call's worst case *before it goes out*. **`"capped"`**: only when the request states its own output cap. **`True`**: also estimate a missing cap with `admission_output_tokens`. **`False`**: no pre-call check at all (0.3.0). Changed from `False` in 0.4.0. See [Admission](detectors.md#admission-a-budget-that-is-not-crossed-budget_admission). |
| `admission_output_tokens` | `int` | `1024` | no | The output size `budget_admission=True` assumes for a request that states no `max_tokens` / `max_completion_tokens` / `max_output_tokens` cap. Must be a positive int. Unused under `"capped"`, and ignored whenever a request names its own cap. |
| `custom_prices` | `dict[str, tuple[float, float] \| tuple[float, float, float] \| tuple[float, float, float, float] \| tuple[float, float, float, float, float]]` | `{}` | no | `model -> (usd per 1M input tokens, usd per 1M output tokens)`, or add a third `usd per 1M cached-input (read) tokens` and, further, a fourth `usd per 1M cache-write tokens` (five-minute) and a fifth for a one-hour write, to state your own cache rates (a missing fifth is priced at the fourth). Overrides the built-in table, and is consulted before it — the fix for a price `runbound.pricing.as_of()` says is stale. |
| `on_unpriced_model` | `"zero"` \| `"estimate"` \| `"refuse"` | `"zero"` | no | What a model with **no** price — not in the built-in table, not in `custom_prices` — costs a dollar budget. **zero**: counted as $0.00, same as always, with a once-per-model warning **on by default** so the blind spot is not a silent one. **estimate**: priced from `unpriced_price_per_1m_usd` instead; the event/anomaly carries `priced="estimated"`. **refuse**: the call is refused at the door (detector `budget`, `details={"reason": "unpriced_model", "model": ...}`), before it goes out, whatever `on_anomaly` says — a choice you stated on purpose, so it is not negotiable per anomaly. A model only discovered unpriced after the fact (`record_call()`, or a model name known only from the response) is priced like `"estimate"` when a fallback pair was given, else like `"zero"`, and logged once either way. |
| `unpriced_price_per_1m_usd` | `tuple[float, float] \| None` | `None` | no | The `(usd per 1M input, usd per 1M output)` fallback pair `on_unpriced_model="estimate"` prices from. Required when that mode is set; a 2-tuple of non-negative numbers otherwise it is rejected at `init()`. |
| `max_steps` | `int \| None` | `None` | yes | Maximum model turns (`llm_call` events) in the session. An agent step is a model turn, not every recorded event — see [max_steps and max_events](detectors.md#max_steps-and-max_events-steps-are-turns-events-are-events). |
| `max_events` | `int \| None` | `None` | yes | Maximum recorded events in the session — every kind counted once each. What `max_steps` counted before 0.3.0. |
| `tokens_per_minute_limit` | `int \| None` | `None` | no | Ceiling on tokens in any trailing 60-second window. |
| `max_call_seconds` | `float \| None` | `None` | yes | Hard per-call duration ceiling. Breaching it is `critical` on the first call, no warm-up. |
| `max_tokens_out_per_call` | `int \| None` | `None` | yes | Hard per-call ceiling on output work — the provider's completion-token count, which already includes reasoning tokens. Same immediate `critical`. |
| `max_cost_per_call_usd` | `float \| None` | `None` | yes | Hard per-call ceiling on estimated dollars for one model call. Same immediate `critical`, from call #1; reported by `spike` with `details["metric"] == "cost_usd"`. `$0.00` for unpriced models — cap tokens there instead. |
| `max_session_seconds` | `float \| None` | `None` | no | Wall-clock ceiling on one *run*, on the monotonic clock. Reset on every `session(key)` entry (unchanged for the default session, which never re-enters). Any event kind trips it; `critical`, once per session, follows `on_anomaly`, `details["scope"] == "run"`. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |
| `max_session_lifetime_seconds` | `float \| None` | `None` | no | Wall-clock ceiling on a keyed session's whole existence, since its first-ever entry — never reset. The pre-0.3.0 meaning of `max_session_seconds`, for a customer who wants it. `critical`, once per session, follows `on_anomaly`, `details["scope"] == "lifetime"`. See [Time and fan-out limits](limits.md#time-and-fan-out-limits). |
| `max_active_sessions` | `int \| None` | `None` | no | How many `session()` blocks may be open at once, process-wide. Enforced at the door, whatever `on_anomaly` says; latches nothing. |
| `max_session_depth` | `int \| None` | `None` | no | How deep `session()` blocks may nest. A top-level block is depth `0`, so `2` permits it plus two levels under it. Same door, same rules. |
| `max_child_sessions` | `int \| None` | `None` | no | How many distinct child sessions one session may open. Same door, same rules. |
| `max_sessions` | `int` | `10000` | no | How many keyed sessions are kept; the least recently used is evicted. Must be >= 1. |
| `max_inflight_calls` | `int \| None` | `None` | no | **Opt-in.** How many guarded calls to one provider label may be in flight at once, process-wide. The call that would exceed it raises `GuardrailTripped` (detector `inflight`) before the request goes out, whatever `on_anomaly` says, and latches nothing. `None` counts nothing at all. See [Self-hosted models](../guides/self-hosted-models.md#self-hosted-models). |
| `max_actions_per_run` | `int \| None` | `None` | yes | How many tool actions (executed `@runbound.tool` calls) one run may make. Enforced at the door, before the `(max_actions_per_run + 1)`th call's body runs, under `envelope=True` only. Refused as detector `fanout`, rule `actions`, and latches. A plane can only lower this, or state one from scratch when you leave it unset, never raise it. |
| `envelope` | `bool` | `True` | yes | The door: `max_steps`, `max_session_seconds`, a stated-cap token check and `max_actions_per_run` are applied *before* a model call or tool action goes out, taking the same reaction the post-call wall would (under `"raise"` a refusal one call earlier, latched exactly as the wall would have; under `"warn"` / `"callback"` a notice, and the wall reacts once). `False` leaves every check to the wall, as in 0.3.x. |
| `estimate_tokens` | `bool` | `False` | no | **Opt-in.** When a response carries no usage at all, estimate tokens as `ceil(chars / 4)` over the request text and the answer, streams included. Never used when the endpoint reported usage. Logged once per process. For self-hosted servers that omit `usage`. |

## Loops, spikes and failing providers

How runbound notices an agent that repeats itself, runs away, or keeps hitting a failing provider, and what it does about it.

| Name | Type | Default | The plane can tighten it | Meaning |
|---|---|---|---|---|
| `loop_threshold` | `int` | `3` | yes | Identical tool-call hashes within the window that count as a loop. Must be >= 2. |
| `loop_window` | `int` | `20` | no | How many recent tool calls are remembered. Must be >= `loop_threshold`. |
| `on_loop` | `str \| None` | `None` | no | What to do about a loop specifically: `None` or `"graded"` (log at `loop_threshold`, page at `loop_alert_threshold`, contain through the spike ladder at `loop_contain_threshold`: the default since 0.7.0), or one of the legacy policies `"break"`, `"throttle"`, `"escalate"`. |
| `loop_alert_threshold`, `loop_contain_threshold` | `int \| None` | `None` / `None` | no | The graded policy's second and third rungs: repeats before it pages, and before it hands the loop to the spike ladder. Default `2 *` and `3 * loop_threshold` (never past `loop_window`). Set explicitly they must satisfy `loop_threshold < loop_alert_threshold < loop_contain_threshold <= loop_window`. |
| `loop_hard_threshold` | `int \| None` | `None` | no | Repeat count at which `on_loop="escalate"` stops the agent. Defaults to `2 * loop_threshold`. Must be greater than `loop_threshold`. |
| `throttle_base_seconds` | `float` | `2.0` | no | First sleep applied by `on_loop="throttle"`; it doubles on each further repeat. Must be positive. |
| `throttle_max_seconds` | `float` | `30.0` | no | Ceiling on the throttle sleep. Must be positive and >= `throttle_base_seconds`. |
| `loop_ignore_tools` | `tuple[str, ...]` | `()` | no | Tool names exempt from the loop window by policy rather than by decorator — equivalent to `@runbound.tool(polling=True)` for every call to that name, whoever runs it (executed or model-requested). They still count toward `tool_calls()` and any `max_calls` in your action policy. For marking a tool that is *meant* to repeat (polling a job, checking a status) — not for tuning around a real loop. |
| `loop_shapes` | `tuple[str, ...]` | `("repeat", "sequence", "retry")` | yes | Which of the four loop shapes run, in what order — the first that matches wins. `"stall"` is opt-in even locally. An unknown name raises `ValueError` at `init()`. A plane can only add a shape you have not opted into, never drop one you have. See [The four loop shapes](detectors.md#the-four-loop-shapes). |
| `loop_max_period` | `int` | `6` | yes | The longest tool-name rotation (`2..loop_max_period`) the `"sequence"` shape looks for. Must be `>= 2`. A plane can only raise this (search further), never lower it. |
| `loop_stall_turns` | `int` | `5` | yes | Consecutive turns introducing no new hash before the (opt-in) `"stall"` shape fires. Must be `>= 1`. A plane can only lower this (trip sooner), never raise it. |
| `spike_detection` | `bool` | `True` | yes | The per-session behavior watch — free and on by default. Set `False` to turn it off. A connected plane can only turn it back on, never off, if you disable it here. |
| `on_spike` | `str` | `"notify"` | yes | What a *confirmed* spike does: `"notify"` logs and alerts only; `"trip"` follows `on_anomaly` and stops that session; `"limit"` climbs [the spike ladder](../guides/spike-detection.md#many-callers-behind-one-service-on_spikelimit) — allowance, then rollover, then a block (requires `on_trip="latch"`). Hard caps always react regardless. A plane can only escalate this (`notify < trip < limit`), never downgrade it. |
| `spike_limit_calls` | `int` | `5` | yes | Ladder only. Abnormal calls a *limited* session may still make before it is closed and rolled over. Halved for each strike the key has already earned, floor 1. Must be >= 1. |
| `spike_cooldown_seconds` | `float` | `300.0` | yes | Ladder only. How long a rolled-over key is refused at the door before its fresh session starts serving. Must be positive. A plane can only lengthen this. |
| `spike_max_strikes` | `int` | `3` | yes | Ladder only. Rollovers a key may earn before it is blocked permanently, until `clear()`. Must be >= 1. |
| `spike_warmup_calls` | `int` | `4` | yes | Model calls of history a session needs before its baseline is trusted. Must be >= 2. |
| `spike_min_duration_s` | `float` | `2.0` | yes | Absolute rise over the median a duration spike must also clear. Must be positive. |
| `spike_min_output_tokens` | `int` | `500` | yes | Absolute rise over the median an output-work spike must also clear. Must be positive. |
| `spike_window` | `int` | `50` | yes | Model calls kept per session for the baseline. Must be greater than `spike_warmup_calls`. |
| `spike_factor` | `float` | `10.0` | yes | Multiple of the session's median duration or output work that counts as abnormal. Must be > 1. |
| `spike_confirm` | `int` | `2` | yes | Abnormal calls out of the trailing 5 that turn a watch into a `critical` anomaly. Must be 1–5. |
| `error_storm_limit` | `int \| None` | `10` | no | Failed calls (model **and** tool) in the trailing 60 seconds that a session may reach and stay quiet at. The one past it is `critical`. `None` disables it. See [Retry storms](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). |
| `on_provider_failure` | `str` | `"notify"` | no | What a failing provider's circuit does: `"notify"` counts and alerts once per outage and never blocks; `"open"` also raises `CircuitOpen` before each call until the provider recovers. |
| `circuit_failure_threshold` | `int` | `5` | no | **Count mode.** Provider failures inside the window that open its circuit. Must be positive. |
| `circuit_window_seconds` | `float` | `60.0` | no | The trailing window failures (count mode) or calls (rate mode) are counted in. Must be positive. |
| `circuit_cooldown_seconds` | `float` | `30.0` | no | How long an open circuit stays open before a probe is let through. Must be positive. |
| `circuit_reads_quota` | `bool` | `False` | no | **Opt-in.** Lets the circuit act on a provider's own rate-limit headers instead of only counting failures: a 429's `Retry-After` sets that opening's cooldown, and a bucket at zero opens the circuit pre-emptively. No header may hold a circuit shut past one hour. See [Reading the provider's own rate-limit headers](circuit-breaker.md#reading-the-providers-own-rate-limit-headers-opt-in). |
| `circuit_mode` | `"count"` \| `"rate"` | `"count"` | yes | How the circuit decides when to open. **count** (above): a fixed number of failures. **rate**: resilience4j's model — once `circuit_min_calls` calls have landed in the window, either `circuit_failure_rate` or `circuit_slow_rate` crossed opens it, a slow call counting even with zero errors. A plane can turn rate mode on from nothing, or tighten your own rate-mode knobs further, never loosen them. See [Retry storms](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). |
| `circuit_failure_rate` | `float` | `0.5` | yes | **Rate mode.** The fraction of calls in the window that must fail — strictly *more* than this — to open the circuit. Must be `> 0` and `<= 1`. |
| `circuit_min_calls` | `int` | `5` | yes | **Rate mode.** Calls in the window before a rate is judged at all; fewer than this and the circuit stays closed whatever the failure count. Must be a positive int. |
| `circuit_slow_call_seconds` | `float \| None` | `None` | yes | **Rate mode.** A successful call slower than this is marked "slow" for `circuit_slow_rate`, even though it did not fail. `None` (the default) turns slow-call detection off. Must be positive or `None`. |
| `circuit_slow_rate` | `float` | `0.5` | yes | **Rate mode.** The fraction of calls in the window that must be slow — strictly *more* than this — to open the circuit, independent of `circuit_failure_rate`. Must be `> 0` and `<= 1`. |
| `circuit_half_open_calls` | `int` | `1` | yes | How many probes a half-open circuit admits at once, in either mode, before the next one is refused. The default is the original single-probe rule. Must be a positive int. |
| `circuit_posture` | `bool` | `False` | yes | **Opt-in.** While a circuit is half-open, narrow the whole process to posture `restricted` (source `"circuit"`) until it fully closes. There is no per-provider tool scoping in this SDK, so this is process-wide, the same mechanism a manual call or the ladder use. A plane can only turn this on, never off, if you enable it here. See [Retry storms](circuit-breaker.md#retry-storms-and-the-provider-circuit-breaker). |
| `on_trip` | `str` | `"latch"` | no | After a critical trip: `"latch"` keeps the session stopped until `clear()`/ttl; `"once"` stops that one call only. See [the reactions table](reactions.md#what-happens-when-something-trips--every-choice-in-one-place). |
| `latch_ttl_seconds` | `float \| None` | `None` | no | **Opt-in.** `None` = a tripped session stays tripped until `clear()`. A number = the latch expires that many seconds after it was set: every detector is re-armed and the session's next event is judged fresh, on the same cumulative counters — a session still over budget re-trips immediately, with the same detector. Re-admits; does not reset. |

## Postures and tools

What an agent may do, class by class, and the rules for its tools.

| Name | Type | Default | The plane can tighten it | Meaning |
|---|---|---|---|---|
| `tool_policy` | `ToolPolicy \| dict \| None` | `None` | no | The rules for what your agent may do, enforced at every guarded tool call: `deny`, `allow`, `max_calls`, `constraints`, `require_approval` + `approval_callback`, and its own `on_violation`. A dict of those fields is coerced to a `ToolPolicy` and validated at `init()`. Per-tool rules belong on `@runbound.tool` instead; this is for the fleet-wide `allow` list, for tools that carry no decorator of yours, and for `on_violation`. Where both name a tool, the decorator wins and one warning says so. See [Action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do). |
| `require_rules` | `bool` | `False` | no | The CI gate. `True`: a `@runbound.tool` that states no rule at all raises `ValueError` — at `init()` for every such tool already imported, and at decoration for every one declared afterwards. `reviewed=True` on the decorator says a tool was reviewed and needs none. |
| `postures` | `dict \| None` | `None` | no | Add or override rows in the built-in posture table (`{name: {class: verdict}}`) — a new named posture, or a stricter class verdict layered onto a built-in one (`full`, `restricted`, `read_only`, `stopped`, `no_side_effects`). Validated against known posture names and verdicts at `init()`. See [Classify by capability](../guides/policy.md#classify-by-capability). |
| `capabilities` | `dict \| None` | `None` | yes | A class rule that holds whatever the posture is (`{class: "allow" \| "deny" \| "approve"}`) — the free four's own `tool_policy`, one capability class at a time. A connected plane's own class rules can only tighten these (`allow` -> `approve` -> `deny`), never loosen them. See [Classify by capability](../guides/policy.md#classify-by-capability). |

## The plane

Connecting to a control plane, and how this process behaves when it is, or is not, reachable. Every one is optional: with no plane the SDK is complete on its own.

| Name | Type | Default | The plane can tighten it | Meaning |
|---|---|---|---|---|
| `token` | `str \| None` | `None` | no | The credential from your runbound dashboard, sent as `Authorization: Bearer …`. This is the one way to connect, and it names one of two kinds of customer (`GuardrailConfig.plane_mode`): **alone, with no `control_plane_url` set anywhere** (not passed, and not in `RUNBOUND_PLANE_URL`), it is **hosted** — you are on our server, we resolve the endpoint at `HOSTED_PLANE_URL`, which is `None` until that plane exists, so a bare token today logs one WARNING and the process stays local. Set `control_plane_url` too, from either source, and that is **self-hosted** instead — your own cluster — and `token=""` is how you state that plane has no auth (see `control_plane_url` below). Read from the `RUNBOUND_TOKEN` environment variable when unset (blank or whitespace counts as unset, from either source). **`token=""` stops `RUNBOUND_TOKEN` being read** — it is how a library or a test harness says "no credential, whatever the caller's shell exports". It does not by itself pin a process local: with a `control_plane_url` (passed, or from `RUNBOUND_PLANE_URL`) it states a self-hosted plane with no auth. **`control_plane_url=""` is the pin** — the documented way to say "never connect, no matter what the caller's shell exports." What a token does *not* do any more is deliver anything: Slack, PagerDuty and a signed webhook are the control plane's job (0.3.0), configured as an [alert route](reactions.md#alerting) on your dashboard, not a keyword here. Kept out of `repr()`. |
| `api_key` | `str \| None` | `None` | no | **Deprecated**, the old name for `token`. Still works for one release: it folds into `token` at `validate()`, with a one-time `WARNING` whether or not `token` was also set, and is cleared to `None` afterwards so nothing downstream reads it. Prefer `token`. Kept out of `repr()`. |
| `control_plane_url` | `str \| None` | `None` | no | Present, this is the **self-hosted** mode: your own cluster, at this url, and it requires a `token` (`token=""` if that plane needs no auth — a url is never enough by itself) — **when you passed it**. A url that came from `RUNBOUND_PLANE_URL` with no token anywhere is a deployment fact rather than a typo, so it warns once and guards locally instead of failing your `init()`: a base image that exports the variable must not be able to take down a service that has no token yet. Absent with a `token` set, a bare token points here for you instead (**hosted** mode, above). Absent with no token at all, this is **off** — the single-process SDK, which opens no sockets of its own. Blank or whitespace, from the call or from `RUNBOUND_PLANE_URL`, counts as unset either way: there is no such thing as a plane at the empty url. `GuardrailConfig.plane_mode` (`"off"` \| `"hosted"` \| `"self_hosted"`) is the one property that answers which of the three a configuration is, and every internal reader (`shared.build` first) asks it instead of re-deriving its own truthiness test. |
| `service` | `str` | `"default"` | no | Which fleet this process belongs to. The plane keys org policy and dashboards on it. |
| `worker_id` | `str \| None` | `None` | no | This process's name inside the fleet. Defaults to `hostname:pid:xxxxxx` — six random hex characters appended so two replicas that share a hostname (containers, a restarted process reusing a pid) never collide on an id. |
| `control_plane_timeout_s` | `float` | `0.15` | no | The longest a plane call may block a session's entry. Must be `> 0` and `<= 2.0`: the plane is an optimization on top of local detection, never a dependency of it. |
| `control_plane_poll_s` | `float` | `5.0` | no | How often the heartbeat runs, on a daemon thread. The plane can slow a chatty fleet down without a redeploy. Must be positive. |
| `control_plane_cache_s` | `float` | `5.0` | no | How long one key's entry answer is reused before the plane is asked again. This is the fleet's convergence window: a worker learns of a key latched elsewhere at its next entry that misses the cache, so it can be up to `control_plane_cache_s` late; a block already running finishes first. Lower it for tighter propagation and more requests; raise it for fewer. Must be positive. |
| `circuit_fleet` | `bool` | `True` | no | **Fleet mode only.** Whether this worker takes part in the fleet-wide circuit fold: its own transitions reported to the plane, a fleet instruction forced onto its local breaker — including starting open on its very first hello if the fleet's circuit for that provider already is. `False` keeps the circuit purely local, exactly as with no plane at all. Ignored, and irrelevant, with no plane configured — a fold with nobody to fold with is not a control the plane needs to state. |
| `on_halt` | `str` | `"raise"` | no | What an org-wide halt does here: `"raise"` refuses every guarded `session()` block at the door (detector `halt`); `"warn"` keeps serving and logs it once a minute. What happens to an *enforced* halt once the plane link itself goes stale is `stale_halt`, below. |
| `stale_halt` | `"release"` \| `"hold"` | `"release"` | no | **release**: a halt stops being enforced 60 s after the last contact with the plane. **hold**: it stays enforced until a heartbeat says otherwise. See [the reactions table](reactions.md#what-happens-when-something-trips--every-choice-in-one-place). |
| `on_plane_loss` | `"guard_locally"` \| `"refuse"` | `"guard_locally"` | no | What a `session()` entry does when the plane could not answer at all (timeout, error, a degraded link with no fresh cached decision): **guard_locally** falls back to local detection; **refuse** refuses the entry itself (detector `plane`), latching nothing. An invalid token always guards locally, in both modes — it is a configuration error, not plane loss. |

## Privacy

What leaves the process when a plane is connected. Counts and hashes by default, never content.

| Name | Type | Default | The plane can tighten it | Meaning |
|---|---|---|---|---|
| `export_events` | `bool` | `True` | no | Ship the event stream (counts and hashes, never content) to the plane in background batches. Telemetry only: with it off the session exit deltas, circuit transitions and trips still ship, so this worker keeps contributing to the fleet's shared spend. |
| `send_session_keys` | `bool` | `False` | no | **Opt-in.** Send the raw session key alongside its hash, to the plane. Off by default: a key travels as `sha256(key)` and nothing else, and every occurrence of it inside an anomaly's `message` and `details` is replaced by the first 12 characters of that hash and an ellipsis (`"3f2a9c1b04d7…"`). By default the plane's own alert adapters see only that hash too. Turn this on and the raw key can reach a delivery — not as a field the plane adds, but wherever you put it yourself: the `{key}` in your service's link template (set on the dashboard), or a detector's `message`/`details`, now unredacted, passed through to your own Slack, PagerDuty or webhook. |

## Other

Reactions and wiring that fit none of the above.

| Name | Type | Default | The plane can tighten it | Meaning |
|---|---|---|---|---|
| `on_anomaly` | `str` | `"warn"` | no | `"warn"`, `"raise"`, or `"callback"`. |
| `callback` | `Callable[[Anomaly], None] \| None` | `None` | no | Your handler. Required by, and only valid with, `on_anomaly="callback"`. |
| `on_event` | `Callable[[dict], None] \| None` | `None` | no | **Opt-in.** Told about every record `runbound.events()`/`decisions()` appends to their in-memory rings, the moment it happens — an anomaly, a refusal, a posture transition or a Decision, as a plain dict. A raising callback is logged and swallowed (fail-open); the rings are readable with no callback at all. See [API reference](api.md). |
| `refusals` | `dict \| None` | `None` | no | **Opt-in.** Your own HTTP status and sentence for a refusal, by detector (including `"plane"`) or `"default"`. Validated at `init()` — a bad status or an over-length message raises `ValueError` naming the key. A control-plane profile overrides this field by field; unset, `BUILTIN` answers. See [What the caller sees](reactions.md#what-the-caller-sees--your-words-your-status). |
| `auto_wrap` | `bool` | `True` | no | Patches the OpenAI and Anthropic SDK classes at `init()`, so every client built afterwards is guarded without a `wrap()` call. The provider label is still read per call from that client's `base_url`, so per-endpoint circuits keep working. `False` leaves the classes untouched. Reversible with `runbound.unpatch()`. See [What the SDK actually sees](../concepts/what-it-sees.md#what-the-sdk-actually-sees--and-what-it-never-sees). |
| `coverage_check_seconds` | `float \| None` | `60.0` | no | How long after `init()` runbound waits before warning, once, that a provider SDK is imported but no guarded call has been recorded. `None` turns the check off. |

Every limit knob — `budget_usd`, `max_total_tokens`, `max_steps`, `max_events`,
`tokens_per_minute_limit`, `max_call_seconds`, `max_tokens_out_per_call`,
`max_cost_per_call_usd`, `max_session_seconds`, `max_session_lifetime_seconds`,
`max_active_sessions`, `max_session_depth`, `max_child_sessions`, `max_inflight_calls`,
`error_storm_limit`, `latch_ttl_seconds` — must be positive or `None`. The three original `circuit_*`
knobs are always on and must be positive; `circuit_mode`, `circuit_failure_rate`, `circuit_min_calls`,
`circuit_slow_call_seconds`, `circuit_slow_rate`, `circuit_half_open_calls`, `circuit_fleet` and
`circuit_posture` are validated the same way whatever `circuit_mode` is set to, so a later
switch to `"rate"` never discovers a bad value that was sitting there unchecked.

---
