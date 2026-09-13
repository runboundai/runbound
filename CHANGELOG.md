# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-09-12

First public release, as `runbound` (import `runbound`), by Runbound AI.
Earlier versions were internal.

### Added

- **Fleet mode — a control plane, so N workers share one truth.** Connect
  with `token` alone (hosted) or `control_plane_url` plus a `token`
  (self-hosted — `token=""` if that plane needs no auth; see Wave 31 below
  for how the two are told apart), and the replicas serving one end-user
  share a budget, a latch, a strike count, an org action policy and a set of
  provider circuits. Detection does not move: every detector still runs
  in-process, on your thread, with no model calls.
  - **Shared budget.** A session entry carries the fleet's spend and tokens for
    that key; the SDK folds them in as offsets, so `budget_usd` and
    `max_total_tokens` trip on the turn a single worker with those numbers
    would (`details["fleet_spend_offset_usd"]`, `["fleet_tokens_offset"]`).
  - **Shared latch and strikes.** A critical trip is reported synchronously and
    the next worker to open that key is refused at the door, with what is left
    of the fleet's ttl; `clear(key)` clears it fleet-wide.
  - **Org-wide action policy**, merged with the local `tool_policy`
    most-restrictive-wins (`policy.merge()`) — bans unioned, allow-lists
    intersected, `max_calls` the lower of the two, callables never taken off
    the wire. Org rules report `details["origin"] == "org"`. A `dry_run`
    version is logged and alerted and never blocks, which is how a rule is
    rolled out across a fleet; local rules keep blocking.
  - **Fleet circuits.** The heartbeat can open or close a provider circuit on
    every worker at once (`circuit.force_open()` / `force_close()`). What an
    open circuit does is still `on_provider_failure`.
  - **Fleet kill switch**, with `on_halt="raise"` (refuse every guarded
    `session()` block, detector `halt`) or `"warn"` (log once a minute, keep
    serving).
  - **The plane is contacted at four moments and nowhere else**: an `init()`
    heartbeat every `control_plane_poll_s` (5 s), a session entry (bounded by
    `control_plane_timeout_s`, 150 ms, and cached per key for 5 s), a session
    exit (queued, posted in background batches) and a trip or circuit change.
    Never per model call, never per tool call.
  - **Fail-open throughout.** A plane that is down costs one timeout, warns once
    a minute, degrades after 3 consecutive failures and retries once every 30 s;
    a rejected key goes local-only; a fleet halt stops being enforced 60 s after
    the last contact. `plane_status()` reports `"local"` / `"connected"` /
    `"degraded"`.
  - **Hashes and counts, never content.** One module, `runbound.plane_types`,
    defines every outbound record: session keys travel as `sha256(key)` (raw
    only with `send_session_keys=True`), tool arguments only as `args_hash`,
    error messages only as `error_class`, detector `details` scrubbed to JSON
    scalars, tags capped. Prompts and replies have no field to travel in.
  - New config: `control_plane_url`, `token` (`api_key` is the deprecated
    alias), `service`, `worker_id`, `control_plane_timeout_s`,
    `control_plane_poll_s`, `export_events`, `send_session_keys`, `on_halt`.
    New API: `plane_status()`, `fleet_status(key)`, `key_hash(key)`.
- **Fleet mode talks to any server that speaks six endpoints, not a bespoke
  client.** `POST /v1/hello`, `/v1/enter`, `/v1/trip`, `/v1/events`,
  `GET /v1/policy`, `POST /v1/clear` are the whole wire protocol a connected
  plane must answer; the SDK's half of fleet mode is free, MIT-licensed, and
  works against anything that speaks them, ours or not.
