"""User-facing configuration and its validation.

Configuration is validated once, loudly, at ``runbound.init()`` time — a
misconfigured SDK should fail at startup, never mid-run inside a hot path.
"""

import logging
import os
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

from .circuit import CIRCUIT_MODES
from .events import Anomaly
from .policy import ToolPolicy, coerce
from .posture import validate_class_rules, validate_overrides
from .shared import HOSTED_PLANE_URL

_LOG = logging.getLogger("runbound")

#: Has this process already been told, once, that its token has nowhere to
#: connect to? A customer with a bare token and no hosted plane yet may build
#: many configs (every test, every ``init()``), and the warning is only
#: worth saying the first time — see :func:`_warn_token_has_no_plane`.
_TOKEN_NO_PLANE_WARNED = False

#: Has this process already been told, once, that an environment-supplied
#: plane url has no token? Same reasoning as ``_TOKEN_NO_PLANE_WARNED``: many
#: configs can be built in one process (every test, every ``init()``), and a
#: deployment fact that hasn't changed doesn't deserve a new line every time.
_ENV_PLANE_URL_NO_TOKEN_WARNED = False


def _warn_token_has_no_plane() -> None:
    """Once per process: a token is set but there is nowhere to send it yet.

    ``HOSTED_PLANE_URL`` is ``None`` until the hosted plane exists (see
    :mod:`runbound.shared`), so a bare ``token`` with no
    ``control_plane_url`` and no ``RUNBOUND_PLANE_URL`` cannot connect
    anywhere today. That costs nothing detection cares about — stopping and
    refusing are unaffected, and this is the only consequence — so it is a
    WARNING, not a rejection, and it is said once per process rather than
    once per ``init()``.
    """
    global _TOKEN_NO_PLANE_WARNED
    if _TOKEN_NO_PLANE_WARNED:
        return
    _TOKEN_NO_PLANE_WARNED = True
    _LOG.warning(
        "runbound: token is set but there is nowhere to send it yet "
        "(no hosted plane); set control_plane_url for a self-hosted plane. "
        "Detection, stopping and refusals are unaffected."
    )


def _warn_env_plane_url_no_token() -> None:
    """Once per process: ``RUNBOUND_PLANE_URL`` named a plane but no token
    came with it, so this process guards locally instead of connecting.

    This is not the same mistake as passing ``control_plane_url`` to
    ``init()`` with no token — that call site is a specific person writing a
    specific line, and a url with no token there is almost always a typo, so
    it still raises. An environment variable is different: it is a
    deployment fact, often set once in a base image or a shared manifest,
    long before every service that inherits it has been issued a token. If a
    bare ``RUNBOUND_PLANE_URL`` could fail ``init()``, one environment
    variable could take down every service that hasn't caught up yet — in a
    product whose whole thesis is failing open everywhere except the policy
    gates themselves. So this logs once and falls back to local guarding
    rather than raising; the value of the url is never in the message.
    """
    global _ENV_PLANE_URL_NO_TOKEN_WARNED
    if _ENV_PLANE_URL_NO_TOKEN_WARNED:
        return
    _ENV_PLANE_URL_NO_TOKEN_WARNED = True
    _LOG.warning(
        "runbound: RUNBOUND_PLANE_URL is set in the environment but no "
        "token was found (RUNBOUND_TOKEN, or token= to init()); guarding "
        "locally instead of connecting. Detection, stopping and refusals "
        "are unaffected."
    )


ON_ANOMALY_MODES = ("warn", "raise", "callback")
ON_LOOP_POLICIES = (None, "graded", "break", "throttle", "escalate")

#: The graded loop policy's three rungs, as multiples of ``loop_threshold``.
#: 3 is worth a line, 6 is worth a person, 9 is a runaway: a call repeated
#: ``loop_threshold`` times (3) is logged, ``LOOP_ALERT_MULTIPLE`` times (6)
#: pages someone, ``LOOP_CONTAIN_MULTIPLE`` times (9) is contained by the spike
#: ladder. The alert multiple is the SDK's own precedent for "a second, harder
#: bar" (``loop_hard_threshold`` defaults to twice ``loop_threshold``). Both
#: scale with the customer's own ``loop_threshold``; ``loop_alert_threshold``
#: and ``loop_contain_threshold`` set them outright.
LOOP_ALERT_MULTIPLE = 2
LOOP_CONTAIN_MULTIPLE = 3

#: The four loop shapes ``LoopDetector`` can catch. "repeat": the same
#: hash over and over (period 1) — the original, unchanged behavior.
#: "sequence": a period-k cycle of distinct hashes (2 <= k <= loop_max_period)
#: repeating loop_threshold times. "retry": one tool failing and being
#: re-attempted loop_threshold times, distinct from error_storm (which counts
#: every failure, any tool, any kind, in a trailing time window). "stall":
#: loop_stall_turns consecutive turns that add no hash the session has not
#: already seen. "stall" is deliberately not in the default tuple — it is
#: opt-in, since a quiet agent is not always a stuck one.
LOOP_SHAPES = ("repeat", "sequence", "retry", "stall")

#: What a model with no known price does. "zero": counted as $0.00 (a
#: once-per-model warning, on by default, says so). "estimate": priced from
#: ``unpriced_price_per_1m_usd`` instead, marked ``priced="estimated"``.
#: "refuse": refused at the door before the request goes out, whatever
#: ``on_anomaly`` says.
ON_UNPRICED_MODEL_MODES = ("zero", "estimate", "refuse")

#: What a halt does while the control-plane link itself is degraded.
#: "release": today's behavior — a halt lifts after ``60s`` regardless.
#: "hold": the halt stays enforced until the plane says otherwise.
STALE_HALT_MODES = ("release", "hold")

#: What a session entry does when the plane could not answer at all (timeout,
#: error, degraded, no cached decision). "guard_locally": today's behavior —
#: local detection alone. "refuse": the entry itself is refused.
ON_PLANE_LOSS_MODES = ("guard_locally", "refuse")

#: What an open provider circuit does. "notify": alert and log, never block —
#: the default, because refusing calls is the customer's choice to make.
#: "open": raise CircuitOpen before each call until the provider recovers.
ON_PROVIDER_FAILURE_MODES = ("notify", "open")

#: What a confirmed spike does. "notify": log and alert only. "trip": stop the
#: session. "limit": climb the abuse ladder — notice, session limit, rollover.
ON_SPIKE_MODES = ("notify", "trip", "limit")

#: budget_window's three named calendar buckets, each a UTC boundary. Any
#: other string is rejected at validate(); a float/int is a rolling window
#: in seconds instead, not a name at all.
BUDGET_WINDOW_NAMES = ("hour", "day", "month")

# knobs that are either None (disabled) or strictly positive
_POSITIVE_LIMITS = (
    "budget_usd",
    "max_total_tokens",
    "run_budget_usd",
    "run_max_total_tokens",
    "max_steps",
    "max_events",
    "tokens_per_minute_limit",
    "max_call_seconds",
    "max_tokens_out_per_call",
    "latch_ttl_seconds",
    "error_storm_limit",
    "max_session_seconds",
    "max_session_lifetime_seconds",
    "max_cost_per_call_usd",
    "max_active_sessions",
    "max_session_depth",
    "max_child_sessions",
    "max_inflight_calls",
    "coverage_check_seconds",
    "max_actions_per_run",
)

#: Circuit-breaker knobs: always on, always positive, never None.
_CIRCUIT_KNOBS = (
    "circuit_failure_threshold",
    "circuit_window_seconds",
    "circuit_cooldown_seconds",
)

