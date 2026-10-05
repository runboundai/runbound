# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.2] - 2026-10-05

### Fixed

- **A second outage of the same provider is reported again.** A worker paged a provider's circuit once for the life of the process
  (`Engine._alerted`, keyed on the provider label and never cleared), so in a long-lived worker the first outage was reported and every
  later one, hours or days afterwards, was silent. The memory is now forgotten when the circuit closes (a successful probe, or the
  fleet closing it), so one outage pages once however many retries run into it, and the next outage pages again.

## [0.11.1] - 2026-10-04

### Fixed

- **Claude pricing.** The Claude 5 family (Fable 5.1 and 5, Mythos 5.1 and 5, Opus 5.5 and 5, Sonnet 5.5 and 5) now has price rows: until now each was unpriced, so a money budget counted it as $0.00 or, under `on_unpriced_model="refuse"`, refused it. Opus 4.6, 4.7 and 4.8 were matched to the retired Opus 4 row ($15 / $75) and over-charged three times; they are $5 / $25. A one-hour cache write was priced as a five-minute one (1.25x instead of 2x), an under-charge: the wrapper now reads `cache_creation.ephemeral_1h_input_tokens` and prices it at the new fifth column of a price row. Every row carries its read date (`PRICES_AS_OF`, now 2026-10-03) and source URL, and a test pins the table to the published list.
- **Admission for newer Claude tokenizers.** Claude 4.7 and later count about 30% more tokens than the characters-over-four input estimate; budget admission scales its input estimate by 1.3 for them (`runbound.pricing.token_estimate_factor`).

### Added

- **Price multipliers a request carries.** `inference_geo="us"` (1.1x, Claude 4.6 and later) and `speed="fast"` (2x on Opus 5.5, Opus 5 and Opus 4.8) are applied to the cost, and to the admission estimate, only when the request itself includes the field (`runbound.pricing.request_multiplier`). What the SDK cannot see (an account-level setting, the Batch API, server tools) stays a documented limit in "What the SDK actually sees".
- `custom_prices` accepts a fifth number, the one-hour cache-write rate.
- **A stated limit of this release.** The `inference_geo` and `speed` request fields are priced as Anthropic documents
  them on 2026-10-03; their spellings and any beta header they may need are not exercised against the live API in this
  release.

## [0.11.0] - 2026-10-03

### Added

- **`plane_status().applied_policy_version`**: the org policy version this worker has actually fetched and installed
  (`None` until one has been, and without a plane). Not the version the last heartbeat announced, so a policy write
  can be checked as having reached this worker. Read-only; nothing new on the wire.
- **`runbound[otel]`: OpenTelemetry export.** `from runbound import otel; otel.enable()` sends anomalies, refusals,
  posture changes and runtime changes as log records (a Decision's fields as `runbound.decision.*` attributes) and
  five counters (`runbound.refusals`, `runbound.anomalies`, `runbound.posture_changes`, `runbound.guarded_calls`,
  `runbound.estimated_usd`; the last two tick once per guarded model call). A session key leaves only
  as its hash. It does nothing, and returns `False`, when the packages are absent. The local event sinks now also
  receive the raw session key of an anomaly or refusal (never `events()` or `on_event`), so a sink that leaves the
  process can redact it.

## [0.10.0] - 2026-10-03

### Added

- **`runbound.check()` and `python -m runbound check`: what is guarded in this process, in one report.**
  Wrapped clients by provider, decorated tools with their capability classes, whether a plane is connected (its
  address, never its key), the posture in force, the budgets and limits, and the last ten events. It makes no
  network call. `check()` is called in your own process; `python -m runbound check [--json] [TARGET]` loads a script
  or module first (as `runbound_check`, so an `if __name__ == "__main__":` block does not run) and exits `0` when
  something is guarded, `1` when nothing is, `2` when the target cannot be loaded. `--json` has a stable shape
  (`schema` 1). A new guide, "Attach with an agent", is a prompt a coding agent follows to attach the SDK or the
  gateway, and a test checks that every command in it exists.

### Changed

- The configuration reference is grouped by purpose (budgets and limits; loops, spikes and failing providers;
  postures and tools; the plane; privacy; other), with each parameter's type, its real default and whether a
  connected plane can tighten it. Three `init()` parameters the old page never listed are there now:
  `budget_window`, `run_budget_usd` and `run_max_total_tokens`. A test reads the page against `GuardrailConfig`,
  so a parameter cannot be missing, duplicated, or shown with a wrong default. The repository manual is the
  canonical one; the site renders it at build time.

## [0.9.0] - 2026-10-02

### Changed

- A refusal's `decision.level` (and `exc.scope["level"]`) now names the scope that really decided,
  where it used to read `"session"` for all of these: an open provider circuit and a capability
  rule given to `init()` say `"process"`; a refusal because the run is stopped, or because a
  tool's class is denied by a posture, says `"session"`, `"process"` or `"fleet"` according to
  whose posture did it (a session's own safe mode, `runbound.enter_safe_mode`, or a posture or
  Narrow halt the control plane states). When several narrowings apply, the one that is actually
  the effective posture is named, along with its `source` and `reason`, rather than the first
  one set. Code that compared `decision.level == "session"` for these refusals should read the
  boundary and `level` together.

## [0.8.0] - 2026-10-01

### Fixed

- A dollar reservation's refusal no longer shares an alert key with the budget wall's own
  alert for the same session. Its anomaly now carries `details["reservation_level"]` where it
  carried `level`, so under `on_anomaly="warn"` the wall's alert is no longer dropped after a
  reservation refusal.
- A malformed limit value from a control plane is now ignored for that limit alone;
  the rest of the plane's limits still apply. Before, one value that was not a
  number, on a limit your code also caps, set the whole plane body aside. It is
  logged once, at warning level, naming the limit.

### Changed

- A token budget stated by a control plane is now merged like a dollar budget: the
  stricter one wins, across the org, service and run levels and against what your
  code sets. It is the control plane's name for `max_total_tokens`, and is
  enforced like it (next item).
- **A token budget is now enforced at the door as well as after the call.** A token limit
  (`max_total_tokens`, or a stricter `budget_tokens` a control plane states) is reserved
  like a dollar budget: the call's worst case (its input at four characters a token plus its
  output cap) is held until the call closes out. A call that would cross the limit is refused
  with `GuardrailTripped` before it goes out **only under `on_anomaly="raise"`**; under
  `"warn"` (the default) or `"callback"` the anomaly and its decision are recorded, your
  observers are told, and the call goes out, and the post-call wall stops the next call. A call
  that exactly reaches the limit is admitted. This follows `budget_admission` (default
  `"capped"`, so it applies to requests that state an output cap), needs no price (an
  unpriced model is reserved too), and is per worker: the fleet total is applied at entry. The
  dollar door is unchanged and still refuses whatever `on_anomaly` says. Set
  `budget_admission=False` to keep the post-call wall alone.

## [0.7.0] - 2026-09-30

### Changed