- **Webhook signatures verify the same way after delivery left the SDK.**
  `runbound.verify_webhook_signature()` is the receiver-side check kept when
  the SDK's own `WebhookAlerter` retired in Wave 31 (below): a constant-time
  compare of `X-Runbound-Timestamp` / `X-Runbound-Signature` =
  `"sha256=" + HMAC_SHA256(secret, f"{timestamp}.{body}")`, with a
  ±5-minute replay window, `False` rather than an exception for anything
  malformed. Whatever now sends a webhook uses the same envelope, headers
  and signing string the retired sender did, so a receiver that only checks
  the signature needs no changes — but two body fields did change:
  `session.id` is now the sha256 key hash rather than a per-process id, and
  `session.key` (what `send_session_keys=True` used to add) does not exist
  in the body at all; a raw key reaches you only through your own
  `link_template`.
- **Instrumentation coverage.** `auto_wrap` (default `True`) patches the OpenAI
  and Anthropic SDK classes at `init()`, so a client built anywhere — inside a
  framework, in code written before runbound was installed — is guarded
  without a `wrap()` call; per-call endpoint labels still come from that
  client's `base_url`, and `unpatch()` reverses it. `coverage()` reports what is
  actually instrumented right now (auto-wrapped SDKs, wrapped clients, decorated
  tools, guarded calls, providers imported but never seen guarded);
  `assert_guarded()` raises for a startup check or a CI smoke test; and
  `coverage_check_seconds` (default 60 s) warns once when a provider SDK is
  imported and no guarded call has ever been recorded.
- **Customer-set refusal responses.** runbound raises `GuardrailTripped`; it
  never used to say what the end user should be told. `exc.refusal` now
  carries the HTTP status and sentence *you* set — `runbound.init(refusals={...})`
  locally, or set once on a connected control plane, on every plan, never
  gated. Precedence is checked field by field: a connected plane's
  per-service profile, then its org-wide profile, then your local
  per-detector entry, then your local `default`, then a documented
  `BUILTIN` fallback. Keys are `"default"` or a detector name (`budget`,
  `loop`, `spike`, `velocity`, `steps`, `error_storm`, `timeout`, `fanout`,
  `inflight`, `policy`, `circuit`, `halt`, `fleet`) — per-call hard caps
  report through `spike`, so there is no `cap` key. A message may carry
  `{retry_after_s}` / `{detector}`, formatted safely, and
  `exc.refusal.headers` carries a rounded-up `Retry-After` when a latch's
  remaining time is known. A profile set on a connected plane reaches every
  worker within `control_plane_poll_s`, no restart, and survives a plane
  outage like a policy rollout does. `coverage()["refusals"]` reports which
  tier is answering. New module `runbound.responses` (`Refusal`,
  `refusal_for`, `BUILTIN`); new config `refusals`; `examples/stress` now
  answers refusals entirely from a profile instead of a hardcoded status and
  sentence.