#: How ``circuit_mode`` decides when closed becomes open. "count" (the
#: default) is every pre-existing behavior, unchanged. "rate" is
#: resilience4j's sliding-window model: below ``circuit_min_calls`` nothing
#: is ever judged; at or above it, either the failure rate crossing
#: ``circuit_failure_rate`` or the slow-call rate crossing
#: ``circuit_slow_rate`` opens it — a slow call counts even with zero errors.
CIRCUIT_MODE_VALUES = CIRCUIT_MODES

#: Rate mode's own knobs, always validated (not only when the mode is
#: "rate"): a customer who flips ``circuit_mode`` later must never discover a
#: bad value that was sitting there unchecked the whole time.
_CIRCUIT_RATE_FRACTIONS = ("circuit_failure_rate", "circuit_slow_rate")

#: Trailing llm calls the spike detector confirms a spike over.
SPIKE_CONFIRM_MAX = 5

#: Absolute rises a spike must clear on top of the ratio; always positive.
_SPIKE_FLOORS = ("spike_min_duration_s", "spike_min_output_tokens")

#: What an org-wide halt does to a worker, whichever mode the plane set it
#: to: "raise" -- a "stop" halt refuses every guarded session at the
#: door, which is what a kill switch is for, and a "narrow" one states
#: posture "restricted" fleet-wide (Engine.halt_posture), refusing every
#: non-read decorated tool while model calls keep serving. "warn" -- a
#: "stop" halt says so and keeps serving, unchanged since before this task;
#: a "narrow" one is not applied *at all* (no posture, no refusal) and says
#: so instead -- the same "code that asked never to be stopped is never
#: stopped" rule, applied at the tool door instead of the session door.
ON_HALT_MODES = ("raise", "warn")

#: Longest a call to the control plane may block a session's entry. A plane is
#: an optimization on top of local detection, never a dependency of it, so the
#: budget is a fraction of a second and this is its ceiling.
CONTROL_PLANE_TIMEOUT_MAX = 2.0


def _is_price_pair(value: object) -> bool:
    """Is ``value`` a ``(usd_per_1M_input, usd_per_1M_output)`` pair?

    Both non-negative numbers, and explicitly not ``bool`` — ``True``/``False``
    are ``int`` in Python and would otherwise silently pass as prices 1 and 0.
    """
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in value)
    )