- **A loop is now answered in three rungs instead of stopping the session at
  the first repeat.** `on_loop=None` (the default) now means the new
  `on_loop="graded"` policy. Before, a call repeated `loop_threshold` times
  (3) was a critical anomaly that, with `on_anomaly="raise"`, latched the
  session with no way back until you cleared it. Now, 3 is worth a line, 6 is
  worth a person, 9 is a runaway: the third repeat is a warning in your log and
  in `runbound.events()` (reacted `notify`, so it pages no route); the sixth
  (`loop_alert_threshold`, twice `loop_threshold`) is a critical that pages your
  alert routes and stops nothing; the ninth (`loop_contain_threshold`, three
  times) hands the loop to the spike ladder: the session is limited to the
  `restricted` posture, so financial, external and destructive tools are refused
  before they execute while reads and writes still run, each further repeat
  spends the ladder's allowance, and the last closes the session for the
  cooldown and lets the key back in across the fleet when it is served, with a
  strike counted as for a spike. The anomalies keep detector `loop`. It covers
  the repeated-call, tool-cycle and retry shapes. Containment needs a keyed
  session, `on_spike="limit"` and `on_anomaly="raise"`; otherwise the ninth
  repeat is one more critical notice that says why it was not contained. A
  normal model call between two repeats no longer heals a session a loop limited.
  **To keep the old behaviour, name it:** `on_loop="break"` stops the session at
  the threshold (what `None` did under `on_anomaly="raise"`); `"throttle"` and
  `"escalate"` are unchanged. `loop_alert_threshold` and
  `loop_contain_threshold` move the rungs.
- **Every branch refused at the fan-out door is its own record.** A branch
  turned away by `max_active_sessions` (rule `active`), `max_session_depth` or
  `max_child_sessions` used to reach the record only once per session and rule,
  however many branches were refused. Each is now recorded and exported as its
  own anomaly and `Decision`, under the same cap and summary as every other
  refusal.

## [0.6.0] - 2026-09-30

### Added

- **The admission estimate's formula is public.** `runbound.pricing` gains
  `admission_worst_case(price, output_tokens, input_chars)`, the dollar worst
  case of one call (its input at the plain input rate plus the output it may
  produce), and `request_chars(request)`, the characters of input a request
  will be billed for. The engine's budget admission uses exactly these, so
  anything that estimates a call the same way composes the same numbers.

### Changed

- **The worst-case admission estimate now counts what providers bill as
  input.** It counts the Responses API's `input` and `instructions`, Anthropic's
  `system` (a string or blocks), the tool definitions (`tools`, and the legacy
  `functions`) and the tool calls and tool results carried in a conversation.
  Before, it read only chat `messages`, so a Responses call estimated no input
  at all and a request's system prompt and tools were skipped: with
  `budget_admission` on, budgets undercounted those calls and admitted some that
  should have been refused. The estimate used when a server reports no usage
  counts the same fields. Nothing is estimated for content whose size cannot be
  known (an image).

## [0.5.0] - 2026-09-30

### Added

- **A session the spike ladder stopped comes back, and says so.** When its
  cooldown is served, the session records its return (a posture change back
  to full), and the key is let back in across the fleet, on every worker,
  rather than only on the worker that closed it.