- **Honest promises (Wave 24).** An outside review of the positioning and a
  few silent gaps in the code, fixed together.
  - **An unpriced-model policy, `on_unpriced_model`.** `"zero"` (default,
    unchanged behavior) now warns once per model **by default**, so the
    blind spot is not a silent one — anyone self-hosting a model with no
    price set gets one warning, not zero. `"estimate"` prices from a new
    `unpriced_price_per_1m_usd` fallback pair instead, marking the number
    `priced="estimated"`. `"refuse"` stops the call at the door, before it
    goes out, whatever `on_anomaly` says — a choice you stated on purpose.
  - **Tools that are supposed to repeat.** `@runbound.tool(repeatable=True)`
    and the new `loop_ignore_tools` config mark a polling tool exempt from
    the loop window only; it still counts toward `tool_calls()` and any
    `max_calls` in an action policy.
  - **The async throttle actually throttles.** `on_loop="throttle"` used to
    warn-and-skip under a running event loop, silently doing nothing. It now
    hands the delay through a contextvar and the async wrapper `await
    asyncio.sleep()`s it, so throttling works the same way under asyncio as
    it does under threads.
  - **Argument hashes are salted per process.** `args_hash` now mixes in a
    random salt generated once at import (kept across `reset()`). It remains
    an equality token for spotting a repeat inside one process; it is no
    longer even accidentally usable as a fingerprint to correlate calls
    across two processes or two workers from the hash alone.
  - **Abandoned streams are counted, not lost.** A stream that is never
    exhausted or closed used to vanish — no tokens, no step, ever. It is now
    recorded as one partial call the moment Python garbage collects it
    (never at interpreter exit), in the session that opened it, with the
    time actually streamed and tokens from usage if any chunk carried it,
    else `ceil(chars/4)` of the streamed text marked `estimated`. Counted as
    neither a circuit success nor a failure; its in-flight slot is freed.
  - **`stale_halt` and `on_plane_loss`, two more explicit customer choices.**
    `stale_halt="release"` (default, today's behavior) lifts an enforced
    halt 60 s after the last plane contact; `"hold"` keeps it enforced on a
    dead link until a heartbeat says otherwise.
    `on_plane_loss="guard_locally"` (default) falls back to local detection
    when an entry question could not be answered at all; `"refuse"` refuses
    the entry itself instead, latching nothing. An invalid API key is
    always a configuration error, guarded locally under both settings.
  - **The plane's own refusals are now honored at the door.** Before this
    wave the SDK read only the *facts* a plane decision carried (spend
    offsets, strikes, a latch) and never its `allow` field, so a plane that
    refused a session outright — an org daily budget already spent, or an
    entry refused under `on_plane_loss="refuse"` — was silently overruled
    and the call went out anyway. It is now refused at the door, with
    `details["origin"] == "plane"` either way. An entry refused under
    `on_plane_loss="refuse"` is detector `plane` and resolves against a new
    `"plane"` key in refusal profiles (built-in: HTTP 503); an org daily
    budget already spent is detector `budget` (`details["rule"] ==
    "org_budget"`) — a budget like any other, just decided by the plane —
    and resolves against the `"budget"` key.
  - New `INVARIANTS.md`: the budget, latch, circuit, policy-monotonic, halt,
    plane-loss, abandoned-stream and unpriced-model guarantees, each with
    its bound and the tests that assert it — or "not yet asserted" where
    honest.
  - Positioning: the README leads with "runtime controls for autonomous AI
    agents," names the buyer (the platform team that owns AI agents in
    production), and states the fleet budget as a bound (worst case:
    `workers × one in-flight turn` plus the entry-cache window) rather than
    calling it a hard limit. A compatibility matrix (provider ×
    sync/async/stream/tools/usage/live) is filled in only where a named test
    proves the cell.

### Wave 26

- **`init()`'s heartbeat now reports coverage.** The hello payload the SDK
  sends the plane carries `coverage: {guarded_calls, decorated_tools,
  providers_imported, providers_unguarded}` — the same numbers
  `runbound.coverage()` already exposed locally — so a connected plane's
  Services page can show, per worker, what is actually guarded; any
  failure computing it degrades to `{}` rather than breaking the
  heartbeat. Shown as counts, deliberately not a percentage.

### Wave 31 (delivery leaves the SDK; the token is the connection)

- **`token` replaces `api_key` as the one way to connect.** A dashboard
  credential is now `token`. `api_key` is the old name for the same secret:
  it still works, folding into `token` at `validate()` with a one-time
  `WARNING` — fired whether or not `token` was *also* set, so the two never
  sit on the object together — and cleared to `None` afterwards so nothing
  downstream reads it. `token` also reads from the `RUNBOUND_TOKEN`
  environment variable when unset (blank or whitespace counts as unset).
