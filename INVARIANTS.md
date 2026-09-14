# runbound — invariants

What this document is: the promises that must hold for "runtime controls for
autonomous AI agents" to be true, stated one sentence at a time, with the
bound each promise actually carries and the test(s) or Acme scorecard
scenario that assert it today. Where nothing asserts a promise yet, this
file says so — "not yet asserted" is a finding, not something to quietly
drop.

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
  is the README's [What is exact and what is
  estimated](README.md#what-is-exact-and-what-is-estimated) table.

---

## Budget

**One sentence.** A session never spends more than its stated limit without
that being a stated, bounded amount of slack — never an unbounded one.

**What is a turn.** A **turn** is one `runbound.session()` block on one
worker, from entry to exit, whatever model and tool calls it contains. A
worker learns the fleet's spend for a key only at block entry, and reuses
that answer for `control_plane_cache_s` (default 5 s); other workers' spend
reaches the plane through exit deltas posted in batches (about one second).
So a worker can be stale for up to the cache window plus batch latency.

**The bound.** Single process: exact — `total_cost_usd > budget_usd` is a
plain comparison against the running total this process holds, strictly
greater than, so exactly at the limit does not trip; the detector trips on
the very event that crosses the limit. That means the wall **stops the
session after the call that crossed it**, not before: a wrapped model call's
usage exists only once it has returned, so the call that went over is paid
for and the next one is the one prevented. `budget_admission=True` (opt-in,
off by default) is the other column — a pre-call *estimate* that refuses a
call before it goes out, never latches, and is bounded by the estimate's
accuracy rather than by arithmetic. The dollars on both sides of that line
come from a static price table, so the comparison is exact and the total it
compares is an estimate of the bill (see the README table named above).
Fleet: for one key, worst-case overspend over `budget_usd` is the sum over
workers of (the spend of that worker's one in-flight block + the spend of
any blocks it admitted during its staleness window).

**How a customer tightens it.** Keep blocks short (one end-user request per
block), and lower `control_plane_cache_s` at the cost of more entry requests.
The Acme scorecard asserts the two-worker, one-request-per-block case lands
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
- **Not yet asserted:** a test that names the quantified worst-case bound
  itself (`workers × one turn + cache window`) as a single guarantee — the
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

## Latch

**One sentence.** Once a session is latched, no provider call starts after
the latch is observed locally — the door refuses the block before its body
runs, not after.

**The bound.** Single process: immediate — the very next `session()` entry
(under `on_anomaly="raise"`) or the very next guarded call (otherwise) sees
the latch, because it lives in the same process's memory. Fleet: a latch set
on one worker reaches another within `control_plane_cache_s` **plus one
in-flight turn** — the same window the budget bound uses, because both ride
the same cached entry answer.

**Asserted by:**
- Local, single-process latch-before-the-call: `tests/test_latch.py::test_entering_a_latched_session_raises_before_the_body_runs`,
  `::test_a_blocked_user_is_blocked_on_every_later_message`,
  `::test_a_latched_session_skips_detection_entirely`.
- Fleet propagation, refused before the model call runs:
  `tests/test_session_sync.py::test_a_remote_latch_refuses_the_block_before_it_runs`.
- Acme scorecard scenario 2 ("abuser blocked on the other worker") — a
  cross-process demonstration with a real latency measurement proving no
  model call happened.
- **Not yet asserted:** the exact phrase-level guarantee as a concurrency
  property — e.g. a test that starts a call already in flight when a latch
  lands mid-call and shows that call is unaffected while the *next* one is
  refused. What exists proves "the door checks before the body runs," not a
  race between an in-flight call and a landing latch.

---

## Circuit

**One sentence.** A provider whose calls keep failing gets its circuit
opened after a stated number of failures in a stated window, and stays open
for a stated cooldown before exactly one probe is let through — per
provider endpoint, never per session.

**The bound.** `circuit_failure_threshold` failures inside
`circuit_window_seconds` opens it; it stays open for
`circuit_cooldown_seconds`; the half-open state lets exactly one call
through, and a fleet circuit does the same thing for every worker at once
under one shared decision. That decision is shared, not instantaneous: a
worker that did not see the failures itself adopts the plane's circuit on its
next heartbeat, not the moment another worker opens it — around 1.5-2 s in
the Acme scorecard's scenario 4, but that is an observed number from a
polling loop, not a numeric bound the scenario asserts.

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

---

## Policy-monotonic

**One sentence.** Fleet propagation can only make an enforced policy
stricter; a stale worker never weakens a restriction it already knows.

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
- **Not yet asserted** as one named invariant: no single test combines both
  halves into "a stale worker, mid-propagation, never enforces something
  looser than it already had" — the two properties above are each tested on
  their own, and nothing in this repo uses the literal term
  `policy_monotonic`. Treat the sentence above as implied by the two proven
  halves, not as a directly-asserted guarantee.

---

## Halt (release / hold)

**One sentence.** What an *enforced* fleet halt does when the plane link
itself goes stale is the customer's stated choice — lift it after a bounded
window (`"release"`), or hold it until a heartbeat explicitly says
otherwise (`"hold"`) — never an undocumented default either way.

**The bound.** `"release"` (default): the halt stops being enforced 60
seconds after the last successful contact with the plane. `"hold"`: no time
bound at all — it stays enforced until a heartbeat explicitly lifts it, even
on a link that has been dead far longer than 60 seconds.

**Asserted by:** `tests/test_plane_loss.py::test_stale_halt_release_is_the_default`,
`::test_stale_halt_release_drops_the_halt_after_the_window`,
`::test_stale_halt_hold_keeps_the_halt_well_past_the_window`,
`::test_stale_halt_hold_is_lifted_only_by_a_heartbeat_saying_so`,
`::test_an_unrecognized_stale_halt_value_behaves_like_release`,
`::test_halt_stale_s_is_none_with_no_halt`,
`::test_halt_stale_s_counts_seconds_since_last_contact_while_enforced`; also
`tests/test_shared_state.py`. Acme scorecard scenario 6 ("kill switch")
exercises the halt end to end but does not itself flip `stale_halt`.

---

## Plane-loss (guard_locally / refuse)

**One sentence.** When a session entry cannot be answered at all — timeout,
error, or a degraded link with no fresh cached decision — the SDK either
falls back to local detection (`"guard_locally"`) or refuses the entry
itself (`"refuse"`), by the customer's stated choice; an invalid token is
always a configuration error, guarded locally under both settings, never
treated as plane loss.

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
`tests/test_plane_refusal_entry.py` and `tests/test_review_fixes_t59.py`
(the api honoring `EntryDecision.allow` at the door — the Wave 24 fix that
made an *answered* plane refusal actually stop the call instead of being
silently overruled).

---

## Abandoned stream accounting

**One sentence.** A guarded stream that is never exhausted or closed is
still accounted for — as one partial call, recorded when it is garbage
collected (never at interpreter exit), in the session that opened it, with
the time actually streamed and output tokens from usage if any chunk carried
it else an estimate from the streamed text (input tokens only from usage, or
from the request under `estimate_tokens=True` — so `0` for an OpenAI stream,
whose usage rides the final chunk) — counted as neither a circuit
success nor failure, with its in-flight slot freed.

**The bound.** Reported exactly once per stream either way (normal end, or
abandonment) — never both, never zero unless nothing was ever collected at
all (a live reference held forever, same as any Python object), or the
process exits while the stream is still alive: the finalizer is registered
with `atexit=False` on purpose, so a process that exits with the stream still
referenced never reports it rather than calling out during interpreter
teardown, when threads, logging handlers and the network may already be gone.

**Asserted by:** `tests/test_streams_abandoned.py` (25 tests: sync and async
abandonment mid-stream after `gc.collect()`, usage-present vs.
chars-estimated tokens, in-flight slot release, circuit neutrality, no
double report); `tests/test_streaming.py::test_an_abandoned_stream_is_recorded_as_one_partial_call`,
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
anomaly — and alerts once per model per Engine, never once per call. It is
bounded by what the door can know: a `record_call()` report, and a model
name only known after the response comes back, cannot be stopped after the
fact — both are recorded instead, priced like `"estimate"` when a fallback
pair was given, else like `"zero"`, under a once-per-model warning saying the
call was recorded rather than refused (`runbound/pricing.py::_warn_refuse_recorded`).

**Asserted by:** `tests/test_unpriced.py` (48 tests covering all three
modes, the once-per-model warning/alert cap, and the `"refuse"` door check
before the request goes out); also `tests/test_review_fixes_t59.py`,
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
(Wave 31) rather than grow a second gate: `SlackAlerter`, `PagerDutyAlerter`,
`WebhookAlerter` and everything that existed only to send are gone from
`runbound/alerts.py`, and `Engine._alert` (`runbound/engine.py`) now only
notifies observers. Before Wave 31 it also ran `for alerter in self.alerters:
alerter.send(...)`; that loop is deleted, not merely unreached, so there is
nothing left for a fork to un-gate.

**The bound.** Every detector, every reaction, the latch, refusals, tool
policy, the circuit breaker and the in-flight cap work with no token, no
network and no account, forever. `on_anomaly` decides how the anomaly reaches
the customer's *own* process, and the three settings are alternatives, never
a set: `"raise"` hands the caller a `GuardrailTripped`, `"callback"` hands the
anomaly to the customer's own function, and only `"warn"` logs it
(`runbound/engine.py::_react` — `raise` and `callback` both return before
the `_LOG.warning` line; they do not also log). None of the three, before or
after Wave 31, ever posted anywhere — an anomaly is never "always logged."
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

**The bound.** The mirror is this SDK's own directory, published by a
`git subtree split` that carries exactly the committed content of that
directory — anything outside it is excluded by construction, not by care
taken while writing a commit. Before the split, the release script lints
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