@dataclass
class GuardrailConfig:
    """Thresholds and reactions for one guarded agent.

    Every limit defaults to ``None``, which disables the detector that reads
    it, so an unconfigured session observes and never trips.
    """

    budget_usd: float | None = None
    max_total_tokens: int | None = None
    # A budget accumulates over the key's whole lifetime (every run this key
    # has ever made) unless budget_window says otherwise: "hour" | "day" |
    # "month" is a calendar UTC boundary; a float or int is a rolling window
    # in seconds; None (the default, unchanged) is the pre-existing,
    # unbounded-lifetime behavior. Governs every cumulative counter on the
    # key -- total spend, total tokens, the estimated-cost slice -- which all
    # reset together the moment the window rolls over. In memory only: a
    # restart starts a fresh window, documented in docs/guides/runs.md.
    budget_window: str | float | None = None
    # Per-run caps: reset on every session() block entry, exactly as
    # max_session_seconds's own run clock already is (run_started_at). A
    # run's own budget is independent of the key's -- a fresh run_budget_usd
    # every entry, on top of whatever the key's own (possibly windowed)
    # budget_usd still has room for; the tighter of the two is what an
    # estimate is actually checked against, and Decision.level says which one
    # bound the call ("run" or "key").
    run_budget_usd: float | None = None
    run_max_total_tokens: int | None = None
    # An agent step is a model turn: max_steps is measured against
    # SessionState.turns, which counts llm_call events only. max_events
    # counts every recorded event (what max_steps used to mean) and is
    # measured against SessionState.event_count.
    max_steps: int | None = None
    max_events: int | None = None
    tokens_per_minute_limit: int | None = None
    loop_threshold: int = 3  # k identical action-hashes => loop
    loop_window: int = 20  # sliding window size (events)
    # None means "graded" (log, then alert, then contain: see LOOP_ALERT_MULTIPLE).
    # "break" | "throttle" | "escalate" keep their exact behaviour.
    on_loop: str | None = None
    loop_hard_threshold: int | None = None  # escalate's hard stop; default 2 * loop_threshold
    # The graded policy's second and third rungs. None: LOOP_ALERT_MULTIPLE and
    # LOOP_CONTAIN_MULTIPLE times loop_threshold (never past loop_window).
    loop_alert_threshold: int | None = None
    loop_contain_threshold: int | None = None
    # The longest period LoopDetector's "sequence" shape will look for —
    # a period-k cycle (2 <= k <= loop_max_period) repeated loop_threshold
    # times. Bounds the per-call cost: checking every period from 2 up to
    # this ceiling is what keeps "sequence" detection O(window) rather than
    # O(window^2) against an unbounded period.
    loop_max_period: int = 6
    # Consecutive turns with no new hash before the "stall" shape fires.
    # Only read when "stall" is in loop_shapes (it is opt-in; see LOOP_SHAPES).
    loop_stall_turns: int = 5
    # Which of LOOP_SHAPES LoopDetector actually checks, in this order —
    # the first shape in the tuple that matches wins. "stall" is left out of
    # the default on purpose (see LOOP_SHAPES); an unknown name here is a
    # ValueError at validate(), not a silently-skipped shape.
    loop_shapes: tuple[str, ...] = ("repeat", "sequence", "retry")
    throttle_base_seconds: float = 2.0
    throttle_max_seconds: float = 30.0
    spike_detection: bool = True
    on_spike: str = "notify"  # "notify" | "trip" | "limit"  # per-session behavior-change watch, on by default
    # The abuse ladder, read only under on_spike="limit": how many abnormal
    # calls a limited session may still make before it is closed and rolled
    # over, how long the cooldown on that closure lasts, and how many
    # rollovers a key gets before it is blocked outright.
    spike_limit_calls: int = 5
    spike_cooldown_seconds: float = 300.0
    spike_max_strikes: int = 3
    spike_warmup_calls: int = 4  # llm calls of history before a baseline is trusted
    spike_window: int = 50  # llm calls kept per session for the baseline
    spike_factor: float = 10.0  # x median duration or output work => abnormal
    spike_confirm: int = 2  # abnormal calls out of the trailing 5 => confirmed
    spike_min_duration_s: float = 2.0  # a spike must also be this many seconds slower
    spike_min_output_tokens: int = 500  # ...or this many output tokens larger
    max_call_seconds: float | None = None  # hard per-call duration cap
    max_tokens_out_per_call: int | None = None  # hard per-call output cap
    max_cost_per_call_usd: float | None = None  # hard per-call dollar cap
    # How long one *run* may go on, whatever it is doing: a run going for
    # hours is an incident even when every individual call looks fine. None
    # (the default) means no wall clock at all. Measured from
    # SessionState.run_started_at, which the api resets on every entry of a
    # keyed session() block — this is the run's clock, not the key's own
    # clock, so a returning caller's tenth request next week does not
    # inherit the age of their first. The unkeyed default session has no
    # entry to reset on, so for it this is simply the process's own age.
    max_session_seconds: float | None = None
    # The old, identity-scoped meaning of the above, for a customer who wants
    # it: how long a session may exist at all, from its first creation,
    # regardless of how many times it is re-entered. None (the default) means
    # off. Measured from SessionState.started_at, which is never reset. Fires
    # as a second "timeout" anomaly with details["scope"] == "lifetime".
    max_session_lifetime_seconds: float | None = None
    # The shape of a fan-out. A cascade of agents opening sub-sessions costs
    # money in proportion to numbers nobody looked at, so each of these is a
    # number the customer states: sessions open at once across the process,
    # how deep session blocks may nest, and how many distinct children one
    # session may open. All None (off) by default; when set they are enforced
    # at the door, like the per-call caps and whatever on_anomaly says.
    max_active_sessions: int | None = None
    max_session_depth: int | None = None
    max_child_sessions: int | None = None
    # Retry storms and the provider circuit. Failed calls in the trailing
    # minute past error_storm_limit are a storm (None disables it); a provider
    # that fails circuit_failure_threshold times inside circuit_window_seconds
    # opens its circuit for circuit_cooldown_seconds. What an open circuit then
    # does is the customer's explicit choice: "notify" (the default) alerts and
    # lets every call through, "open" raises CircuitOpen instead of calling.
    error_storm_limit: int | None = 10
    on_provider_failure: str = "notify"  # "notify" | "open"
    circuit_failure_threshold: int = 5
    circuit_window_seconds: float = 60.0
    circuit_cooldown_seconds: float = 30.0
    # Opt-in: let the circuit read the rate-limit headers the provider
    # already sent, instead of only counting failures. It reads three things
    # and nothing else — the remaining count across every bucket the provider
    # publishes (Anthropic's four `anthropic-ratelimit-*`, OpenAI's requests
    # and tokens), when they reset, and a 429's `Retry-After`. A bucket at
    # zero opens the circuit until its reset, before the failure threshold is
    # anywhere near; a 429's `Retry-After` sets that opening's cooldown
    # instead of circuit_cooldown_seconds. No header may hold a circuit open
    # longer than quota.MAX_COOLDOWN_S (one hour), whatever it says.
    #
    # The reach is narrower than it sounds, and the limit is in both SDKs, not
    # in runbound: **a plain successful call carries no headers at all**. Both
    # providers hand back a parsed model with nothing to read, and runbound
    # will not change how your call is made to get at them. So a pre-emptive
    # opening happens only from an error response (which always carries
    # headers) or from a call your own code already made through
    # `with_raw_response` / `.parse()`.
    #
    # Off by default because a header your gateway or proxy rewrites would
    # otherwise stop your traffic by surprise; an unreadable header always
    # says nothing rather than guessing. With it off the circuit behaves
    # exactly as it did before this option existed.
    circuit_reads_quota: bool = False
    # "count" (the default, and every behavior this circuit had before
    # this existed) counts failures inside circuit_window_seconds, unchanged.
    # "rate" is resilience4j's model: once at least circuit_min_calls calls
    # (success, slow success, or failure) have landed inside
    # circuit_window_seconds, either the fraction that failed crossing
    # circuit_failure_rate or the fraction slower than
    # circuit_slow_call_seconds crossing circuit_slow_rate opens the circuit
    # -- a provider answering everything, just slowly, can open it with zero
    # errors, which failure-counting alone never could. circuit_min_calls
    # keeps a lightly used provider from being judged on two data points.
    # circuit_slow_call_seconds is None (off) by default: nothing is "slow"
    # until you say how slow. circuit_half_open_calls (default 1, the prior
    # single-probe behavior) is how many calls a half-open breaker admits at
    # once, in either mode, before the (N+1)th is refused.
    circuit_mode: str = "count"  # "count" | "rate"
    circuit_failure_rate: float = 0.5  # (0, 1], rate mode only
    circuit_min_calls: int = 5
    circuit_slow_call_seconds: float | None = None
    circuit_slow_rate: float = 0.5  # (0, 1], rate mode only
    circuit_half_open_calls: int = 1
    # Does this worker take part in the fleet-wide circuit fold — its
    # own transitions reported upward, a fleet instruction forced onto its
    # local breaker? True whenever there is a plane to fold with (a named
    # default change this SDK explicitly allows); irrelevant, and
    # never read, with no plane at all.
    circuit_fleet: bool = True
    # Does a half-open circuit narrow the whole process to "restricted"
    # (source "circuit" in runbound.state.POSTURE_SOURCES) while it waits to
    # find out if the provider recovered? Off by default -- an operator opts
    # in because a circuit and a posture are two different signals until they
    # say otherwise.
    circuit_posture: bool = False
    max_sessions: int = 10_000  # keyed-session registry capacity (LRU)
    # Self-hosted endpoints. On your own GPUs the scarce resource is
    # concurrency, not dollars: max_inflight_calls is how many calls to one
    # provider label ("openai@gpu-box:8000") may be in flight at once,
    # process-wide, before the next one is refused before it goes out. None
    # (the default) counts nothing and refuses nothing. estimate_tokens is the
    # opt-in fallback for servers that report no usage at all: chars/4 over the
    # request and the answer, used only when the endpoint said nothing.
    max_inflight_calls: int | None = None
    estimate_tokens: bool = False
    # Instrumentation coverage. auto_wrap patches the provider SDKs themselves
    # at init() time, so a client built anywhere — inside a framework, in code
    # nobody remembered to wrap — is guarded without a wrap() call. Set it False
    # to instrument by hand only. coverage_check_seconds is how long after
    # init() runbound waits before saying, once, at WARNING, that a provider
    # SDK is imported and yet no guarded call has ever been seen; None disables
    # that check.
    auto_wrap: bool = True
    coverage_check_seconds: float | None = 60.0
    # What a critical trip does to the session afterwards. "latch": the session
    # stays stopped — every later call is refused until clear() (or the ttl).
    # "once": stop that one call only; the next call is evaluated afresh.
    on_trip: str = "latch"  # "latch" | "once"
    # None: a tripped session stays tripped until clear(). A number of seconds:
    # the latch expires that long after it was set, re-admitting the session
    # (every detector rearmed) without a restart or a clear() call — its
    # next event is judged fresh, on the same cumulative counters, so a
    # session still over budget re-trips immediately with the same detector.
    # This re-admits; it does not reset. A windowed budget that actually
    # zeroes on a schedule is a different, unbuilt feature.
    latch_ttl_seconds: float | None = None
    on_anomaly: str = "warn"  # "warn" | "raise" | "callback"
    callback: Callable[[Anomaly], None] | None = None
    # What the agent's tools may do, stated by the customer and enforced at the
    # tool hook before the body runs. A dict is coerced to a ToolPolicy in
    # validate(). Its own on_violation decides the reaction; on_anomaly governs
    # detectors and has no say over policy.
    tool_policy: ToolPolicy | dict | None = None
    # The CI gate. True: a tool decorated with @runbound.tool that states no
    # rule (blocked, max_calls, constraint, require_approval, or allow=True to
    # say it was reviewed and needs none) is a ValueError — raised by init()
    # for every such tool already imported, and by the decorator itself for
    # every one declared afterwards, since decorators normally run after
    # init(). Any CI step that imports the app fails with it, so a tool cannot
    # reach production without a stated rule.
    # Under require_rules an effect="irreversible" tool needs a rule that
    # can refuse it (blocked, max_calls, constraint, require_approval);
    # reviewed=True alone does not pass the gate for an action that cannot be
    # undone. Enforced where tools are decorated (api._require_rule).
    require_rules: bool = False
    # Fleet mode. control_plane_url turns it on: workers say hello at init,
    # ask at session entry and report at session exit, and share budgets,
    # latches and org policy through the plane. Everything below is off or
    # local by default, and every plane failure is answered locally — the
    # plane can raise the floor of what the SDK knows, never lower it.
    #
    # token is the credential from your runbound dashboard; it is what
    # turns fleet mode on. A bare token is enough to connect: validate()
    # points control_plane_url at the hosted plane for you when one is set
    # and no url was given (HOSTED_PLANE_URL is None until that plane
    # exists, so today this instead logs one WARNING and stays local — see
    # _normalize_token). token="" is itself a deliberate credential — "this
    # plane is self-hosted with no auth" — and never invents a url. api_key
    # is the old name for this same secret: it keeps working for one
    # release, folding into token in validate() (with a one-time
    # deprecation warning, whether or not token was also set), so nothing
    # downstream reads api_key after that point.
    #
    # Alert delivery (Slack, PagerDuty, a signed webhook) is not a setting
    # here at all any more: it is a paid, plan-gated feature the
    # control plane routes and delivers, configured as an alert route on
    # your runbound dashboard, not a keyword on this call. A truthiness
    # check running inside your own process could never have enforced that
    # gate — control_plane_url="" walked straight past one — so the sending
    # code left rather than grow a second one. init() answers the five
    # retired field names (slack_webhook, pagerduty_routing_key, webhook_url,
    # webhook_secret, link_template) with a ValueError naming where the
    # setting went, so nobody keeping one around after an upgrade hits a
    # bare TypeError instead.
    #
    # service names the fleet this process belongs to and worker_id names this
    # process inside it (hostname:pid when unset). The timeout is what a plane
    # call may cost a session at the door; poll_s how often a worker checks in;
    # cache_s how long one key's entry answer is reused before the plane is
    # asked again, which is also how long a latch takes to reach a worker that
    # is already holding a cached answer for that key.
    # export_events sends the event stream (counts and hashes, never content);
    # send_session_keys is the opt-in to send raw keys instead of hashes, and
    # stays False — export.py reads it for telemetry, and it is the one field
    # of the old delivery block that survives because it is not about sending
    # anything itself. on_halt is what an org-wide halt does here.
    control_plane_url: str | None = None
    token: str | None = field(default=None, repr=False)  # secret
    api_key: str | None = field(default=None, repr=False)  # secret; deprecated alias for token
    # Which kind of customer this is, worked out by validate() and never
    # passed in (hence init=False: `init(plane_mode=...)` is a TypeError).
    # "off" is the free SDK, local and offline. "hosted" is our server: a
    # token and nothing else, and we resolve the endpoint. "self_hosted" is
    # the customer's own cluster, named by control_plane_url. Every site that
    # needs to know whether there is a plane asks this one field, because the
    # an early version had two call sites asking two different
    # falsiness questions about control_plane_url, and `""` fell into the gap
    # between them.
    _plane_mode: str | None = field(default=None, init=False, repr=False)
    service: str = "default"
    worker_id: str | None = None
    control_plane_timeout_s: float = 0.15
    control_plane_poll_s: float = 5.0
    control_plane_cache_s: float = 5.0
    export_events: bool = True
    send_session_keys: bool = False
    on_halt: str = "raise"  # "raise" | "warn"
    # What a halt does while the plane link is itself degraded ("hold" keeps
    # it enforced past the usual 60s; "release" is that today's behavior),
    # and what an unanswerable entry question does ("refuse" the entry rather
    # than fall back to local detection alone). Read by RemoteState/plane.py.
    stale_halt: str = "release"  # "release" | "hold"
    on_plane_loss: str = "guard_locally"  # "guard_locally" | "refuse"
    custom_prices: dict[
        str, tuple[float, float] | tuple[float, float, float] | tuple[float, float, float, float]
    ] = field(default_factory=dict)
    # model -> (usd_per_1M_input_tokens, usd_per_1M_output_tokens), optionally
    # extended with a cached-input *read* rate and, further, a
    # cache-*write* rate: (..., usd_per_1M_cached_input, usd_per_1M_cache_write).
    # Always wins over the built-in runbound.pricing.PRICES table, which is
    # exactly how a price that changed since PRICES_AS_OF gets fixed.
    # What a model with neither a static nor a custom price does to a call.
    # "zero" (default) counts it as free, same as always, and warns once per
    # model per process so a dollar budget's blind spot is not a silent one.
    # "estimate" prices it from unpriced_price_per_1m_usd instead (required
    # then) and marks the event "estimated" rather than exact. "refuse" stops
    # the call at the door, before it goes out, whatever on_anomaly says —
    # a call that still reaches accounting anyway (record_call(), or a model
    # only known after the response) is priced like "estimate" when a
    # fallback pair was given, else like "zero", and logged once either way.
    on_unpriced_model: str = "zero"
    unpriced_price_per_1m_usd: tuple[float, float] | None = None
    # Opt-in, off by default: budget_usd's post-call wall is the
    # identity of this product — deterministic, never an estimate — so an
    # admission check that guesses a call's cost *before* it goes out is
    # something a customer chooses, not something turned on for them. When
    # True, Engine.admit() refuses a call whose estimated cost would push
    # total_cost_usd + spend_offset_usd past budget_usd, using the same price
    # table the post-call check uses. admission_output_tokens is the assumed
    # output size when a request states no max_tokens/max_completion_tokens/
    # max_output_tokens cap; it is never used when a request names one.
    #
    # Default "capped": reserve only when the request
    # states its own output cap (max_tokens / max_completion_tokens /
    # max_output_tokens). A stated cap is an exact upper bound on the output
    # side, so the reservation is not a guess about output; the input side is
    # still the chars/4 estimate. A call with no stated cap is not checked here
    # at all and takes 0.3.0's path. True keeps the assumed-cap estimate
    # (admission_output_tokens stands in for a missing cap); False restores
    # 0.3.0, where nothing is checked before a call goes out.
    budget_admission: bool | str = "capped"
    admission_output_tokens: int = 1024
    # A soft line under budget_usd, as a fraction of it (0.8 is 80%).
    # The first call that takes the session past it gets one "warn" anomaly
    # (detector "budget", details["limit_hit"] == "budget_soft") and nothing
    # latches. on_budget_soft="safe_mode" also puts the session in safe mode
    # (safe mode), lifted once spend is back under the line or by clear().
    budget_soft: float | None = None
    on_budget_soft: str = "notify"
    # Postures. `postures` overrides or adds rows in the built-in table
    # ({name: {capability class: verdict}}); `capabilities` states a class rule
    # that holds whatever the posture is ({class: "allow"|"deny"|"approve"}).
    # Both are validated against runbound.posture, which owns the vocabulary.
    postures: dict | None = None
    capabilities: dict | None = None
    # Tool names exempt from the loop window by policy rather than by
    # decorator: a name here is treated like @runbound.tool(polling=True)
    # for both tool_call and tool_request events, whoever runs it. Mark
    # polling; do not tune it to dodge a real loop.
    loop_ignore_tools: tuple[str, ...] = ()
    # What the caller is told when a session is refused: the customer's own
    # status/message per detector (or "default"), overriding
    # runbound.responses.BUILTIN. See that module for the profile shape and
    # precedence against a control-plane profile. None (the default) leaves
    # BUILTIN in effect for everything.
    refusals: dict | None = None
    # The execution envelope. True (the default — the package carries no
    # compatibility burden) admits a model call
    # through Engine.admit's new door stages (steps, run time, tokens) before
    # it goes out, and a tool action through the same door's blast-radius
    # stage (max_actions_per_run), instead of discovering the overrun only
    # after the call already happened. False restores 0.3.0's behavior
    # exactly: those stages are skipped, and only the post-call walls
    # (StepDetector, TimeoutDetector, the tokens half of BudgetDetector) ever
    # fire. Money admission (budget_admission) and posture/capability
    # enforcement are unaffected either way — they are their own, older
    # opt-ins, not new envelope controls.
    envelope: bool = True
    # How many tool actions (executed @runbound.tool calls) one run may
    # make. None (the default) means no cap. Enforced at the door, before the
    # (max_actions_per_run + 1)th call's body runs, under
    # ``envelope=True`` only — this is a new control, so it is silent unless
    # both it and the envelope are on. Refused as detector "fanout",
    # rule "actions", and latches (CONTROLS §2.5: a deterministic envelope
    # deny does).
    max_actions_per_run: int | None = None
    # Local telemetry is free: told about every
    # record runbound.local_events appends to its in-memory ring (an
    # anomaly, a refusal, a posture transition or a Decision) as a plain
    # dict, the moment it happens, in this process. Never required — the
    # ring is readable without one through runbound.events()/decisions() —
    # and a callback that raises is logged and swallowed (fail-open), the
    # same as every other observer this SDK calls.
    on_event: Callable[[dict], None] | None = None

    def hard_loop_threshold(self) -> int:
        """Repeat count at which ``on_loop="escalate"`` stops the agent.

        ``loop_hard_threshold`` when set, otherwise twice ``loop_threshold``.
        """
        if self.loop_hard_threshold is not None:
            return self.loop_hard_threshold
        return 2 * self.loop_threshold

    def is_graded_loop(self) -> bool:
        """Does the graded loop policy govern this config (``on_loop`` is
        ``None`` or ``"graded"``)?"""
        return self.on_loop in (None, "graded")

    def alert_loop_threshold(self) -> int:
        """Repeat count at which the graded policy pages someone.

        ``loop_alert_threshold`` when set, otherwise
        :data:`LOOP_ALERT_MULTIPLE` times ``loop_threshold``, never past
        ``loop_window`` (a window can only ever hold that many repeats) and
        never below ``loop_threshold``.
        """
        if self.loop_alert_threshold is not None:
            return self.loop_alert_threshold
        return max(self.loop_threshold, min(LOOP_ALERT_MULTIPLE * self.loop_threshold, self.loop_window))

    def contain_loop_threshold(self) -> int:
        """Repeat count at which the graded policy hands the loop to the spike
        ladder: ``loop_contain_threshold`` when set, otherwise
        :data:`LOOP_CONTAIN_MULTIPLE` times ``loop_threshold``, never past
        ``loop_window`` and never below the alert threshold."""
        if self.loop_contain_threshold is not None:
            return self.loop_contain_threshold
        return max(self.alert_loop_threshold(), min(LOOP_CONTAIN_MULTIPLE * self.loop_threshold, self.loop_window))

    def resolved_worker_id(self) -> str:
        """This process's name inside the fleet.

        ``worker_id`` when the customer set one, used verbatim. Otherwise
        ``"<hostname>:<pid>:<6 lowercase hex chars>"`` — a hostname and a pid
        alone are not enough: two containers sharing a hostname (the common
        case behind an orchestrator) collide on the same id, and a process
        restarted after a crash reuses the pid of the one it replaced, so a
        dashboard would show a worker that "never left." The random suffix
        makes every process's default id unique regardless of host or pid
        reuse. It is computed once per :class:`GuardrailConfig` instance —
        cached on first resolution, not re-rolled on every call — so a
        worker's id is one stable string for the process's whole life, not a
        new random identity every time something asks for it. A hostname
        lookup that fails yields ``"unknown"`` rather than an exception, and
        a `secrets` failure (no OS randomness available) yields the fixed
        placeholder suffix ``"000000"`` rather than one: this is a
        display/correlation id, not a security token, and fail-open means a
        worker without a good name is still a worker, never a crash.
        """
        if self.worker_id:
            return self.worker_id
        cached = getattr(self, "_resolved_worker_id", None)
        if cached is not None:
            return cached
        try:
            host = socket.gethostname() or "unknown"
        except Exception:
            host = "unknown"
        try:
            suffix = secrets.token_hex(3)
        except Exception:
            suffix = "000000"
        resolved = f"{host}:{os.getpid()}:{suffix}"
        self._resolved_worker_id = resolved
        return resolved

    def validate(self) -> None:
        """Raise ``ValueError`` if this configuration cannot be honored.

        Normalizes ``token`` first, before any other plane check: an
        ``api_key`` folds into it (see :meth:`_normalize_token`), then an
        unset ``token`` is read from ``RUNBOUND_TOKEN``, then a non-empty
        ``token`` with no ``control_plane_url`` points fleet mode at the
        hosted plane. Only after that do the rest of the checks run.

        Rejects an unknown ``on_anomaly`` mode or ``on_loop`` policy, a
        callback that is missing or that would never be called, non-positive
        limits, a loop threshold below 2, a loop window too small to ever hold
        the threshold, a hard threshold that is not above the loop threshold,
        and throttle delays that are not positive or that cap below the base.
        Also rejects a ``loop_max_period`` below 2, a ``loop_stall_turns``
        below 1, and a ``loop_shapes`` that is not a tuple of ``str`` or that
        names anything outside :data:`LOOP_SHAPES` — a typo'd shape name fails
        configuration rather than silently detecting nothing.
        Also rejects spike settings that could never produce a baseline (a
        warmup below 2, a window that cannot outgrow it, a factor of 1 or less,
        a confirmation count outside 1..5, a floor that is not positive), a
        session registry with no room, ladder knobs below their minimum, and
        ``on_spike="limit"`` without the latch its cooldown needs. Rejects an
        unknown ``on_provider_failure`` mode and a circuit knob that is not
        positive. Rejects an unknown ``circuit_mode``, a ``circuit_failure_
        rate``/``circuit_slow_rate`` outside ``(0, 1]``, a non-positive
        ``circuit_min_calls``/``circuit_half_open_calls``, a ``circuit_slow_
        call_seconds`` that is neither positive nor ``None``, and a non-bool
        ``circuit_fleet``/``circuit_posture``. Rejects fleet settings that could not work: a control plane
        url with no token, a plane timeout outside ``(0, 2.0]`` seconds or a
        poll interval that is not positive, and an unknown ``on_halt`` mode. A
        ``tool_policy`` written as a dict is coerced to a :class:`ToolPolicy`
        here and validated with it, and a non-bool ``require_rules`` is
        rejected. Rejects a ``refusals`` profile whose entry
        for some key is not a dict, whose ``status`` is not an int in
        200-599, or whose ``message`` is not a string of at most 500
        characters — always naming the offending key. Rejects an unknown
        ``on_unpriced_model`` mode, an ``unpriced_price_per_1m_usd`` that is
        not a 2-tuple of non-negative numbers or ``None``, and that mode set
        to ``"estimate"`` with no such pair to estimate from. Rejects a
        non-bool ``budget_admission`` and an ``admission_output_tokens`` that
        is not a positive int. Rejects a ``loop_ignore_tools`` that is not a
        tuple of ``str``, and an unknown ``stale_halt`` or ``on_plane_loss``
        mode.
        """
        self._normalize_connection()
        self._validate_reaction()
        self._validate_limits()
        self._validate_budget_window()
        self._validate_loop()
        self._validate_loop_policy()
        self._validate_spike()
        self._validate_circuit()
        self._validate_plane()
        self._validate_policy()
        self._validate_refusals()
        self._validate_unpriced()
        self._validate_admission()
        self._validate_loop_ignore_tools()
        self._validate_fleet_modes()

    @property
    def plane_mode(self) -> str:
        """``"off"`` | ``"hosted"`` | ``"self_hosted"`` — which kind of
        customer this is, and the only question any reader should ask about
        whether there is a plane.

        :meth:`validate` settles it (see :meth:`_normalize_connection`). A
        configuration that has not been validated has no settled answer, so
        this derives one from the url the same way validation would — blank
        is not a url — which keeps ``shared.build`` correct for the
        hand-built configurations in the test suite and for anything
        duck-typed. Only the hosted/self-hosted distinction needs
        validation's memory, because hosted is the mode where *we* supply the
        url.
        """
        if self._plane_mode is not None:
            return self._plane_mode
        return "self_hosted" if (self.control_plane_url or "").strip() else "off"

    def _normalize_connection(self) -> None:
        """Work out how — and whether — this process connects to a plane.

        There are exactly two kinds of connected customer, and they are not
        one setting with a truthiness test:

        * **hosted** — a ``token`` and nothing else. They are on our server;
          we resolve the endpoint, and the token says which org they are.
          They never type a url.
        * **self_hosted** — a ``control_plane_url``. They run the plane in
          their own cluster, and ``token`` is whatever that plane requires
          (``""`` when it requires nothing).

        Both facts can come from the call or from the environment
        (``RUNBOUND_TOKEN``, ``RUNBOUND_PLANE_URL``), and a blank value
        from either source counts as unset in both cases: **there is no such
        thing as a plane at the empty url.** That is fixed here, once, rather
        than at each reader — an early version had
        ``_default_alerters`` test ``is None`` while ``shared.build`` tested
        falsiness, so ``control_plane_url=""`` looked like a plane to one and
        no plane to the other.

        Steps, in order:

        1. ``api_key`` is the old name for this secret. A caller who still
           sets it gets exactly one WARNING and ``api_key`` is cleared to
           ``None``, so every check after this point (and every other
           module) reads ``token`` alone — and this fires whether or not
           ``token`` was *also* passed: setting both used to be silent and
           left two secrets sitting on the object, which is worse than
           either mistake alone. An explicit ``token`` still wins over
           ``api_key`` when both are set; the warning and the clearing do
           not.
        2. With no ``token`` yet, ``RUNBOUND_TOKEN`` is read from the
           environment. A value that is empty or all whitespace counts as
           unset, the same as never having set the variable at all.
        3. With no ``control_plane_url`` passed, ``RUNBOUND_PLANE_URL`` is
           read from the environment — and this remembers that it came from
           there, which step 4 needs. Blank or whitespace — from either
           source — becomes ``None``, and no longer counts as "from the
           environment" either: there is no url to have a source.
        4. A url with no token is a self-hosted plane with a credential
           missing, but *which* mistake it is depends on who wrote the url.
           A url **passed to** ``init()`` is one engineer writing one line —
           if there is no token, it is almost always a typo, so this still
           raises the same message it always has (:meth:`_validate_plane`).
           A url that came from ``RUNBOUND_PLANE_URL`` is a deployment
           fact, not a call site — a base image or shared manifest can set
           it long before every service built from it has been issued a
           token, and this product's thesis is failing open everywhere
           except the policy gates. So an env-sourced url with no token
           cannot raise: it logs exactly one WARNING and the url is put back
           to ``None``, leaving the process local exactly as if
           ``RUNBOUND_PLANE_URL`` had never been set.
        5. A url with a token, from either source, means **self_hosted**.
        6. Otherwise a non-empty ``token`` means **hosted**:
           ``control_plane_url`` becomes
           :data:`runbound.shared.HOSTED_PLANE_URL`, which is ``None``
           until that plane exists — in which case this logs one WARNING and
           the process stays local (``plane_mode == "off"``).

        Two blank values read very differently and it is worth being
        explicit about which is which, because the review found a claim here
        that did not match the code. ``token=""`` only ever answers "what is
        my credential" — it stops step 2 from reading ``RUNBOUND_TOKEN``
        (an explicit ``""`` is not ``None``), but it does nothing to
        ``control_plane_url``, so a ``RUNBOUND_PLANE_URL`` in the
        environment still names a plane and ``token=""`` then correctly
        means "that plane, no auth" (self-hosted). The setting that actually
        pins a process local regardless of what the shell exports is
        ``control_plane_url=""``: passed explicitly, it is not ``None``, so
        step 3 never consults the environment for it either, and it
        normalizes to ``None`` in step 3's blank check — which is how a
        library embedding this SDK, or a test harness, says "never connect,
        whatever the caller's shell exports" without also having to silence
        ``RUNBOUND_TOKEN``.
        """
        if self.api_key is not None:
            _LOG.warning(
                "runbound: api_key is now token; api_key still works in "
                "0.2.x and will be removed"
            )
            if self.token is None:
                self.token = self.api_key
            self.api_key = None
        if self.token is None:
            env_token = os.environ.get("RUNBOUND_TOKEN")
            if env_token is not None and env_token.strip():
                self.token = env_token
        control_plane_url_from_env = False
        if self.control_plane_url is None:
            self.control_plane_url = os.environ.get("RUNBOUND_PLANE_URL")
            control_plane_url_from_env = self.control_plane_url is not None
        if self.control_plane_url is not None and not self.control_plane_url.strip():
            self.control_plane_url = None
            control_plane_url_from_env = False
        if self.control_plane_url is not None:
            if self.token is None and control_plane_url_from_env:
                # Same missing credential as _validate_plane rejects below,
                # but the url came from the environment rather than from a
                # line someone just typed, so raising here would let a base
                # image's ENV take down every service that hasn't been
                # issued a token yet. Warn once, forget the url, stay local.
                _warn_env_plane_url_no_token()
                self.control_plane_url = None
            else:
                self._plane_mode = "self_hosted"
        elif self.token:
            self.control_plane_url = HOSTED_PLANE_URL
            if self.control_plane_url is None:
                # A token, and nowhere to send it yet. One warning, and the
                # process guards locally exactly as it would with no token.
                _warn_token_has_no_plane()
            else:
                self._plane_mode = "hosted"

    def _validate_reaction(self) -> None:
        if self.on_trip not in ("latch", "once"):
            raise ValueError(f'on_trip must be "latch" or "once", got {self.on_trip!r}')
        if self.on_anomaly not in ON_ANOMALY_MODES:
            raise ValueError(
                f"on_anomaly must be one of {ON_ANOMALY_MODES}, got {self.on_anomaly!r}"
            )
        if self.on_anomaly == "callback" and self.callback is None:
            raise ValueError('on_anomaly="callback" requires a callback')
        if self.callback is not None and self.on_anomaly != "callback":
            raise ValueError(
                'callback is set but on_anomaly is "%s"; it would never be called '
                '(use on_anomaly="callback")' % self.on_anomaly
            )

    def _validate_limits(self) -> None:
        for name in _POSITIVE_LIMITS:
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None, got {value!r}")

    def _validate_loop(self) -> None:
        if self.loop_threshold < 2:
            raise ValueError(
                f"loop_threshold must be >= 2, got {self.loop_threshold!r}"
            )
        if self.loop_window < self.loop_threshold:
            raise ValueError(
                f"loop_window ({self.loop_window!r}) must be >= "
                f"loop_threshold ({self.loop_threshold!r})"
            )
        if not isinstance(self.loop_max_period, int) or isinstance(
            self.loop_max_period, bool
        ) or self.loop_max_period < 2:
            raise ValueError(
                f"loop_max_period must be an int >= 2, got {self.loop_max_period!r}"
            )
        if not isinstance(self.loop_stall_turns, int) or isinstance(
            self.loop_stall_turns, bool
        ) or self.loop_stall_turns < 1:
            raise ValueError(
                f"loop_stall_turns must be an int >= 1, got {self.loop_stall_turns!r}"
            )
        if not isinstance(self.loop_shapes, tuple) or not all(
            isinstance(name, str) for name in self.loop_shapes
        ):
            raise ValueError(
                f"loop_shapes must be a tuple of str, got {self.loop_shapes!r}"
            )
        unknown = [name for name in self.loop_shapes if name not in LOOP_SHAPES]
        if unknown:
            raise ValueError(
                f"loop_shapes contains unknown shape(s) {unknown!r}; "
                f"each must be one of {LOOP_SHAPES}"
            )

    def _validate_loop_policy(self) -> None:
        if self.on_loop not in ON_LOOP_POLICIES:
            raise ValueError(
                f"on_loop must be one of {ON_LOOP_POLICIES}, got {self.on_loop!r}"
            )
        if self.loop_hard_threshold is not None and (
            not isinstance(self.loop_hard_threshold, int)
            or self.loop_hard_threshold <= self.loop_threshold
        ):
            raise ValueError(
                f"loop_hard_threshold must be an int greater than loop_threshold "
                f"({self.loop_threshold!r}) or None, got {self.loop_hard_threshold!r}"
            )
        self._validate_graded_thresholds()
        for name in ("throttle_base_seconds", "throttle_max_seconds"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value!r}")
        if self.throttle_max_seconds < self.throttle_base_seconds:
            raise ValueError(
                f"throttle_max_seconds ({self.throttle_max_seconds!r}) must be >= "
                f"throttle_base_seconds ({self.throttle_base_seconds!r})"
            )

    def _validate_graded_thresholds(self) -> None:
        """The graded policy's rungs must be ints in order: ``loop_threshold`` <
        ``loop_alert_threshold`` < ``loop_contain_threshold`` <= ``loop_window``
        (a window cannot hold more repeats than its size). Only the ones set
        explicitly are checked; the defaults are derived to fit."""
        for name in ("loop_alert_threshold", "loop_contain_threshold"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"{name} must be an int or None, got {value!r}")
        alert, contain = self.loop_alert_threshold, self.loop_contain_threshold
        if alert is not None and alert <= self.loop_threshold:
            raise ValueError(
                f"loop_alert_threshold ({alert!r}) must be greater than loop_threshold "
                f"({self.loop_threshold!r})"
            )
        if contain is not None:
            floor = alert if alert is not None else self.loop_threshold
            if contain <= floor:
                raise ValueError(
                    f"loop_contain_threshold ({contain!r}) must be greater than "
                    f"{'loop_alert_threshold' if alert is not None else 'loop_threshold'} ({floor!r})"
                )
            if contain > self.loop_window:
                raise ValueError(
                    f"loop_contain_threshold ({contain!r}) must be <= loop_window "
                    f"({self.loop_window!r}): the window cannot hold more repeats than its size"
                )
        if alert is not None and alert > self.loop_window:
            raise ValueError(
                f"loop_alert_threshold ({alert!r}) must be <= loop_window ({self.loop_window!r})"
            )

    def _validate_spike(self) -> None:
        if self.on_spike not in ON_SPIKE_MODES:
            raise ValueError(
                f"on_spike must be one of {ON_SPIKE_MODES}, got {self.on_spike!r}"
            )
        if self.spike_warmup_calls < 2:
            raise ValueError(
                f"spike_warmup_calls must be >= 2, got {self.spike_warmup_calls!r}"
            )
        if self.spike_window <= self.spike_warmup_calls:
            raise ValueError(
                f"spike_window ({self.spike_window!r}) must be greater than "
                f"spike_warmup_calls ({self.spike_warmup_calls!r})"
            )
        if self.spike_factor <= 1:
            raise ValueError(f"spike_factor must be > 1, got {self.spike_factor!r}")
        if not 1 <= self.spike_confirm <= SPIKE_CONFIRM_MAX:
            raise ValueError(
                f"spike_confirm must be between 1 and {SPIKE_CONFIRM_MAX}, "
                f"got {self.spike_confirm!r}"
            )
        for name in _SPIKE_FLOORS:
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value!r}")
        if self.max_sessions < 1:
            raise ValueError(f"max_sessions must be >= 1, got {self.max_sessions!r}")
        self._validate_ladder()

    def _validate_circuit(self) -> None:
        """The provider circuit's mode and its always-on knobs."""
        if self.on_provider_failure not in ON_PROVIDER_FAILURE_MODES:
            raise ValueError(
                f"on_provider_failure must be one of {ON_PROVIDER_FAILURE_MODES}, "
                f"got {self.on_provider_failure!r}"
            )
        for name in _CIRCUIT_KNOBS:
            value = getattr(self, name)
            if value is None or value <= 0:
                raise ValueError(f"{name} must be positive, got {value!r}")
        self._validate_circuit_rate()

    def _validate_circuit_rate(self) -> None:
        """The circuit's ``circuit_mode`` and its own knobs, validated whatever the mode.

        Rejects an unknown ``circuit_mode``; a ``circuit_failure_rate`` or
        ``circuit_slow_rate`` outside ``(0, 1]``; a non-positive
        ``circuit_min_calls`` or ``circuit_half_open_calls``; a
        ``circuit_slow_call_seconds`` that is neither ``None`` nor positive;
        and a non-bool ``circuit_fleet`` or ``circuit_posture``.
        """
        if self.circuit_mode not in CIRCUIT_MODE_VALUES:
            raise ValueError(
                f"circuit_mode must be one of {CIRCUIT_MODE_VALUES}, "
                f"got {self.circuit_mode!r}"
            )
        for name in _CIRCUIT_RATE_FRACTIONS:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 1
            ):
                raise ValueError(f"{name} must be > 0 and <= 1, got {value!r}")
        if (
            isinstance(self.circuit_min_calls, bool)
            or not isinstance(self.circuit_min_calls, int)
            or self.circuit_min_calls <= 0
        ):
            raise ValueError(
                f"circuit_min_calls must be a positive int, got {self.circuit_min_calls!r}"
            )
        if (
            isinstance(self.circuit_half_open_calls, bool)
            or not isinstance(self.circuit_half_open_calls, int)
            or self.circuit_half_open_calls <= 0
        ):
            raise ValueError(
                "circuit_half_open_calls must be a positive int, got "
                f"{self.circuit_half_open_calls!r}"
            )
        if self.circuit_slow_call_seconds is not None and (
            isinstance(self.circuit_slow_call_seconds, bool)
            or not isinstance(self.circuit_slow_call_seconds, (int, float))
            or self.circuit_slow_call_seconds <= 0
        ):
            raise ValueError(
                "circuit_slow_call_seconds must be positive or None, got "
                f"{self.circuit_slow_call_seconds!r}"
            )
        if not isinstance(self.circuit_fleet, bool):
            raise ValueError(f"circuit_fleet must be a bool, got {self.circuit_fleet!r}")
        if not isinstance(self.circuit_posture, bool):
            raise ValueError(f"circuit_posture must be a bool, got {self.circuit_posture!r}")

    def _validate_plane(self) -> None:
        """The control-plane knobs, checked whether or not a plane is set.

        A token is required *by the url*, not by itself: pointing at a plane
        with no credential is a typo in production. An empty string is a
        deliberate credential, though — that is how a self-hosted plane behind
        a customer's own network boundary says "no auth here". Runs after
        :meth:`_normalize_connection`, so this only ever sees a normalized
        pair: an ``api_key`` has been folded into ``token``, a blank url is
        already ``None``, and ``plane_mode`` is settled.
        """
        if self.control_plane_url is not None and self.token is None:
            raise ValueError(
                "a self-hosted plane needs a token: control_plane_url is set "
                'but token is not (use token="" if that plane has no auth). '
                "A token on its own, with no url, is the hosted plane."
            )
        if not 0 < self.control_plane_timeout_s <= CONTROL_PLANE_TIMEOUT_MAX:
            raise ValueError(
                f"control_plane_timeout_s must be > 0 and <= "
                f"{CONTROL_PLANE_TIMEOUT_MAX}, got {self.control_plane_timeout_s!r}"
            )
        if self.control_plane_poll_s <= 0:
            raise ValueError(
                f"control_plane_poll_s must be positive, "
                f"got {self.control_plane_poll_s!r}"
            )
        if self.control_plane_cache_s <= 0:
            raise ValueError(
                f"control_plane_cache_s must be positive, "
                f"got {self.control_plane_cache_s!r}"
            )
        if self.on_halt not in ON_HALT_MODES:
            raise ValueError(
                f"on_halt must be one of {ON_HALT_MODES}, got {self.on_halt!r}"
            )

    def _validate_policy(self) -> None:
        """Coerce a dict policy into a real one, then let it check itself.

        ``require_rules`` is a gate that fails a start-up, so a truthy string
        or a stray ``1`` must not quietly arm or disarm it.
        """
        self.tool_policy = coerce(self.tool_policy)
        if self.tool_policy is not None:
            self.tool_policy.validate()
        if not isinstance(self.require_rules, bool):
            raise ValueError(
                f"require_rules must be a bool, got {self.require_rules!r}"
            )

    def _validate_refusals(self) -> None:
        """Check ``refusals`` against the shape :mod:`runbound.responses` reads.

        Validated once here, loudly, so a customer's typo in a caller-facing
        status code fails at startup rather than being silently ignored (or
        crashing a request) the first time it is resolved. ``None`` is the
        default and always accepted; an unset field within an entry is fine
        too — :mod:`~runbound.responses` falls through to ``"default"``,
        then to its own built-in text, for whatever is left out.
        """
        if self.refusals is None:
            return
        if not isinstance(self.refusals, dict):
            raise ValueError(
                f"refusals must be a dict or None, got {type(self.refusals).__name__}"
            )
        for key, entry in self.refusals.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"refusals[{key!r}] must be a dict with 'status' and/or "
                    f"'message', got {type(entry).__name__}"
                )
            if "status" in entry:
                status = entry["status"]
                if (
                    isinstance(status, bool)
                    or not isinstance(status, int)
                    or not 200 <= status <= 599
                ):
                    raise ValueError(
                        f"refusals[{key!r}]['status'] must be an int between "
                        f"200 and 599, got {status!r}"
                    )
            if "message" in entry:
                message = entry["message"]
                if not isinstance(message, str):
                    raise ValueError(
                        f"refusals[{key!r}]['message'] must be a str, "
                        f"got {type(message).__name__}"
                    )
                if len(message) > 500:
                    raise ValueError(
                        f"refusals[{key!r}]['message'] must be at most 500 "
                        f"characters, got {len(message)}"
                    )

    def _validate_ladder(self) -> None:
        """The ``on_spike="limit"`` knobs, and the latch the ladder rests on."""
        if self.spike_limit_calls < 1:
            raise ValueError(
                f"spike_limit_calls must be >= 1, got {self.spike_limit_calls!r}"
            )
        if self.spike_cooldown_seconds <= 0:
            raise ValueError(
                f"spike_cooldown_seconds must be positive, "
                f"got {self.spike_cooldown_seconds!r}"
            )
        if self.spike_max_strikes < 1:
            raise ValueError(
                f"spike_max_strikes must be >= 1, got {self.spike_max_strikes!r}"
            )
        if self.on_spike == "limit" and self.on_trip != "latch":
            raise ValueError(
                'on_spike="limit" requires on_trip="latch": the cooldown after a '
                f"rollover is enforced by the latch, got on_trip={self.on_trip!r}"
            )

    def _validate_unpriced(self) -> None:
        """The unpriced-model policy and its fallback price pair.

        ``"estimate"`` needs a pair to estimate with; ``"zero"`` and
        ``"refuse"`` do not require one, though a pair given for ``"refuse"``
        is still used if a call somehow reaches accounting anyway (the door
        could not stop it in time).
        """
        if self.on_unpriced_model not in ON_UNPRICED_MODEL_MODES:
            raise ValueError(
                f"on_unpriced_model must be one of {ON_UNPRICED_MODEL_MODES}, "
                f"got {self.on_unpriced_model!r}"
            )
        pair = self.unpriced_price_per_1m_usd
        if pair is not None and not _is_price_pair(pair):
            raise ValueError(
                "unpriced_price_per_1m_usd must be a 2-tuple of non-negative "
                f"numbers or None, got {pair!r}"
            )
        if self.on_unpriced_model == "estimate" and pair is None:
            raise ValueError(
                'on_unpriced_model="estimate" requires unpriced_price_per_1m_usd '
                "to be set to (usd_per_1M_input, usd_per_1M_output)"
            )

    def _validate_admission(self) -> None:
        """The admission budget check, its output guess and the soft line."""
        if not (isinstance(self.budget_admission, bool) or self.budget_admission == "capped"):
            raise ValueError(
                'budget_admission must be "capped", True or False, '
                f"got {self.budget_admission!r}"
            )
        self._validate_budget_soft()
        self._validate_postures()
        tokens = self.admission_output_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError(
                f"admission_output_tokens must be a positive int, got {tokens!r}"
            )
        if not isinstance(self.envelope, bool):
            raise ValueError(f"envelope must be a bool, got {self.envelope!r}")

    def _validate_postures(self) -> None:
        """``postures`` and ``capabilities`` name known classes and verdicts."""
        validate_overrides(self.postures)
        validate_class_rules(self.capabilities)

    def _validate_budget_soft(self) -> None:
        """``budget_soft`` is a fraction strictly inside (0, 1), under a real budget."""
        soft = self.budget_soft
        if soft is not None:
            if isinstance(soft, bool) or not isinstance(soft, (int, float)) or not 0 < soft < 1:
                raise ValueError(
                    "budget_soft must be a fraction strictly between 0 and 1 "
                    f"(0.8 is 80% of budget_usd), got {soft!r}"
                )
            if self.budget_usd is None:
                raise ValueError(
                    "budget_soft is a line under budget_usd, and budget_usd is not set"
                )
        if self.on_budget_soft not in ("notify", "safe_mode"):
            raise ValueError(
                'on_budget_soft must be "notify" or "safe_mode", '
                f"got {self.on_budget_soft!r}"
            )

    def _validate_budget_window(self) -> None:
        """``budget_window`` is a known calendar name, a positive number of
        seconds, or ``None``; it holds only under a real ``budget_usd``."""
        window = self.budget_window
        if window is None:
            return
        if isinstance(window, str):
            if window not in BUDGET_WINDOW_NAMES:
                raise ValueError(
                    f"budget_window must be one of {BUDGET_WINDOW_NAMES}, a "
                    f"positive number of seconds, or None, got {window!r}"
                )
        elif isinstance(window, bool) or not isinstance(window, (int, float)) or window <= 0:
            raise ValueError(
                f"budget_window must be one of {BUDGET_WINDOW_NAMES}, a "
                f"positive number of seconds, or None, got {window!r}"
            )
        if self.budget_usd is None:
            raise ValueError(
                "budget_window governs budget_usd, and budget_usd is not set"
            )

    def _validate_loop_ignore_tools(self) -> None:
        """``loop_ignore_tools`` must be a tuple of tool names."""
        if not isinstance(self.loop_ignore_tools, tuple) or not all(
            isinstance(name, str) for name in self.loop_ignore_tools
        ):
            raise ValueError(
                f"loop_ignore_tools must be a tuple of str, got {self.loop_ignore_tools!r}"
            )

    def _validate_fleet_modes(self) -> None:
        """The two plane-loss knobs T61 reads straight off this config."""
        if self.stale_halt not in STALE_HALT_MODES:
            raise ValueError(
                f"stale_halt must be one of {STALE_HALT_MODES}, got {self.stale_halt!r}"
            )
        if self.on_plane_loss not in ON_PLANE_LOSS_MODES:
            raise ValueError(
                f"on_plane_loss must be one of {ON_PLANE_LOSS_MODES}, "
                f"got {self.on_plane_loss!r}"
            )
        if self.on_event is not None and not callable(self.on_event):
            raise ValueError(f"on_event must be callable or None, got {self.on_event!r}")