- **Two kinds of connected customer, not one setting with a truthiness
  test.** `token` alone — with no `control_plane_url` set anywhere, and none
  in `RUNBOUND_PLANE_URL` either — is **hosted**: you are on our server, we
  resolve the endpoint at `HOSTED_PLANE_URL` (`None` until that plane
  exists; a bare token then logs one WARNING and the process stays local,
  rather than the ~45-line urllib traceback a placeholder domain pointing
  at a parked host used to produce). A `control_plane_url`, from the call or from
  `RUNBOUND_PLANE_URL`, is **self-hosted** instead: your own cluster, and
  `token=""` is how you say that plane has no auth. Both facts can also come
  from the environment
  (`RUNBOUND_TOKEN`, `RUNBOUND_PLANE_URL`), and a blank value from either
  source is unset either way — there is no such thing as a plane at the
  empty url. `GuardrailConfig.plane_mode` (`"off"` / `"hosted"` /
  `"self_hosted"`) is the one question every internal reader now asks
  instead of re-deriving its own falsiness test; `shared.build` asks it
  first. A `control_plane_url` with no token still raises, reworded around
  the two modes (`'a self-hosted plane needs a token ... token="" if that
  plane has no auth'`).
- **Delivery is not in the SDK.** An earlier cut of this wave gated
  `SlackAlerter`, `PagerDutyAlerter` and `WebhookAlerter` behind a token
  inside `runbound/`; a review found that could never be made to hold —
  `control_plane_url=""` passed validation while the gate and `shared.build`
  read the field differently, and any non-empty token turned delivery on for
  the life of the process (`Engine.alerters` is fixed at construction), so a
  revoked or invented token kept posting. A paid feature cannot be enforced
  by a conditional running inside the customer's own process, so the sending
  code left instead: those three classes, the PagerDuty constants, the
  posting helpers and the alert rate limiter are gone from
  `runbound/alerts.py`, and `Engine._alert` now only notifies observers.
  `init()` answers the five retired keywords — `slack_webhook`,
  `pagerduty_routing_key`, `webhook_url`, `webhook_secret`, `link_template`
  — with a `ValueError` naming where the setting actually lives now (an
  alert route, or a per-service field, on your runbound dashboard) instead
  of a bare `TypeError: unexpected keyword`. `verify_webhook_signature` and
  the outbound-thread draining bookkeeping stay in `runbound/alerts.py` —
  the receiver's helper, and what `export.py` needs at interpreter exit —
  and `on_anomaly="callback"` stays free forever, a hook into the customer's
  own process, not a delivery channel. This changes nothing about detection:
  every detector, the latch, refusals, tool policy, the provider circuit and
  the in-flight cap are identical with and without a token, `on_anomaly`'s
  three reactions (`raise`, `callback`, `warn`) are alternatives — a raise or
  a callback does not also log — and behave identically with or without one,
  all asserted directly in `tests/test_token_and_delivery.py` and
  `tests/test_plane_modes.py`. See the rewritten invariant in
  `INVARIANTS.md`, "The SDK detects, stops, refuses and reports. The plane
  routes and delivers."

### Wave F (T133–T135): what the SDK counts, and which anomaly wins

Three release-gating fixes from an outside review of the SDK, landed before
the first PyPI release because each changes the meaning of a public knob —
renaming or re-meaning one after real customers hold a version would break
them.

- **T133 — `max_session_seconds` measures the run, not the end-user's whole
  history.** Before this, `SessionState.started_at` was set once, at
  creation, and `max_session_seconds` measured elapsed time from it — so for
  a keyed chatbot session that is reused across requests, the wall clock
  counted from the user's very first message ever, and a returning user
  could trip the timeout on their next word. `SessionState.run_started_at` is
  now reset on every entry of `runbound.session(key)`, and the `timeout`
  detector measures `max_session_seconds` against it instead
  (`details["scope"] == "run"`). The default (unkeyed) session, which has no
  entry to reset on, is unchanged: it *is* the run, from `init()` onward.
  `started_at` keeps its old meaning — the session's whole existence, never
  reset — for the new, opt-in `max_session_lifetime_seconds`
  (`details["scope"] == "lifetime"`), for a customer who wants the old
  behavior back on purpose. The two fire independently.