- **What changed, and how posture moved, now travel as records.**
  `runbound.events()` gains a `"runtime_change"` record whenever a value this
  process runs on moves from one value to another: the `model` a call used,
  the `provider` it went to, and the `policy_version` or `controls_version`
  a control plane delivered. Only on a real change: never on the first value
  seen and never while it stays the same. Posture records gain `"from"` (the
  posture they replaced), `"scope"` (`"session"` or `"process"`), `"level"`
  (the spike ladder's rung, when the ladder moved it) and, for a session, its
  `"key"`; a session narrowed by the ladder, by hand, by the soft budget line
  or lifted by `runbound.clear()` is now recorded, not only the process's own
  posture. With a control plane connected, both kinds are sent to it on a
  priority lane, with the session key as a hash and never raw; a posture
  change is sent even with `export_events=False`, like a trip, while a
  runtime change is telemetry and follows that setting. No record carries a
  prompt, a reply or a tool argument.
- **Every model call's event names its provider.** `Event` and its wire form
  gain `provider`, the endpoint label the call went to
  (`"openai@api.openai.com"`), from every wrapped client, `record_call()` and
  `@runbound.llm`.
- **A partial outage now reads as one.** `PlaneStatus` gains `entries_window`
  (`{"plane", "cache", "local"}`, a trailing one-minute count of how session
  entries were actually decided), `entries_local_share` (the local share of
  that window) and `reason`. A plane that answers every heartbeat but keeps
  missing `control_plane_timeout_s` on the hot `/v1/enter` path — the
  previously invisible case, since one entry timeout is a single failure and
  the next successful heartbeat reset the old count before three ever landed
  in a row — now degrades `mode` on its own once the window holds enough
  entries to mean something, with `reason="entry timeouts"`. The older,
  coarser case (the link has stopped answering anything at all) still
  degrades the same way and now says so: `reason="heartbeat failures"`.
  `coverage()["fleet"]` carries the same distinction in one sentence —
  `"connected"`, `"connected; N% of entries in the last minute were decided
  locally (plane timeouts)"`, or the existing `"local protection active;
  fleet coordination: not connected"` — and `coverage()["fleet_pending"]`
  reports the exporter's own queued-record count alongside it. The
  heartbeat now carries this worker's own local share too, so a connected
  console can show how many of a service's workers are deciding locally
  right now. See the "What connected means" section of the fleet mode guide.
- **A plane that has lost its own state says so, instead of answering as if
  nothing were wrong.** A hello reply can now carry `fleet_state:
  "unavailable"` — the plane is answering (the link is fine) but cannot
  currently read the store every halt, latch and fleet budget lives in.
  `RemoteState` treats this as plane loss for state, not for the link: a
  halt, its posture and the policy/Controls versions are all held exactly
  as they were rather than read from the reply's own "nothing to report"
  defaults, so an absent halt is never mistaken for a lifted one.
  `plane_status().mode` reads `"degraded"` with a third `reason`,
  `"fleet state unavailable"`, within one heartbeat; `coverage()["fleet"]`
  reads `"connected; fleet state unavailable: deciding locally, last halt
  and posture held"`. `stale_halt` applies the same as it always has, except
  its window now starts at the moment fleet state was first found
  unavailable rather than at the last successful heartbeat — a dead plane's
  own store outage does not stop it from answering its heartbeats, so the
  old clock would never let a held halt go stale under `stale_halt=
  "release"`. When the state comes back — a heartbeat says so, or the plane
  answers a session entry again — `reason` clears within one heartbeat: the
  entries the outage forced a worker to decide locally stay in
  `entries_window` as history but stop counting toward `"degraded"`.
  Timeouts are not cleared this way and keep the window's one-minute
  smoothing. See the new paragraph in the fleet mode guide and the updated
  `stale_halt` row in the reactions reference.
- **The entry window now records *why*, not just that, an entry was decided
  locally.** `entries_window` gains `local_causes` —
  `{"timeout", "plane_loss", "plane_unavailable", "error"}` — a breakdown
  of the existing `"local"` count by cause: a genuine client-side timeout,
  a 503 the plane answered because its own state store is specifically
  unreachable, the same shape of 503 for any other reason the plane could
  not help (a saturated connection pool, a handler bug — never a state
  outage), or anything else. The three existing counts (`"plane"`,
  `"cache"`, `"local"`) are unchanged. When the entry window is what
  degrades `mode` (as opposed to the older consecutive-failure count, or a
  hello's `fleet_state: "unavailable"`), `reason` now names the *majority*
  cause among that window's local entries instead of always saying
  `"entry timeouts"` — a genuine state outage answers `/v1/enter` with a
  503 naming that specifically, and for up to a minute after the outage
  recovers, the window still holds those entries; they now read `"fleet
  state unavailable"`, the same string the hello-based signal already
  uses, rather than the misleading `"entry timeouts"`. Two new reason
  strings: `"plane unavailable"` for a 503 shaped like plane loss but
  naming no specific cause — a plane that is merely overloaded, with its
  state store perfectly healthy, must never be reported as having lost
  its fleet state — and `"plane errors"` for anything else (a connection
  refused, a malformed reply). See the reason table in the fleet mode
  guide.

### Changed

- **Every refusal is recorded and exported, not only the first in a
  session.** A tool or call the SDK turns away (a posture or capability
  denial, a tool-policy block, a budget or envelope door) is now its own
  anomaly and `Decision` in `runbound.events()`, `runbound.decisions()` and
  the export, every time. Before, the second refusal in a session was dropped
  as a repeat, even one of a different tool, so a call refused a refund and
  then a transfer showed only the refund. To keep a runaway loop from
  flooding the record, identical refusals are recorded one by one up to 100
  per session, rule and tool (`runbound.engine.REFUSAL_RECORD_CAP`); past
  that they are counted, and one summary anomaly per rule and tool, carrying
  `details["suppressed_count"]` (how many were not recorded since the last
  summary), is reported when the session block exits, ahead of the session's
  exit record. Two rules or two tools refused at once never hide each other.
  Turning an already-stopped key away at the door is still counted per knock,
  as before, and the loop detector's once-per-session notification under
  `on_loop="throttle"` and `"escalate"` is unchanged.
- **A refusal is exported even when the plan's telemetry cap is reached.** A
  control plane whose monthly event allowance is used up closes the telemetry
  lanes; it no longer silences anomalies that record a refusal
  (`reacted` of `"raise"`, `"blocked"` or `"door"`), which are evidence that
  something was stopped, not telemetry. Everything else on those lanes stays
  closed, and `export_events=False` still turns all of it off.

## [0.4.0] - 2026-09-23

Every runtime control held to the dominant version of itself. Additive
unless a line says otherwise, and every change of behaviour is named where it
happens.

### Added

- **Every refusal site carries a `Decision`, including the fleet ones.** An
  org budget the control plane itself decided, an unreachable plane under
  `on_plane_loss="refuse"`, a fleet-wide halt, and a latch relayed from
  another worker used to carry no `Decision` at all — `exc.decision` was
  `None`, `exc.provider_called` fell back to `False` by default rather than
  by statement, and the completeness this SDK claims for every other
  refusal did not literally hold for these four. Each now carries one:
  `level="fleet"`, `provider_called=False` (none of the four is ever
  decided after a provider call), and a `boundary` — `"money"`, `"plane"`,
  or `"halt"` — read off the relayed detector name. None states
  `limit`/`used`/`reserved`/`estimate`/`remaining`: what reached this
  worker is a verdict and a name, not the numbers behind it, and a made-up
  number would be less honest than an `evaluation` that says only what is
  actually known.

- **Run and key budgets, named as such.** `budget_usd` stays the **key**'s
  budget — the identity behind a `session(key)`, which can live for months —
  and gains `budget_window`: `"hour"` / `"day"` / `"month"` (a calendar UTC
  boundary) or a float number of seconds (a rolling window), governing every
  cumulative number on the key (spend, tokens, the estimated-cost slice),
  which reset together the moment the window rolls over. In memory only: a
  restart starts a fresh window. New, independent controls `run_budget_usd`
  and `run_max_total_tokens` are the **run**'s own budget — one
  `session(key)` block — reset to a fresh number on every entry, exactly as
  `max_session_seconds`'s own clock already is. A call is checked against
  whichever of the run's and the key's own remaining is tighter, and
  `Decision.level` (`"run"` or `"key"`) says which one actually bound a
  refusal. A keyed session with a real `budget_usd` and no `budget_window`
  warns once after living past 24 hours — spend on it will otherwise
  accumulate for as long as the key is reused, which is almost never what a
  customer meant for a key that represents a person or a tenant. See
  [docs/guides/runs.md](docs/guides/runs.md#key-and-run-are-two-different-things).

- **`ExecutionRefused` — the refusal contract.** The exception runbound
  raises is now publicly named for what it is: a structured, retry-aware
  dependency failure, never a fake success and never a provider SDK's own
  exception. `GuardrailTripped` is the identical class under its old name,
  permanently (`runbound.GuardrailTripped is runbound.ExecutionRefused`) —
  every handler already written as `except GuardrailTripped:` keeps
  working. Every refusal now exposes `reason` (a stable code from a
  15-value closed set: `budget`, `tokens`, `steps`, `time`, `posture`,
  `policy`, `approval`, `circuit`, `concurrency`, `blast_radius`, `halt`,
  `plane`, `loop`, `error_storm`, `spike`), `boundary`, `decision`
  (unchanged), `retryable` (`False` for every reason a bare wait cannot fix,
  `True` — with a `retry_after` for `circuit` and `concurrency` — for the
  three reasons where the dependency itself said "not right now" rather
  than "this is against the rules"), `provider_called` (`True` only for a
  post-call budget crossing, where the call already happened and its result
  is withheld — `False` for every other, pre-call refusal), `scope`
  (`{"level": ..., "key_hash": ...}`) and `refusal` (unchanged). New:
  `runbound.is_retryable(exc)`, the predicate a retry loop should check in
  place of guessing from an exception's type or message. No refusal ever
  subclasses a provider SDK's own exception, so a provider's own retry
  layer — or a bare `except openai.APIError: retry` — never mistakes a
  refusal for its own kind of failure worth retrying, whatever `max_retries`
  is set to. New guide, `docs/guides/handling-refusals.md`; new table in
  `docs/reference/reactions.md`.

- **Complete Decisions.** The money reservation's own `Decision` used to
  state only `estimate`/`remaining`; `limit` and `reserved` were in the
  anomaly's own details but not on the object an exception actually hands
  the caller. Both are now on it too. Every refusal's `Decision.evaluation`
  now also states `provider_called: bool` — including two api-level entry
  doors (the fan-out limits, the in-flight cap) that predate the `Decision`
  object and previously omitted it.

- **A `stopped` posture now actually stops model calls, not only tools.**
  Every posture but `stopped` already kept serving model calls, as the
  posture table always said; `stopped` itself only refused a decorated
  tool's own capability classes, so a process (or a session) narrowed to
  `stopped` by hand could still call the provider. `Engine.admit` now runs a
  posture check first, before even the circuit, for a model call reached any
  way — a wrapped client's sync, async or streamed `create`, or a
  `@runbound.llm`-decorated function — so no provider call ever begins while
  the effective posture is `stopped`. The refusal carries `detector=
  "safe_mode"`, `boundary="posture"`, and latches only when the abuse
  ladder's own closed rung is what set it (that path already latches the
  session the moment it closes); a manual or plane-directed `stopped` never
  latches here — lifting it is exactly what lets calls through again.

- **Four action counters instead of one.** `SessionState` used to bump a
  single `executed_actions` the moment a tool call's event was recorded,
  before admission (posture, a capability rule, `max_actions_per_run`) or
  the tool policy had a say — so a refused attempt still counted as
  executed, and `max_actions_per_run` refused one attempt earlier than its
  own message said it did. `requested_actions` (a model asking for a tool,
  unchanged), `admitted_actions`, `executed_actions` and `refused_actions`
  are now four separate counters, and `executed_actions` only rises once an
  attempt has actually cleared admission and the tool policy —
  `max_actions_per_run` is measured against it. `tool_calls()` and the loop
  window are unaffected: they still count every attempt, refused or not, the
  same reasoning that lets a refused repeat still trip a loop. `envelope()`'s
  `execution` object and `session_status()`'s new `actions` object both
  report all four.

- **Safe mode, capabilities and postures.** A tool declares the
  capability classes it carries — `@runbound.tool(effects={"financial"})`, from
  `read`, `write`, `external`, `financial`, `destructive`, `privileged` — and a
  **posture** says which classes may run right now: `full`, `restricted`,
  `read_only`, `no_side_effects`, `stopped`. A run that gets hot keeps thinking
  and loses the right to act. While a session
  (`session.enter_safe_mode(posture=…)`), the process
  (`runbound.enter_safe_mode(posture=…)`, `exit_safe_mode()`,
  `runbound.posture()`, `safe_mode()`) or the fleet (a per-service `posture`
  field on the heartbeat reply, tighten-only) is narrowed, a tool is refused
  before its body runs whenever the posture denies **any one** of its classes —
  and an unclassified tool is refused by every posture but `full`. The refusal
  is `SafeModeViolation`, a `PolicyViolation` with detector `safe_mode` and a
  built-in 403 "That action isn't available right now."; it names the posture
  and the class it denied, and it latches nothing. `init(capabilities={class:
  verdict})` states a class rule that holds whatever the posture is, and
  `init(postures={name: {class: verdict}})` edits a row of the table. One
  function decides, `runbound.posture.Posture.allows`. Undecorated tools are
  never refused by a posture. `coverage()` gains `posture` and
  `tools_by_class`, `session_status()` gains `posture`, and the tool report
  gains a seventh key, `effects`, so every worker's tools hash changes once.

- **A soft line under the budget, and a readable budget.**
  `budget_soft=0.8` warns once at the first call that takes a session past 80%
  of `budget_usd` (detector `budget`, `details["limit_hit"] == "budget_soft"`);
  it never latches. `on_budget_soft="safe_mode"` also puts the session in safe
  mode until spend is back under the line or the key is cleared.
  `runbound.budget(key=None)` returns a `BudgetView` — `limit`, `spent`,
  `remaining`, `soft_at`, `window`, `resets_at`, `scope` — and
  `session_status()` carries the same numbers under `"budget"`.

- **The execution envelope and the Decision.** The runtime was
  `execute -> observe -> detect -> react`; it is now
  `admit -> execute -> reconcile -> record`. With `envelope=True` (the new
  default), a model call is checked at the door — circuit, unpriced model,
  `max_steps`, `max_session_seconds`, the `max_total_tokens` half of `budget`
  for a request stating its own output cap, then the money hold — before it
  goes out, instead of only being discovered over after the call already
  happened; a tool action is checked the same way — posture, a capability
  class rule, then a new limit, `max_actions_per_run`. Every refusal **at the
  door** — before a call or an action happens — now carries a `Decision`
  (`runbound.Decision`; `exc.decision`, also `anomaly.details["decision"]`):
  its verdict, which boundary of the envelope it hit, the detector, and the
  numbers behind it (`limit`, `used`, `reserved`, `estimate`). A **post-call**
  detector trip (`loop`, the post-call `budget`/`steps`/`timeout` walls,
  `spike`, `error_storm`) does not carry one — `exc.decision` is `None` for
  those — because `detectors.py` is deliberately out of scope here.
  `runbound.envelope(key=None)` reads the whole picture back as one object —
  money, steps, time, actions and capabilities — composed from `budget()`,
  the session's counters, `posture()` and any class rules; `None` before
  `init()`.

  The three door stages that stand in front of a **pre-existing** wall —
  `max_steps`, `max_session_seconds`, and the `max_total_tokens` half of
  `budget` for a request stating its own cap — honor `on_anomaly` exactly as
  that wall always has: **`"raise"` refuses here, at the door**, one call
  earlier than the wall would have. **`"warn"` and `"callback"` both log/alert
  at the door and let the call through** (a customer who asked only to be
  told is not silently upgraded to enforcing) — neither latches and neither
  calls the customer's handler *at the door*: the call's own event still gets
  recorded, and the wall behind the door detects the same crossing on it, for
  real, and reacts exactly once, including invoking the callback and
  latching under `"callback"`, one call later than the door, precisely as it
  always has. One consequence to expect in your telemetry: under `"warn"` and
  `"callback"` a single crossing now produces **two** anomalies rather than
  one — the door's projection (`details["rule"] == "envelope"`) and then the
  wall's own confirmed trip — which is also why the door's carries that rule,
  so neither dedupes the other away. What *runs* is unchanged.
  `max_actions_per_run`, a **brand-new** control with no prior
  wall to stay faithful to, and the money/reservation door (unchanged by
  this release) both refuse unconditionally, ignoring `on_anomaly`, as
  they always have.

  A steps, run-time or `max_actions_per_run` door refusal latches exactly as
  the wall it stands in front of would have; a money or tokens door refusal
  never does, for the same reason the money reservation never has — an
  estimate says nothing
  about the next call. `runbound/admission.py` is new: nine pure stage
  functions, numbers in and a `Decision` out, with no lock, no I/O and no
  session object, so the shape of each boundary is table-driven to test.

- **Three more loop shapes, beside the original "repeat".** The loops
  agents actually fall into are the edit-test-edit cycle, the retry storm and
  the stall — not `search("x")` three times, which "repeat" already caught.
  All four are deterministic and content-blind; general graph inference is
  refused, because it would cross into behaviour understanding.
  `config.loop_shapes` (default `("repeat", "sequence", "retry")` —
  `"stall"` is opt-in) says which of `runbound.config.LOOP_SHAPES` run, in
  what order; an unknown name is a `ValueError` at `init()`, not a silently
  skipped shape.
  - **`"sequence"`**: a period-k *rotation of tool names* (not exact
    arguments — the edit and the test each cycle usually differ),
    `2 <= k <= loop_max_period` (new knob, default `6`), repeated
    `loop_threshold` times back to back.
  - **`"retry"`**: one tool's `tool_error`s reaching `loop_threshold` within
    the trailing `loop_window` failures of *that* tool — distinct from
    `error_storm`, which counts every failure, any tool, any kind, in a
    trailing 60-second window rather than a trailing action count.
  - **`"stall"`**: `loop_stall_turns` (new knob, default `5`) consecutive
    turns that introduce no hash the session has not already seen.
  A `loop` anomaly's `details` gains `"shape"`, `"period_tools"` (the
  rotation's tool names, `[]` for `"stall"`), `"repeats"`, `"started_turn"`
  and `"usd_inside_loop"` — the model spend since `started_turn`, exact when
  the loop began within the trailing `spike_window` turns and an honestly
  documented lower bound otherwise (`SessionState.recent_calls` is the only
  per-call cost history a session keeps, and it is bounded by `spike_window`).
  Never `args_hash`, or anything else that could leak call arguments, in
  `message`. `@runbound.tool` gains `idempotent=` and `retryable=`, both
  purely declarative today — reported in the tool report and the control
  surface, enforced by nothing yet.

- **The plane delivers Controls.** A `Controls` body —
  per-level `limits` (`budget_usd`, `max_steps`, `max_events`,
  `loop_threshold`, `max_cost_per_call_usd`, `max_call_seconds`,
  `max_tokens_out_per_call`), `capabilities`, `posture`, per-detector
  `detectors` (`{name: {"action": "stop"|"notify", "mode":
  "shadow"|"enforce"}}`) and `envelope` — now reaches a connected worker on
  the heartbeat, the same way the org policy does: `HelloReply.controls_
  version` (new field, both sides of the wire) changes, the worker fetches
  `/v1/controls`, and the body is tightened against what your own `init()`
  already configured — **never the reverse**. A field the plane states that
  would loosen your own value is refused, your own value is kept, and the
  fact is reported back on the next heartbeat (`controls_refused`) for the
  dashboard's Controls tab to show. `runbound.coverage()`'s new `can_stop`
  key answers whether this process's own `on_anomaly` can actually stop a
  run (`in ("raise", "callback")`); a process where it cannot logs one
  WARNING at `init()` — "runbound is in warn mode: nothing will be
  stopped" — and the same fact rides every heartbeat for the plane's own
  warn-mode badge.

  **Per-detector `action`/`mode`, tightened against this worker's own code
  in *both* directions — this is `can_stop`'s whole reason to exist.** Each
  detector's own code baseline is table-driven (`Engine.
  _local_detector_action`), not one blanket default: `circuit` reads
  `on_provider_failure`, `loop` reads `on_loop`, `spike` reads `on_spike`,
  `velocity` (always `severity="warn"`) can never stop at all, and every
  other name (`budget`, `steps`, `events`, `error_storm`, `timeout`) reads
  `on_anomaly`. A plane `"notify"`/`mode: "shadow"` that would *loosen*
  that baseline is refused — the worker keeps stopping exactly as its own
  code said, and the refused field is reported (`detectors.<name>.action`
  and/or `.mode`) — so a raise-mode worker's walls, money included, cannot
  be switched off from a dashboard, silently or otherwise. A plane
  `"stop"`/`"enforce"` that *tightens* past a `"notify"` baseline (for
  example: code `on_spike="notify"`, a plane `spike: stop`) is applied
  for real only when this worker `can_stop` (`on_anomaly in ("raise",
  "callback")`); under `on_anomaly="warn"` it is not applied at all —
  forcing a `GuardrailTripped` nothing catches is exactly the bug fixed
  for the local case, not a remote feature — and is
  reported with `"reason": "cannot_stop"` instead, so the dashboard's
  warn-mode badge and this report tell the same story. `mode: "shadow"`
  never stops anything either way, whatever `can_stop` says. A
  warn-severity anomaly (a spike still watching, an escalating loop not
  yet confirmed, `velocity`, always) is never forced to stop by any
  control — the existing rule, unaffected.

  **Enforced at the session/process level only**: `agent`, `key`, `action`
  levels are carried and shown, never enforced, until a scope registry
  exists. `capabilities` and `envelope` merge through the existing
  paths; `posture` is unaffected — it still rides `HelloReply.posture`
  alone. The shared `controls_cases.json` fixture that
  proves the SDK's own merge (`runbound.controls_merge`) agrees with the
  control plane's own merge module case for case moved to `runbound-sdk/
  tests/fixtures/` from the plane's own test tree — the SDK cannot import
  the plane, so the one canonical copy lives on the side that can be read
  from.

- **Kill switch: Stop and Narrow, with a convergence number.** A
  fleet-wide halt now carries a `mode`: `"stop"` (unchanged — refuses every
  guarded session at the door) or `"narrow"` (states posture `restricted`
  fleet-wide — model calls keep serving, every non-read decorated tool
  refuses `SafeModeViolation`). `HelloReply.halt_mode` (new field, both
  sides of the wire, default `"stop"` so an older plane's halt keeps its
  old meaning exactly) carries it; `Engine.halt_posture()` reads it,
  tightened into `effective_posture()` from its own source, `"halt"` (new
  entry in `runbound.state.POSTURE_SOURCES`) — deliberately independent of
  the Controls-stated posture (`"plane"`), so lifting one can never
  clear the other. `on_halt` (`"raise"`/`"warn"`) now gates Narrow the same
  way it always gated Stop: `"warn"` never installs the posture at all
  (logged once a minute instead — "Fleet narrowed by the control plane...")
  rather than force a `SafeModeViolation` into code that asked never to be
  stopped by the kill switch. Every worker acks a halt it has seen and
  applied on its next heartbeat (`halt_ack`, staleness-aware — a worker
  that gave up enforcing a stale halt under `stale_halt="release"` stops
  acking it too); the control plane stamps `converged_at` on the halt event
  once every worker with a heartbeat in the last `2 x poll_s` has acked,
  and the dashboard's kill switch shows the live count and, once
  converged, the time it took, per service and org-wide, with history.

- **Circuit: rate mode, slow calls, `prevented`, fleet-wide.** The
  provider circuit gains `circuit_mode`: `"count"` (default, unchanged) or
  `"rate"` — [resilience4j](https://resilience4j.readme.io/docs/circuitbreaker)'s
  sliding-window model. Below `circuit_min_calls` (5) calls in
  `circuit_window_seconds`, nothing is judged; at or above it, either the
  fraction that failed crossing `circuit_failure_rate` (0.5) or the fraction
  slower than `circuit_slow_call_seconds` (`None`, off) crossing
  `circuit_slow_rate` (0.5) opens it — a slow-call rate can open the circuit
  with zero errors, which failure-counting alone never could. Both lines are
  strict `>`; an ordinary (non-slow) success in rate mode only adds a data
  point, unlike count mode's reset-on-success. A **streamed** call's slow-call
  measure is time-to-*first*-chunk, not the whole stream: a long, healthy
  answer (the exact shape of a chat UI or long-form generation) must never
  read as "slow" just for taking a while to finish saying it — only whether
  the provider started answering promptly. Everything else about a stream —
  its full duration for usage and spike detection, its token counts — is
  unaffected; a call that fails, at any point, is a failure regardless of
  timing, and a stream abandoned before ever being read reaches neither side
  of the circuit. `circuit_half_open_calls` (default **1**, the prior
  single-probe rule) admits that many probes at once, in either mode, before
  the next is refused. `CircuitOpen.retry_after` is a snapshot taken the
  instant it is raised — the breaker's own clock and the cooldown remaining
  then — and decays from that on every read, never from a number frozen
  once, and never from a live engine looked up later (which would answer
  wrong, or not at all, once a later `runbound.init()`/`reset()` swaps the
  breaker it actually opened under out from under it).
  `CircuitBreaker.snapshot()` gains `prevented`, per label: calls
  `on_provider_failure="open"` actually turned away, reset when the circuit
  fully closes. `circuit_posture` (opt-in, default `False`) narrows the
  *process* to `restricted` (source `"circuit"`) while a circuit sits
  half-open, lifting only that source when it closes — there is no
  per-provider tool scoping in this SDK, so this is process-wide, the same
  mechanism a manual call or the ladder use.

  On the plane: a per-service circuit, already folded from every worker's
  own transitions, is now explicitly a two-way opt-out —
  `circuit_fleet: bool`, **default `True` whenever a plane is connected**
  (a named default change; irrelevant, and never read, with no plane) — and
  a worker that connects while the fleet's circuit for a provider is already
  open starts open on its very first hello. That fleet instruction is never
  forced into a *refusal*: `on_provider_failure` is read after the breaker's
  state, not before it, so a `"notify"` worker sees the same `"open"` state a
  fleet-joining `"open"` worker does and keeps calling anyway, exactly as it
  would for an outage it found on its own.

  `docs/reference/circuit-breaker.md` is rewritten around the two modes.

- **Baselines that survive a restart, and peer baselines.** Connected
  to a plane, every session exit reports this key's *held, trusted* spike
  baseline (never a live median taken mid-spike — that would let a
  sustained abuse run teach itself a new "normal") and the abuse ladder's
  own rung — level, and, while limited, the allowance left. The plane keeps
  one row per key (a new `baselines` table) and hands both back on the next
  entry, from any worker: a worker restarted mid-spike re-enters a limited
  key at the same rung — never reset to level 0 (forgiveness by redeploy)
  and never escalated — and a brand-new key is judged against the
  service-wide median from its first call rather than served a free
  warmup nobody else got. The service median is a median of every key's
  own baseline, one vote per key, never weighted by call volume, so one
  high-volume caller cannot define what "normal" means for a key that has
  never called before.

  Spike detection and the ladder are local and free (`spike_detection`
  defaults `True` with no plane and no enable flag of any kind — see
  "The paid line" below),
  so what this task delivers is the connected half on top of a detector
  that already runs without one: the restored baseline and rung, the peer
  baseline, and the wire shape to carry both. The per-key restored
  baseline and rung ride `EntryDecision` — the same per-key payload
  `strikes`/`generation`/`latch` already do, since `Controls` (the
  heartbeat's per-*service* payload) has no per-key dimension — while the
  service-wide median rides `Controls` itself
  (`runbound.shared.RemoteState.service_baseline()`, read off the same
  Controls body `controls_directive()` is, but never gated on `dry_run`:
  a peer baseline is advisory data, not an enforcement directive). One
  honest consequence, still true under the local-by-default detector: a
  service needs *some* Controls row published (any rollout state) before
  its peer baseline reaches a worker at all — a service with none
  configured never fetches `/v1/controls` in the first place, and the
  session simply keeps judging itself against its own baseline alone.
  `anomaly.details` gains `baseline_source`
  (`"local"` / `"restored"` / `"peer"`) and, whenever a service baseline is
  known, `vs_service` — the call's ratio to it, reported alongside the
  existing ratio to the session's own baseline in the anomaly's message
  ("N× its own normal, M× this service's"). `session_status(key)["why"]`
  gains the same `source` on `baseline` and a new `service_baseline` key.
  None of this changes what `on_spike` does with a confirmed spike — a
  restored or peer baseline only changes what counts as abnormal, never
  the reaction — and without a plane every one of these fields is exactly
  what it always was (`baseline_source: "local"`, `service_baseline:
  None`). `state.spike_baseline_samples` -- the count an exit delta reports
  as this baseline's support -- tracks the consecutive calls that were
  individually clean, not the raw length of the trailing call window: a
  review of a real spike-train probe found the count still growing through
  calls that came after an abnormal one aged out of the ladder's own
  5-call confirm window while remaining in the up-to-`spike_window` (50)
  history the median is drawn from (harmless to the median itself, which
  stays a robust minority-outlier case, but not to a count claiming to
  describe this baseline's clean support). `docs/guides/spike-detection.md`
  is retitled "Progressive degradation" around this: the ladder as the
  automatic driver of postures, never a headline abuse score.

- **Local telemetry is free.** `runbound.events(n=100)` and
  `runbound.decisions(n=100)` read an in-memory ring of this process's own
  anomalies, refusals, posture transitions and Decisions — no account, no
  plane, nothing written to disk, nothing sent anywhere, and no record ever
  carries a call argument, a prompt or a reply. An optional `on_event=`
  callback on `init()` is told about every record as it happens; a raising
  callback is logged and swallowed (fail-open).

### Changed

- **`@runbound.tool(repeatable=True)` is now `polling=True`.** Same
  meaning — a tool that is *supposed* to run with the same arguments over and
  over never feeds the loop window — new name, chosen to read next to the
  new `idempotent=`/`retryable=` keywords. `repeatable=` is gone rather than
  aliased (the package is public but unannounced, so there is no
  compatibility burden): passing it now raises `TypeError`.

- **`@runbound.tool(effect=...)` is now `effects={...}`.** One value on
  one axis became a set of capability classes; `effect=` is gone rather than
  deprecated (the package is public but unannounced, so there is no
  compatibility burden). A tool is refused when the posture denies **any** of
  its classes, and an unclassified tool is refused by every posture but
  `full`. `require_rules=True` now fails an unruled tool carrying `financial`
  or `destructive` — the consequential classes — where it previously named
  `irreversible`. The tool report's `effect` key became `effects` (a sorted
  list), `coverage()["tools_by_effect"]` became `["tools_by_class"]` counting a
  tool under every class it carries, `coverage()` gained `posture`, and
  `session_status()["safe_mode"]` became `["posture"]` carrying the posture's
  name. The spike ladder's limited rung sets `restricted` and its closed rung
  `stopped`; a budget soft line sets `restricted`. On the wire, the heartbeat's
  `safe_mode: bool` became `posture: str | None`.

- **Default change: `budget_admission="capped"`.** `budget_admission`
  was `False`. It now defaults to `"capped"`: a request that states its own
  output cap (`max_tokens`, `max_completion_tokens`, `max_output_tokens`) is
  refused before it goes out when that cap at the model's output rate, plus its
  input estimate, would cross `budget_usd` (`details["reason"] ==
  "reservation"`; it never latches). A request with no stated cap takes 0.3.0's
  path unchanged, and `True` keeps the assumed-cap estimate. Anthropic's
  `messages.create` always states `max_tokens`, so every Anthropic call under a
  `budget_usd` is now reserved. The trade-off, as the docs state it:

  > A call whose cap-priced worst case exceeds what is left is refused even if it
  > would have used less: with $0.10 left, a request capped at 15,000 output
  > tokens on a $10-per-million model is refused although its answer might have
  > cost a cent. That is the price of "cannot be crossed". Two limits are stated
  > rather than hidden: the output side of the worst case is exact, and the input
  > side is the same chars/4 estimate used everywhere else in runbound, so a prompt
  > much denser than four characters a token can still take the session past the
  > line by the difference; and a call with no stated cap is not reserved at all.
  > `budget_admission=False` restores 0.3.0.

- **The reservation now holds for the call's lifetime, not just at the
  door.** The admission check used to compare a call's worst case
  against *settled* spend only, so N calls racing the budget edge at once were
  all admitted — the overshoot window was N × worst case. The estimate that
  passes is now held on the session's own ledger (`session.reserved`) from
  admission until the call closes out, however it ends (return, error, or —
  for a stream — exhaustion, an early close, a mid-flight failure, or being
  abandoned and collected), so near the budget edge a second, otherwise
  generous, stated cap is now refused while another capped call is still in
  flight — exactly `floor(remaining / worst_case)` of N concurrent calls are
  admitted, not N. A refusal now also names what is already held:
  `details["reserved_usd"]`, with `remaining_usd` already reduced by it.
  Under `budget_admission=True` the *assumed* `admission_output_tokens` cap is
  held the same way, so an uncapped call — an open stream especially — holds a
  guess's worth of money for as long as it runs. `runbound.budget().reserved`
  and `BudgetView.reserved` (its last field) read
  what this worker currently holds — worker-local, never folded into fleet
  spend or sent to the control plane — and `session_status()["budget"]
  ["reserved"]` carries the same number. `record_call()` never takes a hold,
  because the call it describes is already over.

- **Default change: `envelope=True`.** `envelope` is a new field,
  defaulting on: `runbound.init()` with no other change now refuses the
  `max_steps`/`max_session_seconds`/(capped) `max_total_tokens` overrun one
  call earlier than 0.3.0 did — at the door, before the offending call goes
  out, rather than after it returns — which is one fewer paid provider call
  per over-limit session. No other observable behaviour moves: money
  admission (`budget_admission`) and posture/capability enforcement are
  unaffected either way, since they are their own, older opt-ins, not new
  envelope controls. `envelope=False` restores 0.3.0's admission behaviour
  exactly, proven by `tests/test_behaviour_golden.py`'s golden fixture,
  captured against 0.3.0 code and still passing byte-for-byte under
  `envelope=False` today.
- **`on_spike="limit"`: the limited rung puts the session in safe mode.**
  Its tools stop acting while `spike_limit_calls` abnormal model calls are
  served; healing or closing lifts it. In 0.3.x a limited session's tools kept
  running.
- **`require_rules=True` fails a tool carrying a consequential capability class
  — `financial` or `destructive` — with no rule that can refuse it, even with
  `reviewed=True`.** A tool that declares no such
  class is judged exactly as before.

### Fixed

- **The action-cap refusal's own sentence miscounted what it was reporting.**
  "Too many actions this run: N taken, limit L" read `N` as
  `executed + 1` — the count this attempt would have brought the tally to,
  the same "count already spent" convention `max_steps`'s own door uses —
  as if it were a count of completed actions, so a cap of 3 with 3 actions
  actually executed read "4 taken". `Decision.evaluation` and
  `session_status()["actions"]` were already correct; only the message
  lied. Now: "the next action would be action N, limit L", the same shape
  the steps door's own message already uses correctly.

- **A warn-severity anomaly never stops the run, in any `on_anomaly` mode.**
  `on_anomaly` is the reaction to a *critical* anomaly, as the reactions table
  has always said, and `velocity` "never stops anything" — but under
  `on_anomaly="raise"` a velocity warning raised `GuardrailTripped` on the
  crossing call (without latching), and under `"callback"` it was handed to
  the kill switch. Both now log and alert only; observers and the plane see
  the reaction as `warn`.

- **The source distribution ships `examples/` and `docs/`.** 0.3.0's sdist
  carried `tests/` but not `examples/`, so its bundled docs-example test
  failed when run from the sdist (the wheel was unaffected). `MANIFEST.in`
  now grafts both directories, and `tests/test_docs_examples.py` skips with
  a reason instead of failing when `examples/docs/` is absent.

### The paid line

Restated here rather than as a Removed-then-Added pair, since nothing
between was ever released: **open-source runtime, cloud control plane.**
Every local, deterministic control is free and local, configurable in code,
with no account. `postures`, `capabilities`, `budget_soft`,
`on_budget_soft`, `max_actions_per_run`, the eight circuit-rate knobs
(`circuit_mode`, `circuit_failure_rate`, `circuit_min_calls`,
`circuit_slow_call_seconds`, `circuit_slow_rate`, `circuit_half_open_calls`,
`circuit_fleet`, `circuit_posture`), the three loop-shape knobs
(`loop_shapes`, `loop_max_period`, `loop_stall_turns`), and the eleven
spike/ladder knobs (`spike_detection` — defaulting `True` again —, `on_spike`
and its nine tuning fields) are all real `init()` keywords. A connected
control plane can only *tighten* what your code configures for these six
controls (circuit rate mode and posture-narrowing, loop shapes, the budget
soft line, `max_actions_per_run`, class-rule capabilities, and spike
detection/the ladder), or state one from scratch where you leave a field
unconfigured — one merge function (`runbound.controls_merge`), the same
algebra whichever field it touches; a looser plane value is refused and the
refusal is visible (`Engine.controls_refusals()`). `enter_safe_mode()` /
`exit_safe_mode()` and `runbound.envelope()` are public again, on the
process and the session — `envelope()`'s object also still rides the
heartbeat. `coverage()` drops `advanced_controls` for a single `fleet` key:
`"connected"` while a plane answers, else the honest "local protection
active; fleet coordination: not connected" — the one nudge, never a
per-call log line. `entitlements` on hello now carries only coordination
and scale facts, never a control name; the control engine reads no
entitlement at all. See
[Free SDK, connected plane](docs/concepts/free-and-connected.md).

## [0.3.0] - 2026-09-12

First public release, as `runbound` (import `runbound`), by Runbound AI.
Earlier versions were internal.

### Added

- **Fleet mode — a control plane, so N workers share one truth.** Connect
  with `token` alone (hosted) or `control_plane_url` plus a `token`
  (self-hosted — `token=""` if that plane needs no auth; see "Two kinds
  of connected customer" below for how the two are told apart), and the
  replicas serving one end-user
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
  the SDK's own `WebhookAlerter` retired (see "Delivery is not in the SDK"
  below): a constant-time
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

- **Honest promises.** A review of the positioning and a few silent gaps
  in the code, fixed together.
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
    release the SDK read only the *facts* a plane decision carried (spend
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

### Added

- **`init()`'s heartbeat now reports coverage.** The hello payload the SDK
  sends the plane carries `coverage: {guarded_calls, decorated_tools,
  providers_imported, providers_unguarded}` — the same numbers
  `runbound.coverage()` already exposed locally — so a connected plane's
  Services page can show, per worker, what is actually guarded; any
  failure computing it degrades to `{}` rather than breaking the
  heartbeat. Shown as counts, deliberately not a percentage.

### Changed

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
- **Delivery is not in the SDK.** A paid feature cannot be enforced by a
  conditional running inside the customer's own process, so the code that
  sent alerts directly left: `SlackAlerter`, `PagerDutyAlerter` and
  `WebhookAlerter`, the PagerDuty constants, the posting helpers and the
  alert rate limiter are gone from `runbound/alerts.py`, and `Engine._alert`
  now only notifies observers. `init()` answers the five retired keywords —
  `slack_webhook`, `pagerduty_routing_key`, `webhook_url`, `webhook_secret`,
  `link_template` — with a `ValueError` naming where the setting actually
  lives now (an alert route, or a per-service field, on your runbound
  dashboard) instead of a bare `TypeError: unexpected keyword`.
  `verify_webhook_signature` and the outbound-thread draining bookkeeping
  stay in `runbound/alerts.py` — the receiver's helper, and what
  `export.py` needs at interpreter exit — and `on_anomaly="callback"` stays
  free and local, a hook into the customer's own process, not a delivery
  channel. This changes nothing about detection: every detector, the
  latch, refusals, tool policy, the provider circuit and the in-flight cap
  are identical with and without a token, `on_anomaly`'s three reactions
  (`raise`, `callback`, `warn`) are alternatives — a raise or a callback
  does not also log — and behave identically with or without one, all
  asserted directly in `tests/test_token_and_delivery.py` and
  `tests/test_plane_modes.py`. See the rewritten invariant in
  `INVARIANTS.md`, "The SDK detects, stops, refuses and reports. The plane
  routes and delivers."

Three fixes to what public knobs mean, landed before the first PyPI
release because each changes the meaning of a public knob — renaming or
re-meaning one after real customers hold a version would break them.

- **`max_session_seconds` measures the run, not the end-user's whole
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
- **Steps are model turns; a new `max_events` counts everything.**
  `max_steps` counted every recorded event before this — a turn with one
  model call and three tool calls read as four steps. `SessionState.turns`
  now counts `llm_call` events only, and `max_steps` (detector `steps`) is
  measured against it. `SessionState.step_count` is renamed
  `event_count` (measuring what `max_steps` used to: every recorded event),
  and a new `max_events` (detector `events`) is measured against it instead.
  `step_count` is kept for one release as a read-only alias for
  `event_count`. `ExitDelta.steps_delta` now carries turns, not the raw event
  count — see "What the plane needs from this" below.
- **Anomaly ties are resolved by a stated order, not list order.**
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

  **What the plane needs from this:** `ExitDelta.steps_delta` on the
  wire now means model turns, not the raw event count it meant before this
  release — a dashboard's "Steps" column reading it should say "model turns."
  This release only prepares the SDK side (`SessionState.turns`,
  `SessionState.event_count`, the `events` detector) and repoints the one
  existing wire field. It does not add a separate wire field for the raw
  event count, `errors_delta`, `tokens_cached_delta`, `last_detector`, or
  the two trigger fields — those are a later, plane-side addition, together
  with the plane-side router, ledger and migration this SDK release does
  not touch.

### Fixed

- **A healed latch re-arms detection.** `latch_ttl_seconds` expired a
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

### Added

- **Admission: a named phase, and an opt-in pre-call budget
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
- **The circuit can read the provider's own rate-limit headers**, opt-in via
  `circuit_reads_quota=True` (default off). A 429's `Retry-After` sets that
  opening's cooldown instead of `circuit_cooldown_seconds`, and a remaining
  bucket at zero opens the circuit until its reset without waiting for
  `circuit_failure_threshold` failures — reported as the usual `circuit`
  anomaly with `details["reason"] == "quota"` (a failure-driven one now says
  `"failures"`). No header may hold a circuit shut for more than an hour.
  Fail-open throughout: an unreadable header says nothing and is never read
  as zero. Note the limit, which is in both provider SDKs rather than in
  runbound — a plain successful call returns a parsed model with no headers
  at all, so a pre-emptive opening happens only from an error response or
  from a call your own code made through `with_raw_response` / `.parse()`;
  runbound never changes how your call is made to get at headers. Streaming
  is out of scope. With the option off, the circuit behaves exactly as before.

### Fixed

- **Anthropic's extended-thinking count is read.** `anthropic`
  1.5.0 states it as `usage.output_tokens_details.thinking_tokens`, one
  level below where the wrapper was looking, so `tokens_reasoning` had
  been reporting 0 for every thinking call. **No bill changes**: thinking
  tokens are a *subset* of `output_tokens` and were always priced at the
  output rate. What you get back is the split — how much of an answer was
  thinking. Found by replaying a real recorded response; no test could
  have caught it, because every test built its own usage object.


### Changed

- README: a "Fleet mode" section (when the plane is contacted, what it adds,
  what goes on the wire, and reading the link), a "What the SDK actually sees —
  and what it never sees" sensor matrix, five new rows in the reactions table
  (`on_halt`, fleet budget, remote latch, org policy dry-run, fleet circuit),
  and an "Alerting" section pointing delivery at the control plane's
  own docs, with the receiver-side `verify_webhook_signature` walkthrough kept
  here.
- README and the SDK's own docstrings lead with an autonomous agent: the quick
  start is a tool-using agent run, "Per-user sessions" is now "Runs keyed by
  any id" (a run id, a job, a tenant, a user), the spike ladder is "Many
  callers behind one service", and "end user"/"abuser" read as
  "caller"/"repeat-offender key". Both worked stories stay — a runaway agent
  and a chatbot free-rider. Wording only; no behaviour changed.

### Docs

Documentation only, no code changes:

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
