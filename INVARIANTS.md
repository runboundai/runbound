# runbound — invariants

What this document is: the promises that must hold for "runtime controls for
autonomous AI agents" to be true, stated one sentence at a time, with the
bound each promise actually carries and the test(s) or Acme scorecard
scenario that assert it today. "Acme" (Acme Commerce) is the fictional
business a multi-worker demo fleet runs against elsewhere in this project;
where a scenario against it is cited below, it is a fleet-wide integration
check, never a unit test in this repository. Where nothing asserts a
promise yet, this file says so — "not yet asserted" is a finding, not
something to quietly drop.

Plain-English reading of the terms used throughout — two bounds and the
word this file never lets stand in for either:

- **single-process exact** — the check is a plain comparison against a
  number this process actually holds; there is no slack in it.
- **fleet bound** — across more than one worker there is necessarily a
  window (network round trips, a cache) where two workers can briefly
  disagree; the bound states the worst that window can cost you, in
  concrete units (a number of turns, a number of seconds), not "eventually."
- **estimated** — a number runbound computed because nobody handed it a real
  one: `chars/4` tokens when an endpoint reports no usage, dollars from a
  static list-price table, the opt-in admission estimate. An estimate is
  never called exact anywhere, and the full list of which numbers are which
  is the [What is exact and what is
  estimated](docs/concepts/what-it-sees.md) table.

---

## Budget

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Budget").
What follows is the bound behind that sentence, and the tests that assert
it.

**What is a turn.** A **turn** is one `runbound.session()` block on one
worker, from entry to exit, whatever model and tool calls it contains. A
worker learns the fleet's spend for a key only at block entry, and reuses
that answer for `control_plane_cache_s` (default 5 s); other workers' spend
reaches the plane through exit deltas posted in batches (about one second).
So a worker can be stale for up to the cache window plus batch latency.

**The bound.** Single process: exact — `total_cost_usd > budget_usd` is a
plain comparison against the running total this process holds, strictly
greater than, so exactly at the limit does not trip; the detector trips on
the very event that crosses the limit. With a stated output cap the budget is **not crossed by that call**:
under the default `budget_admission="capped"` the cap priced at the model's
output rate, plus the input estimate, is *held* against what is left before the
call goes out — not compared once and forgotten, but reserved on the
session's ledger (see [Upper bound](#upper-bound)) for the call's whole
lifetime, minus whatever other in-flight calls already hold, and given back
only when it closes out — and a call whose worst case would still cross is
refused there, never latching.
The output side of that bound is exact; the input side is chars/4, so a prompt
much denser than four characters a token can take the session past the line by
the difference. A call with **no** stated cap is checked after it returns: the
wall **stops the session after the call that crossed it**, because usage exists
only then, so that call is paid for and the next one is prevented.
`budget_admission=False` makes every call the second kind (0.3.0); `True` also
reserves an assumed cap for uncapped calls, which is an estimate. The soft line
(`budget_soft`) is a warning under the wall and bounds nothing. The dollars on both sides of that line
come from a static price table, so the comparison is exact and the total it
compares is an estimate of the bill (see the README table named above).
Fleet — Intended bound: for one key, worst-case overspend over `budget_usd` is the sum over
workers of (the spend of that worker's one in-flight block + the spend of
any blocks it admitted during its staleness window). Not yet asserted by a test (needs a live multi-worker race; v1.1).