- **T134 — steps are model turns; a new `max_events` counts everything.**
  `max_steps` counted every recorded event before this — a turn with one
  model call and three tool calls read as four steps. `SessionState.turns`
  now counts `llm_call` events only, and `max_steps` (detector `steps`) is
  measured against it. `SessionState.step_count` is renamed
  `event_count` (measuring what `max_steps` used to: every recorded event),
  and a new `max_events` (detector `events`) is measured against it instead.
  `step_count` is kept for one release as a read-only alias for
  `event_count`. `ExitDelta.steps_delta` now carries turns, not the raw event
  count — see "What the plane needs from this" below.
- **T135 — anomaly ties are resolved by a stated order, not list order.**
  When more than one detector fires critical on the same event, which one
  drove the reaction used to depend on `detectors.DEFAULT_DETECTORS`' list
  order — invisible, and never a decision anyone actually made. A new
  `runbound.events.PRIORITY` table states the order once (highest first):
  `policy`, `budget`, `loop`, `error_storm`, `steps`, `events`, `timeout`,
  `spike`, `velocity`. `Engine._winner` (renamed from `_most_severe`)
  resolves ties against `(severity, priority, detector name)` instead of
  iteration order — reversing `DEFAULT_DETECTORS` now produces the same
  winner. A detector name the table has never heard of (a customer's own)
  sorts after every named one and warns once; it never crashes the
  selection. Door anomalies (`halt`, `circuit`, `inflight`, `plane`) are
  raised before detection runs and are never part of a tie.

  **What the plane needs from this (T146):** `ExitDelta.steps_delta` on the
  wire now means model turns, not the raw event count it meant before this
  release — a dashboard's "Steps" column reading it should say "model turns."
  T146 is the task that changes the plane; this release only prepares the
  SDK side (`SessionState.turns`, `SessionState.event_count`, the `events`
  detector) and repoints the one existing wire field. It does not add a
  separate wire field for the raw event count, `errors_delta`,
  `tokens_cached_delta`, `last_detector`, or the two trigger fields — those
  are T146's own addition, together with the plane-side router, ledger and
  migration this SDK release does not touch.

### Wave F (T136–T137): admission, and a healed latch that re-arms

Two more release-gating fixes from the same outside review.

- **T137 — a healed latch re-arms detection.** `latch_ttl_seconds` expired a
  latch, but every detector still fires only once per session id and no
  counter was ever reset — so a session that was still over budget when the
  ttl elapsed ran with *no budget wall at all* afterward, the opposite of
  what the setting promises. On heal, the engine now calls a new
  `rearm(session_id)` on every detector (`_FireOnceDetector.rearm`,
  overridden by `TimeoutDetector` for its second, lifetime-scoped memo, and
  by `SpikeDetector` for its two-phase warn/confirm memos), so a session
  whose condition still holds re-trips on its very next event, with the same
  detector. This is re-admission, not a clean slate: no counter is reset —
  only `runbound.clear()` does that — so `latch_ttl_seconds` is not a
  windowed budget (that stays a separate, unbuilt feature). The stale claim
  that a healed session "stays quiet about the condition it already
  reported" is gone from the docs and the code's own docstrings.
- **T136 — admission: a named phase, and an opt-in pre-call budget
  estimate.** The circuit check and the `on_unpriced_model="refuse"` door
  refusal — previously inline in the api's `_Hooks.before` — are now
  `Engine.admit(session, provider, model, request)`, one named phase every
  wrapped call passes through before it goes out. Opt-in and **off by
  default** — `budget_admission: bool = False` — because estimation is not
  deterministic and this product's identity is: `budget_usd`'s post-call
  wall stays the default reaction, exact and unchanged. Set
  `budget_admission=True` to also refuse a call whose *estimated* cost would
  cross `budget_usd` before it goes out: `estimated_tokens(chars of the
  request's messages)` at the model's input rate, plus the request's own
  output-token cap (`max_tokens` / `max_completion_tokens` /
  `max_output_tokens`, read by a new `request_output_cap(kwargs)` in each
  wrapper) or, absent one, the new `admission_output_tokens: int = 1024`, at
  the output rate — the same price table the post-call check prices with. An
  unknown model skips the estimate for that call and warns once per model,
  rather than inventing a limit the customer never set (the post-call wall
  still watches it). A refused admission **never latches** — a cheaper call
  minutes later may still fit — and is alerted once per session
  (`details["rule"] == "admission"` keeps it from being deduped against an
  unrelated post-call `budget` trip in the same session). `call_before` (and
  `_Hooks.before`) now also pass the request's raw kwargs through, tolerant
  of hooks written before this release the same way an older, model-less
  `before` was already tolerated. With `budget_admission` left off, every
  existing call path is unchanged.

### Wave H (the rule lives on the tool)

- **`@runbound.tool` states the rule, in the same line as the function.** No
  policy file, no CLI, no second configuration surface to drift from the code:
  `blocked=True`, `max_calls=n`, `constraint=predicate`,
  `require_approval=predicate` and `reviewed=True` join the existing `name` and
  `repeatable`. They fold into an ordinary `ToolPolicy` — `blocked` becomes a
  `deny` entry, so the rule name in a refusal, an anomaly and the ledger is the
  one it always was — and `policy.evaluate` and `policy.merge` are byte-for-byte
  unchanged. The builder is `policy.from_decorators()`; the statement itself is
  `policy.ToolRules`.
- **The fold is live, not snapshotted at `init()`.** In a normal module `init()`
  runs at the top and the tools are defined below it, so a policy folded once at
  start-up would see an empty registry and enforce *nothing*. The decorators
  write into a process-lifetime registry and `Engine._local_policy()` composes
  on demand, cached on the registry's version and the configured policy's
  identity — the same object comes back while neither has moved, so the org
  merge above it does not re-key on every tool call.
- **Each tool gets its own approval callback.** `ToolPolicy` asks one callback;
  the decorator gives a different callable per tool, so the fold builds one
  dispatcher that routes on the tool's name, falls through to the
  `approval_callback` configured on `init()` for a tool no decorator claimed,
  and **refuses** when there is neither — an approval nobody can answer is a
  refusal, not a permission.
- **`init(require_rules=True)` is the CI gate.** A `@runbound.tool` that states
  no rule at all raises `ValueError` — named at `init()` for every such tool
  already imported, and raised at decoration for every one declared afterwards,
  which is where they normally are. Any CI step that imports the app fails with
  it, so a tool cannot reach production without a stated rule. A tool that
  genuinely needs none says so with `reviewed=True`.
- **`reviewed=True` is not `ToolPolicy.allow`.** The decorator's `reviewed=True` means
  "reviewed, deliberately unrestricted" and is folded nowhere;
  `ToolPolicy.allow` is a fleet-wide *inverting* allow-list, and putting one
  reviewed tool on it would deny every other tool in the process.
- **`tool_policy=` on `init()` stays** for the two things a decorator cannot
  say — the fleet-wide `allow` list and rules for framework tools that carry no
  decorator of yours — plus `on_violation`, which is a property of the policy
  and not of any one tool. Where both name the same tool the decorator wins,
  with one warning naming it.
- **The tool report carries the rules.** Each entry gains
  `"rules": {...}` as the decorator stated them (`{}` when it stated none), with
  a `constraint` or `require_approval` rendered as `"module:qualname"` — never
  the callable, which is never shipped and never called off-process. This moves
  `tools_hash`, which resends each worker's report once, by design.
- An unenforceable decorator keyword (`max_calls=0`, a `constraint` that is not
  callable, `blocked=True` with `reviewed=True`) raises `ValueError` at decoration,
  where it was written, exactly as a bad `init()` argument does.

### Changed

- README: a "Fleet mode" section (when the plane is contacted, what it adds,
  what goes on the wire, and reading the link), a "What the SDK actually sees —
  and what it never sees" sensor matrix, five new rows in the reactions table
  (`on_halt`, fleet budget, remote latch, org policy dry-run, fleet circuit),
  and (Wave 31) an "Alerting" section pointing delivery at the control plane's
  own docs, with the receiver-side `verify_webhook_signature` walkthrough kept
  here.

### Docs

Wave 25 ("freeze and sharpen") — documentation only, no code changes:

- **A formal definition of "turn"** replaces the informal `workers × one
  in-flight turn` phrasing everywhere it appeared (`INVARIANTS.md`'s Budget
  section, README's fleet-budget paragraph): a turn is one
  `runbound.session()` block on one worker, and the fleet-budget bound is
  stated as a sum over workers of in-flight spend plus spend admitted while
  stale.
- README's stream-abandonment text now says plainly that exhausting, closing,
  or exiting the `with` block reports a stream immediately — garbage
  collection is the safety net for a forgotten stream, not the mechanism to
  rely on.
- README now opens with the problem runbound solves (runaway loops,
  uncontrolled spend, tool abuse, cascading provider failures, unstoppable
  fleet-wide execution) before the tagline.
- A "What it controls" table (README) groups every knob by area — cost,
  execution, authorization, reliability, governance — instead of by
  detector name.
- The provider-compatibility claim is narrowed: OpenAI and Anthropic are
  tested against the real SDKs; OpenAI-compatible servers are proven live
  only on Ollama, with vLLM/Groq/OpenRouter/Azure OpenAI/LM Studio sharing
  the code path untested; Gemini/Bedrock/Mistral/Cohere have no adapter.
- Quick start gets a fourth, numbered step — "Check what is actually
  guarded" — putting `runbound.coverage()` and `runbound.assert_guarded()`
  in the startup path, since an unguarded path looks exactly like a quiet
  one.

## [0.2.0] - 2026-09-01

### Changed

- Renamed the package to `runbound`; the earlier working name was retired.

### Added

- **LangChain / LangGraph support** — `GuardrailCallbackHandler` in
  `runbound.integrations.langchain` records framework-driven tool and model
  calls, so agents with no client to `wrap()` and no function to decorate are
  guarded too. Requires `langchain-core` (extra: `runbound[langchain]`).
- **Async clients** — `AsyncOpenAI` and `AsyncAnthropic` are guarded through
  the same `wrap()` call.
- **Streamed responses** — a guarded stream yields provider chunks unchanged
  and records exactly one model call when the stream ends. OpenAI reports
  stream usage only when the request sets `stream_options={"include_usage":
  True}`; a stream that is abandoned rather than exhausted or closed records
  nothing.
- Packaging metadata (authors, MIT license, classifiers, keywords, project
  URLs), a `LICENSE` file, this changelog, and a GitHub Actions CI matrix
  running the test suite on Python 3.10–3.13.

## [0.1.0] - 2026-09-01

### Added

- Core SDK: an in-process session that records every model call and tool call
  as an immutable event.
- Four deterministic detectors — `loop`, `budget`, `velocity`, `steps` — all
  plain counting over in-memory state, no model calls.
- `wrap()` for OpenAI- and Anthropic-shaped clients (recognized by shape, so
  Azure OpenAI, Ollama, vLLM, Groq, OpenRouter and other compatible endpoints
  work too), and the `@tool` decorator for tool functions.
- Reactions on anomaly: `warn`, `raise` (`GuardrailTripped` on the agent's own
  thread), or your own `callback`.
- Slack and PagerDuty alerting, fire-and-forget on a daemon thread.
- Cost estimation from a static list-price table, overridable via
  `custom_prices`; token limits for unpriced and local models.
- Fail-open throughout: every internal failure is logged and swallowed.