`budget_usd` is the **key**'s own limit — it accumulates over every run
this key has ever made unless `budget_window` (`"hour"`/`"day"`/`"month"`,
a calendar UTC boundary, or a float number of seconds, a rolling one) says
otherwise, in which case every cumulative number on the key resets together
the moment the window rolls over. `run_budget_usd` is a second, independent
number: the **run**'s own budget, fresh on every `session()` entry. A call
is checked against whichever of the two is tighter, and a refusal's
`Decision.level` (`"run"` or `"key"`) says which one actually bound it — see
[docs/guides/runs.md](docs/guides/runs.md#key-and-run-are-two-different-things).

**How a customer tightens it.** State an output cap on every call, so the reservation applies to every call. Keep blocks short (one end-user request per
block), and lower `control_plane_cache_s` at the cost of more entry requests.
`tests/test_budget_soft_and_reservation.py` asserts the reservation refusing before any socket opens with spend unchanged, the no-cap path identical to `budget_admission=False`, and the soft line firing once without latching. The Acme scorecard asserts the two-worker, one-request-per-block case lands
within one turn of the single-worker wall.

**Asserted by:**
- Single-process exactness: `tests/test_detectors.py::test_budget_silent_when_cost_exactly_at_limit`,
  `::test_budget_fires_when_cost_strictly_exceeds_limit`.
- Fleet offset folding into the local check:
  `tests/test_detectors.py::test_budget_offsets_default_to_zero`,
  `::test_budget_trips_on_local_spend_plus_the_fleet_offset`,
  `::test_budget_silent_when_local_spend_plus_offset_is_exactly_at_the_limit`,
  `::test_budget_offset_alone_can_trip_a_session_that_spent_nothing`;
  `tests/test_session_sync.py::test_the_fleet_spend_offset_trips_mid_block`,
  `::test_the_offset_never_double_counts_this_workers_own_spend`.
- Intended bound: worst-case fleet overspend of `workers × one turn + cache window`. Not yet asserted by a test (needs a live multi-worker race; v1.1). The
  pieces above prove the mechanism (offsets fold in correctly, the check is
  exact once the offset lands), but no test drives an actual multi-worker
  race and measures the overshoot against that general formula. The Acme
  scorecard's scenario 1 ("budget across workers") does assert a number, but
  only for its own two-worker configuration, not the general formula
  (verified by the control plane's demo fleet): it computes the turn a
  single worker would stop on (`want`) and passes only if the fleet's actual
  stop turn falls in `want..want + 1` — i.e. at most one extra in-flight
  turn across the two workers.

---

## Content independence

**One sentence.** Every core runtime decision — a detector's verdict, an
admission refusal — is made from non-content signals: counts, hashes,
timings, prices, declared capability classes. No prompt, reply or tool
result is ever read to decide anything.

**The bound.** Single process: exact, by construction rather than by
scanning. `Event` (`runbound/events.py`) carries no message text at all —
`tokens_in`/`tokens_out` (counts), `cost_usd` (a price-table lookup),
`duration_s`, `model`, `tool_name`, `args_hash` (a salted digest, never the
arguments) — so a detector reading an `Event` has no content to read even if
it wanted to. The admission stages (`runbound/admission.py`) extend the
same property to the door: every stage is a comparison of plain numbers a
caller already read under a lock (`turns`, `elapsed_s`, `total_tokens`,
`executed`, a posture's verdict) — none of them opens a request's
`messages`/`content` field for anything beyond counting characters for the
money and tokens estimates, and even that reads only a length, never the text
itself.

**Asserted by:** every detector test in `tests/test_detectors.py` builds
events from bare numbers and never a string a detector could have "read";
`tests/test_admission_stages.py` calls every stage with plain numbers and no
session, request or message object at all — there is nothing content-shaped
to pass even if a stage wanted one.

---

## Upper bound

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Reservation,
and its upper bound").

**The bound.** Single process: exact, while the check runs and the calls it
covers stay in flight. `Engine._admit_budget` compares a call's worst case
against `budget_usd - (total_cost_usd + spend_offset_usd) - reserved["usd"]`
and, on a pass, takes the hold and writes it to `reserved` in the same
`session.lock` section as the compare — so two calls racing the same
remaining dollar cannot both read "room" before either writes. The hold is
given back — by whichever close-out path the call actually takes: an
ordinary return, a raised exception, or — for a stream — exhaustion, an early
`close()`, a mid-flight failure, or being abandoned and collected — which is
what keeps `reserved` from drifting upward forever. This holds for a call
made through a wrapped client or `@runbound.llm`, under `budget_admission`:
a stated cap (`"capped"`, the default) or the assumed
`admission_output_tokens` cap (`True`). An **uncapped** call under
`"capped"` reserves nothing — nothing exact to reserve against — and is
bounded only by the post-call wall in [Budget](#budget), exactly as it
was before the reservation existed. The input side of the worst case is
still chars/4, the same estimate
[Budget](#budget) already lives with. `reserved` is **worker-local** — this
process's own in-flight money, never folded into `spend_offset_usd` and
never sent to the control plane — so the *fleet* bound is unchanged from
[Budget](#budget): a worker's reservations say nothing to the rest of the
fleet, and the cross-worker window described there still applies once a call
settles. Fail-open still wins over this bound: a `Hold.release` that itself
raises is logged and swallowed, and the money it was holding leaks for the
rest of the run rather than the host's call being failed by our own bug.

**Asserted by:**
- The floor under concurrency, and that it un-reserves back to zero once
  every call resolves:
  `tests/test_reservation_ledger.py::test_exactly_three_of_ten_concurrent_capped_calls_are_admitted`,
  `::test_a_budget_that_is_not_a_multiple_of_the_worst_case_admits_the_floor`,
  `::test_the_same_bound_holds_for_concurrent_async_calls[openai]`,
  `::test_the_same_bound_holds_for_concurrent_async_calls[anthropic]`.
- The hold lasts exactly as long as a stream is open, for every way a stream
  can end: `::test_an_open_stream_holds_the_money_a_second_call_needs[openai]`,
  `::test_an_open_stream_holds_the_money_a_second_call_needs[anthropic]`,
  `::test_an_open_async_stream_holds_the_money_too[openai]`,
  `::test_an_open_async_stream_holds_the_money_too[anthropic]`,
  `::test_closing_a_stream_early_releases_its_hold`,
  `::test_a_stream_that_dies_mid_flight_releases_its_hold`,
  `::test_a_failed_then_closed_stream_releases_its_hold_exactly_once`,
  `::test_an_abandoned_stream_releases_its_hold_when_it_is_collected`.
- Every other close-out path gives the hold back in full:
  `::test_a_provider_error_releases_the_hold_in_full`,
  `::test_a_post_call_trip_still_releases_the_hold`,
  `::test_an_in_flight_refusal_after_the_money_was_admitted_gives_the_hold_back`,
  `::test_a_hold_whose_release_raises_is_logged_and_the_response_still_returns`.
- `@runbound.llm` holds its assumed cap for exactly the decorated body's
  lifetime, sync and async: `::test_the_llm_decorator_holds_its_assumed_cap_for_the_life_of_the_body`,
  `::test_the_llm_decorator_releases_its_hold_when_the_body_raises`,
  `::test_an_async_llm_decorator_holds_and_releases_too`.
- `record_call` never holds anything, because the call it describes is
  already over: `::test_record_call_never_holds_anything`.
- Intended bound: the same upper bound for N concurrent calls spread across
  *workers* racing one shared `budget_usd`. Not yet asserted by a test (needs a live multi-worker race; v1.1). `reserved` is
  deliberately worker-local and the plane never sees it; only the single-worker floor is measured today.

---

## Authorization before reservation

**One sentence.** Every cheap, deterministic check runs before any resource
is held; a hold is taken last, and released if a later stage denies.

**The bound.** Single process: exact, by the order `Engine.admit` runs its
stages in — for a model call, the stopped posture first, then circuit,
unpriced model, then (under `config.envelope`) steps, run time and tokens, and
only after all of those pass does the money hold (`_admit_budget`) run, as the
*last* stage of `admit`. The one check that runs after it is the in-flight cap,
which lives in the api's registry and runs right after `admit` returns; when it
refuses, the hold is given back
(`tests/test_reservation_ledger.py::test_an_in_flight_refusal_after_the_money_was_admitted_gives_the_hold_back`). A call denied by any earlier stage
holds nothing at all — `tests/test_envelope.py::test_a_call_that_breaks_both_money_and_steps_reports_steps_and_holds_nothing`
configures a call that would be refused by both the step limit and the
money estimate and asserts the cheaper check (steps) wins and
`session.reserved` stays empty, never touched. For a tool action the same
ordering holds between posture and the customer's own action policy: a call
a posture would refuse is never handed to a `require_approval` callback
(`runbound/api.py::_announce_call`) — no approver is asked about a call that
never runs.

**Asserted by:** `tests/test_envelope.py` (the stage-order tests above, and
the fail-open tests proving a broken stage costs the call nothing more than
that one check); `tests/test_reservation_ledger.py` (the reservation's own half: the
hold is always the last thing `_admit_budget` does, never released *for* a
later phase because there is no later phase after it).

---

## Latch

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Fleet
consistency bounds").

**The bound.** Single process: immediate — the very next `session()` entry
(under `on_anomaly="raise"`) or the very next guarded call (otherwise) sees
the latch, because it lives in the same process's memory. Fleet — Intended bound: a latch set
on one worker reaches another within `control_plane_cache_s` **plus one
in-flight turn** — the same window the budget bound uses, because both ride
the same cached entry answer. Not yet asserted by a test (needs a live multi-worker race; v1.1).

**Asserted by:**
- Local, single-process latch-before-the-call: `tests/test_latch.py::test_entering_a_latched_session_raises_before_the_body_runs`,
  `::test_a_blocked_user_is_blocked_on_every_later_message`,
  `::test_a_latched_session_skips_detection_entirely`.
- Fleet propagation, refused before the model call runs:
  `tests/test_session_sync.py::test_a_remote_latch_refuses_the_block_before_it_runs`.
- Acme scorecard scenario 2 ("abuser blocked on the other worker") — a
  cross-process demonstration with a real latency measurement proving no
  model call happened.
- Intended bound: a call already in flight when a latch lands is unaffected and the *next* one is refused. Not yet asserted by a test (needs a live multi-worker race; v1.1). For example, a test that starts a call already in flight when a latch
  lands mid-call and shows that call is unaffected while the *next* one is
  refused. What exists proves "the door checks before the body runs," not a
  race between an in-flight call and a landing latch.

---

## Circuit

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Fleet
consistency bounds").

**The bound.** `circuit_failure_threshold` failures inside
`circuit_window_seconds` opens it; it stays open for
`circuit_cooldown_seconds`; the half-open state lets exactly one call
through, and a fleet circuit does the same thing for every worker at once
under one shared decision. That decision is shared, not instantaneous: a
worker that did not see the failures itself adopts the plane's circuit on its
next heartbeat, not the moment another worker opens it. Intended bound: one heartbeat
(`control_plane_poll_s`). Not yet asserted by a test (needs a live multi-worker race; v1.1). The Acme scorecard's scenario 4 observes
around 1.5-2 s from a polling loop; that is an observation, not an asserted bound.

**Asserted by:** `tests/test_circuit.py::test_it_opens_exactly_at_the_failure_threshold`,
`::test_the_cooldown_lets_exactly_one_probe_through`,
`::test_a_successful_probe_closes_the_breaker`,
`::test_concurrent_failures_open_it_once_and_stay_consistent`; also
`tests/test_circuit_force.py`, `tests/test_autowrap.py`,
`tests/test_selfhosted.py` (per-endpoint keying), `tests/test_real_sdk.py`
(against real SDK classes). Fleet-wide circuits are verified by the control
plane's own test suite (adapters, alert routing, the ledger, the live store,
refusals, and the SDK-facing router); Acme scorecard scenario 4 ("one fleet
circuit, one alert").

**One page per outage.** A provider's circuit pages once however many retries run into the
outage, and the page is forgotten when the circuit closes (a successful probe, or the fleet
closing it), so the next outage pages again: open, close, open is two pages, and a retry storm
on one open circuit is one (`tests/test_circuit_repage.py`).

---

## Policy-monotonic

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Policy
precedence").

**The bound.** A per-scope policy version number only climbs, never repeats
and never walks backward, and merging a local rule with a remote one always
keeps the more restrictive of the two — a stricter `on_violation`, the union
of every `deny`, the intersection of every `allow`, the lower of two
matching limits.

**Asserted by:**
- The *merge is stricter-only* half of this invariant:
  `tests/test_policy_merge.py::test_the_stricter_on_violation_wins`,
  `::test_deny_is_the_union`,
  `::test_allow_is_the_intersection_when_both_sides_set_one`,
  `::test_the_lower_limit_carries_the_origin_of_the_side_that_set_it`,
  `::test_a_dry_run_remote_never_changes_the_local_mode`.
- The *merge never permits what either side refuses* half, as a property
  rather than as examples: `tests/test_policy_merge_properties.py` generates
  500 random policy pairs from a fixed seed (stdlib `random`; no new test
  dependency) and, over 11,900 (tool, attempt-count) probes, asserts that a
  call either input refuses is refused by the merged policy too — plus that
  the merged `on_violation` is never looser than either side's. It generates
  remote policies wire-shaped (no callables, since none can travel) and asks
  a remote approval rule of the local callback, which is what the merged
  policy does with it. It says nothing about propagation over time; that is
  the half below.
- The *version-never-goes-backward* half is verified by the control plane's
  own test suite: versions climb per scope and never repeat, and a
  transition that walks backward is refused.
- Intended bound: a stale worker, mid-propagation, never enforces something
  looser than it already had. Not yet asserted by a test (needs a live multi-worker race; v1.1). No single test combines both
  halves — the two properties above are each tested on
  their own, and nothing in this repo uses the literal term
  `policy_monotonic`. Treat it as implied by the two proven
  halves, not as a directly-asserted guarantee.

---

## Posture is a capability contract

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Posture
behaviour").

**The bound.** Single process: exact. Every posture decision in the SDK goes
through `runbound.posture.Posture.allows`, which turns a tool's declared
capability classes into one verdict — the worst of its classes, so a tool is
only as permitted as its least permitted class, and a tool that declared no
class at all is admitted only by a posture that allows everything. The check
runs in the `@runbound.tool` wrapper before the body, against the session's
posture, the process's and the plane's tightened together (the strictest
wins; a session's narrowing never loosens the process's); a denied
tool raises `SafeModeViolation` and its body never executes. Class rules from
`init(capabilities=...)` are the same function over the same classes, and the
stricter of the two wins.

Three limits, stated rather than hidden: it covers only tools declared with
`@runbound.tool` (an undecorated function, or a framework tool seen only
through the LangChain handler, is not refused); a bug reading the posture is
fail-open like every other internal error — the call runs and a warning is
logged; and `stopped` refuses every declared tool *and* every model call (see
[Stopped means stopped](#stopped-means-stopped)).

Fleet: the plane's posture reaches a worker on its next heartbeat
(`control_plane_poll_s`, default 5 s) and is dropped 60 s after the last
successful contact unless `stale_halt="hold"`, the halt's own rule. The plane
can only **tighten**: its posture combines with the worker's by
`posture.tighten`, so it can narrow a manual posture and can never widen one,
and lifting its own directive leaves the worker's standing.

**Asserted by:** `tests/test_postures.py` (the whole table, posture × class set
× decorated; `allows` as the only decider, asserted by parsing every module;
`tighten`; manual versus automatic; the ladder's three rungs; class rules;
`require_rules`; fail-open) and `tests/test_session_sync.py` (the plane's
posture within one heartbeat, tighten-only, staleness), and the Acme scorecard's
scenario 11 (`demo/fleet_verify.py`), where a fleet-wide Narrow halt states
`restricted` on every worker.

---

## Stopped means stopped

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Posture
behaviour").

**The bound.** Single process: exact, by construction rather than by
scanning. Every model call, whichever of a wrapped client's sync, async or
streamed `create`, or a `@runbound.llm`-decorated function, funnels through
one door (`Engine.admit`), and that door's very first stage for a model call
reads the effective posture — the session's own narrowing, the process's,
the plane's Controls-stated one and a fleet-wide Narrow halt's, tightened
together, the same function every other posture decision goes through — and
refuses before the circuit is even checked, let alone before the provider is
touched. The other four postures (`full`, `restricted`, `read_only`,
`no_side_effects`) keep serving model calls, exactly as the posture table
says; only `stopped` reaches this stage's refusal. Reading the posture is
fail-open like every other admission phase: a bug here costs a call nothing
but a warning, never a refusal of our own making.

**Asserted by:** `tests/test_stopped_posture.py` — `Engine.admit` refuses a
model call under a manual, a ladder-set and a process-level `stopped`
posture and serves one under every other posture (a table over all five); a
bug reading the posture lets the call through; and, end to end through
`runbound.wrap()` and `@runbound.llm`, a fake provider records **zero**
calls under `stopped` for a sync call, an async call, a streamed call, a
synchronous `@runbound.llm` function and an async one — proving the
provider itself was never reached, not merely that an exception was raised
somewhere.

---

## A complete Decision

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Decision
semantics").

**The bound.** Single process: exact, by construction — and, as of this
release, literally every refusal site, not most of them. Every one states,
in `evaluation`, whichever of `limit`, `used`, `reserved`, `estimate`,
`remaining` apply to what it judged, plus `provider_called: bool` — always,
whether or not any of the other five do. `Engine.refuse`/`_stamp_decision`
fold `provider_called` in once, centrally, for every site that raises
through them (every admission stage, a posture or capability denial, a
tool-policy violation, the action cap); the two api-level entry doors that
predate the `Decision` object (fan-out, in-flight) state it themselves, the
same way. The one number this bound does not promise is exactness of the
estimate itself — a money or tokens figure is still the same estimate
[Upper bound](#upper-bound) and [Budget](#budget) already document — only
that whatever number the stage actually compared is the one written down. A
post-call budget crossing is the one wall trip that also carries a
`Decision`: `provider_called=True`, and its `reason` states in words that
the call already happened and its result is withheld, since usage is only
known after the fact and the money was already spent regardless of what
happens next.

Four sites decide a refusal from a fact relayed from elsewhere — an org
budget the plane itself decided, an unreachable plane under
`on_plane_loss="refuse"`, a fleet-wide halt, and a latch another worker set
— rather than from a number this worker compared itself, so none of them
has a `limit`/`used`/`reserved`/`estimate`/`remaining` to state; inventing
one would be less honest than an `evaluation` that states only
`provider_called` (always `False` — none of these is ever decided after a
provider call) and, on the `Decision` itself, a `level` of `"fleet"` and a
`boundary` naming the kind (`"money"`, `"plane"`, `"halt"`) where the
relayed detector name says which one — see `runbound.api._relayed_decision`.
What such a Decision never lacks is a `reason` in words, and whatever fact
did arrive with the verdict: a halt reaches a worker as a flag and a mode,
so its `evaluation` carries `halt_mode` (`"stop"` or `"narrow"`). It carries
no directive id and no operator's note because the plane does not send
them to workers; who halted the fleet, and why, is the plane's own record.

**Asserted by:** `tests/test_decision_evaluation.py` — a table over every
refusal site (circuit, a stopped posture, the steps/tokens door stages, the
money reservation, a post-call budget crossing, the action cap, posture, a
capability class rule, a tool-policy violation, the in-flight cap, the
fan-out limits, an org budget from the plane, an unreachable plane, a fleet
halt, a relayed latch), each checked against what can apply for its own
boundary, plus a closed-set check that no stray evaluation key ships
unread. Only a post-call wall trip other than the budget crossing (`loop`,
`spike`, `error_storm`, the post-call `steps`/`timeout` walls) carries no
`Decision` at all — `tests/test_refusal_contract.py` covers those, and
`exc.reason` still resolves correctly for them from the anomaly's own
detector. `tests/test_admission_stages.py` and
`tests/test_envelope.py` cover the pure stage functions and the door
integration each site is built from.

---

## The refusal contract

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("The refusal
contract").

**The bound.** `ExecutionRefused` is the public name and the base class for
the one exception runbound raises on purpose; `GuardrailTripped` is the
identical class object under its old name, permanently — not a lookalike
kept in sync by hand, so every handler already written as `except
GuardrailTripped:` keeps working, forever, by construction. No refusal here
ever subclasses `openai.APIError`, the Anthropic equivalent, or any other
provider SDK's exception, so a provider's own retry layer — or an
application's bare `except APIError: retry` — never mistakes a refusal for
its own kind of failure to retry, whatever that client's `max_retries` is
configured to. Every refusal exposes `reason` (one value from a 15-entry
closed set — `budget`, `tokens`, `steps`, `time`, `posture`, `policy`,
`approval`, `circuit`, `concurrency`, `blast_radius`, `halt`, `plane`,
`loop`, `error_storm`, `spike` — resolved from the tool policy's own rule
name, then the Decision's boundary, then the anomaly's detector, and never
outside the set); `retryable` (`False` for every reason a bare wait cannot
fix — budget, tokens, steps, time, posture, policy, halt, blast radius,
approval, loop, error storm, spike — `True`, with a `retry_after` where one
is known, for `circuit` and `concurrency`, and `True` with no known
`retry_after` for `plane`); `provider_called`; and `scope` (which level
decided, and an UNSALTED sha256 hash of the session's key, so one caller matches
across workers; never the key itself).
`runbound.is_retryable(exc)` is the predicate a retry loop should use in
place of guessing from the exception's message or type.

**Asserted by:** `tests/test_refusal_contract.py` — the alias identity
(`GuardrailTripped is ExecutionRefused`), an `except GuardrailTripped:`
still catching every built-in refusal, no refusal subclassing a provider
exception, the reason set matching the closed list exactly, `retryable`
for every named reason, and the property this bound exists for: a retry
loop that checks `runbound.is_retryable` makes exactly one attempt on a
budget refusal, and a loop that ignores the predicate entirely and blindly
retries a refused tool call is still stopped — within `loop_threshold`
attempts, by the loop detector's own latch, not by luck. Against the real
`openai` and `anthropic` SDKs over a mock transport
(`tests/test_real_sdk.py`), a pre-call refusal makes zero HTTP requests
whatever the client's own `max_retries` is set to, and
`isinstance(exc, openai.APIError)` / the Anthropic equivalent are `False`.

## Halt (release / hold)

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Fleet
consistency bounds").

**The bound.** `"release"` (default): the halt stops being enforced 60
seconds after the last successful contact with the plane. `"hold"`: no time
bound at all — it stays enforced until a heartbeat explicitly lifts it, even
on a link that has been dead far longer than 60 seconds.

**The same bound, for a Narrow halt's posture.** A fleet-wide `"stop"`
halt and a fleet-wide `"narrow"` one (`Engine.halt_posture`, posture
`restricted`) obey the identical `stale_halt` rule off the identical clock
(`RemoteState._halt_state`) — a Narrow that has gone stale under `"release"`
stops narrowing (and stops being acked, `RemoteState._hello_payload`'s
`halt_ack`) exactly when a Stop that had gone stale would stop refusing.
Narrow is also gated by `on_halt`, same as Stop: `"raise"` (default) honours
it, `"warn"` never installs the posture at all rather than force a
`SafeModeViolation` into code that asked never to be stopped by the kill
switch (`Engine.halt_posture`, `api._sync_entry`/`_warn_narrowed`) — the two
plane-driven postures (this one and the Controls-stated one) are
independent sources (`runbound.state.POSTURE_SOURCES`'s `"halt"` entry) that
tighten together in `Engine.effective_posture` and must lift on their own.

**Asserted by:** `tests/test_plane_loss.py::test_stale_halt_release_is_the_default`,
`::test_stale_halt_release_drops_the_halt_after_the_window`,
`::test_stale_halt_hold_keeps_the_halt_well_past_the_window`,
`::test_stale_halt_hold_is_lifted_only_by_a_heartbeat_saying_so`,
`::test_an_unrecognized_stale_halt_value_behaves_like_release`,
`::test_halt_stale_s_is_none_with_no_halt`,
`::test_halt_stale_s_counts_seconds_since_last_contact_while_enforced`; also
`tests/test_shared_state.py`'s halt tests (including the narrow/mode
ones), `tests/test_kill_switch.py` (Narrow at the `Engine` level, including
the two-source tighten/lift test and the `on_halt="warn"` gate), and
`tests/test_session_sync.py`'s Narrow block (end to end through
`runbound.session`/`@runbound.tool`). Acme scorecard scenario 6 ("kill
switch") exercises the Stop halt end to end but does not itself flip
`stale_halt`; a separate scenario exercises Narrow and convergence.

---

## Plane-loss (guard_locally / refuse)

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#what-is-guaranteed) ("Fleet
consistency bounds").

**The bound.** `"refuse"` latches nothing and costs no strike — the very
next entry asks the plane again, so a `"refuse"` misconfiguration self-heals
the moment the plane answers. An *answered* refusal (the plane said "no" for
a real reason, e.g. an org daily budget already spent) is always honored
regardless of this setting — `on_plane_loss` only governs an *unanswered*
question.

**Asserted by:** `tests/test_plane_loss.py::test_guard_locally_on_a_degraded_link_answers_none`,
`::test_guard_locally_is_the_default_when_unset`,
`::test_refuse_on_a_degraded_link`, `::test_refuse_on_a_timeout`,
`::test_refuse_on_an_error`,
`::test_refuse_is_never_cached_the_next_entry_asks_again`,
`::test_invalid_key_guards_locally_under_guard_locally`,
`::test_invalid_key_guards_locally_under_refuse_instead_of_refusing_forever`,
`::test_limited_mode_answers_locally_under_guard_locally`,
`::test_limited_mode_answers_locally_under_refuse_too`; also
`tests/test_plane_refusal_entry.py` and `tests/test_edge_case_fixes.py`
(the api honoring `EntryDecision.allow` at the door — the fix that made
an *answered* plane refusal actually stop the call instead of being
silently overruled).

---

## Abandoned stream accounting

**Guarantee:** stated for readers in [Guarantees and
limitations](docs/reference/guarantees.md#guarantees-and-limitations)
("An abandoned stream is recorded, but as an estimate, and only when
garbage collected").

**The bound.** Reported exactly once per stream either way (normal end, or
abandonment) — never both, never zero unless nothing was ever collected at
all (a live reference held forever, same as any Python object), or the
process exits while the stream is still alive: the finalizer is registered
with `atexit=False` on purpose, so a process that exits with the stream still
referenced never reports it rather than calling out during interpreter
teardown, when threads, logging handlers and the network may already be gone.

**Asserted by:** `tests/test_streams_abandoned.py` (25 tests: sync and async
abandonment mid-stream after `gc.collect()`, usage-present vs.
chars-estimated tokens, in-flight slot release (`release_calls`), circuit neutrality (no
success or failure reported for an abandoned stream), no double report); `tests/test_streaming.py::test_an_abandoned_stream_is_recorded_as_one_partial_call`,
`::test_abandoned_anthropic_stream_is_recorded_as_one_partial_call`,
`::test_abandoned_async_stream_is_recorded_as_one_partial_call`; also
`tests/test_tool_requests.py`, `tests/test_unpriced.py` (abandonment
interacting with unpriced-model accounting).

---

## Unpriced model

**One sentence.** A model with no known price — not in the static table, not
in `custom_prices` — does exactly what `on_unpriced_model` says and nothing
else: counted as `$0.00` with a once-per-model warning (`"zero"`, default),
priced from a stated fallback pair and marked estimated (`"estimate"`), or
refused at the door before the request goes out regardless of `on_anomaly`
(`"refuse"`).

**The bound.** `"refuse"` is unconditional *at the door* and ignores
`on_anomaly` on purpose — the customer stated it, so it is not negotiable per
anomaly — and every refusal reaches the observers (`tests/test_unpriced.py::test_before_records_every_refusal`). It is
bounded by what the door can know: a `record_call()` report, and a model
name only known after the response comes back, cannot be stopped after the
fact — both are recorded instead, priced like `"estimate"` when a fallback
pair was given, else like `"zero"`, under a once-per-model warning saying the
call was recorded rather than refused (`runbound/pricing.py::_warn_refuse_recorded`).

**Asserted by:** `tests/test_unpriced.py` (48 tests covering all three
modes, the once-per-model warning/alert cap, and the `"refuse"` door check
before the request goes out); also `tests/test_edge_case_fixes.py`,
`tests/test_coverage_warnings.py`, `tests/test_wrappers.py`.

---

## Salted hashes and correlation

One line, stated plainly rather than folded into a table: **`args_hash` is
salted per process by design, foreclosing fleet-wide loop correlation by
hash** — the same tool call hashes differently on two different workers (and
differently again after a restart), so a leaked ledger, or a plane operator
looking at raw hashes across services, cannot line up "who called what with
which arguments" by matching digests across the fleet. It is an equality
token for spotting a repeat *inside one process*, never a fingerprint good
for anything outside it. Asserted by `tests/test_hash_salt.py` (8 tests: the
salt is per-process, survives `reset()`, and two processes hash the same
call differently).

---

## Detection vs. delivery

**One sentence.** The SDK detects, stops, refuses and reports. The plane
routes and delivers.

**Why this is a division of labour, not a gate.** An earlier version of this
invariant read "detection is never gated; delivery is," implemented as a
truthiness check inside `runbound/`: the alerter classes stayed in the SDK
and were built, or not, depending on whether a token was set. A review of
that design found it could not be made to hold — `control_plane_url=""`
passed validation while the gate and `shared.build` asked different
questions about the same field, and any non-empty token turned delivery on
for the life of the process, revoked or invented, because `Engine.alerters`
is fixed at construction. A paid feature cannot be enforced by a conditional
running inside the customer's own process. So the delivery code left
rather than grow a second gate: `SlackAlerter`, `PagerDutyAlerter`,
`WebhookAlerter` and everything that existed only to send are gone from
`runbound/alerts.py`, and `Engine._alert` (`runbound/engine.py`) now only
notifies observers. It used to also run `for alerter in self.alerters:
alerter.send(...)`; that loop is deleted, not merely unreached, so there is
nothing left for a fork to un-gate.

**The bound.** Every detector, every reaction, the latch, refusals, tool
policy, the circuit breaker and the in-flight cap work with no token, no
network and no account: free and local. `on_anomaly` decides how the anomaly reaches
the customer's *own* process, and the three settings are alternatives, never
a set: `"raise"` hands the caller a `GuardrailTripped`, `"callback"` hands the
anomaly to the customer's own function, and only `"warn"` logs it
(`runbound/engine.py::_react` — `raise` and `callback` both return before
the `_LOG.warning` line; they do not also log). None of the three, before or
after that change, ever posted anywhere — an anomaly is never "always logged."
What a token (or a self-hosted `control_plane_url`) buys is a *connection*,
not delivery by itself: `GuardrailConfig.plane_mode` (`runbound/config.py`
— `_normalize_connection`, `_validate_plane`) says which of **off**, hosted
(`token` alone, once the hosted plane exists — until then a bare token warns
once and stays **off**) or **self-hosted** (`control_plane_url`, `token=""` for no
auth) a configuration is. Once connected, exits and trips reach the plane as
telemetry; whether any of that becomes a Slack message, a page or a webhook
POST is an alert route the customer configures on the plane, gated by plan
and enforced there with a `403` (verified by the control plane's own test
suite) — the enforcement the SDK-resident version of this feature could
never have provided. A missing, expired or invalid token never changes what
the SDK detects, stops or refuses.

**Asserted by:** `tests/test_token_and_delivery.py` —
`test_detection_latch_policy_and_circuit_are_identical_with_and_without_a_token`
drives the same synthetic run with and without a token and asserts identical
detection, latch, policy and circuit outcomes; the parametrized
`test_on_anomaly_reactions_are_identical_with_and_without_a_token`
(`"warn"` / `"raise"` / `"callback"`) asserts the actual observable
difference each reaction makes — raised, received by the callback, or
logged — identically with and without a token, and calls `validate()`
explicitly on both arms. `tests/test_plane_modes.py` is the regression net
for the blocker that motivated this rewrite: every mode from the call and
from the environment, a blank `control_plane_url` from either source
settling to `"off"`, `""` never producing a plane client, and `plane_mode`
agreeing with `shared.build` over the full `{None, "", "x"} x {None, "",
"url"}` table. `tests/test_config_plane.py::test_repr_hides_the_token` and
`test_repr_hides_the_api_key` assert the secret stays out of `repr()`.
Signature verification and thread draining — what `runbound/alerts.py` is
left with — are asserted by `tests/test_alert_lifecycle.py`.

---

## Repository boundary

**One sentence.** The public mirror of this project contains only the SDK —
nothing from the control plane, the dashboard, the demo fleet, the marketing
site, or this project's own internal planning documents ever reaches it.

**The bound.** The mirror is this SDK's own directory, published by one
release commit per version, built by `scripts/publish_sdk.sh` from exactly the
committed content of that directory — anything outside it is excluded by
construction, not by care taken while writing a commit. Before publishing, the
release script lints
every file in the directory and refuses to publish if any of them still
contains a reference to the private half of this project: the control
plane's source or its Python package name, the marketing site, this
project's internal planning and business documents, or the product's
retired internal name — or is a symlink, is over 1 MB, or carries a
provider API key. A public mirror that fails that lint is never published.

**Asserted by:** the lint's own tests, which test release tooling that lives
in the development monorepo and so are not part of this repository: each
lint token, a planted symlink, a file over 1 MB and a planted provider key
fails the lint, and a clean tree passes.
