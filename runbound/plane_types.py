"""The wire contract between an SDK worker and the control plane.

Everything the SDK ever puts on the network has a frozen dataclass in this
module, and nothing else does. That is the point: the privacy promise ("hashes
and counts, never content") is a property of these types, checkable by reading
one file. A session key is only ever sent as :func:`key_hash` of itself, an
event's error *text* has no field to travel in, and a detector's ``details``
dict is scrubbed down to JSON scalars before it leaves the process.

A detector writes the key it is talking about into its own ``details`` and into
its message ("Error storm for session 'user:9': …"), because a local log with
the key in it is the useful one. That is exactly where a raw key would escape,
so the last thing :func:`anomaly_to_wire` does is :func:`redact_key`: unless
the customer opted into ``send_session_keys``, every occurrence of the key in
the message and in the details is replaced by :func:`redacted_key` of it. The
detectors are left alone; the redaction lives at the wire boundary, which is
the only place it has to be right.

The module imports :mod:`runbound.events` and the standard library, nothing
else, so it can be imported from anywhere in the SDK without a cycle.

Decoding is deliberately tolerant: :func:`from_wire` fills a missing key with
the field's default, ignores a key it does not know, and falls back to the
default when a value has the wrong type. A plane that is one version ahead of
the SDK must never crash the agent it is talking to.
"""

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, get_args, get_type_hints

from .events import Anomaly, Event

#: Longest string a scrubbed ``details`` value (or an anomaly message) keeps.
DETAIL_STRING_MAX = 256

#: Most keys a scrubbed ``details`` dict keeps — and, for the same reason, the
#: most items a scrubbed list keeps.
DETAIL_KEYS_MAX = 64

#: How deep :func:`scrub_details` will follow nested containers before it
#: drops what is below. Also what stops a self-referencing details dict.
DETAIL_DEPTH_MAX = 4

#: Most tags one session may carry to the plane, and the longest a tag key or
#: value may be.
TAG_KEYS_MAX = 32
TAG_VALUE_MAX = 64

#: Longest ``error_class`` accepted from an error string.
ERROR_CLASS_MAX = 128

#: How many hex characters of a key's digest stand in for a redacted key, and
#: the ellipsis that marks the result as a stand-in rather than a key.
REDACTED_HASH_CHARS = 12
REDACTED_ELLIPSIS = "…"

#: A leading ``"Name: "`` or ``"pkg.mod.Name: "`` in an error message. Python's
#: own ``"%s: %s" % (type, message)`` shape, and what every provider SDK
#: produces; anything else (a bare sentence, a URL, a number) matches nothing.
_ERROR_PREFIX_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*:"
)

#: A whole error string that is nothing but an exception class name — what
#: ``engine._error_text`` falls back to when ``str(exc)`` itself raises.
_ERROR_BARE_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*(?:Error|Exception|Timeout)$")


@dataclass(frozen=True)
class WireEvent:
    """One observed action, as the plane sees it.

    The event's ``error`` text has no field here; only ``error_class``
    survives, and only when the message names a class (see
    :func:`event_to_wire`). ``key_hash`` is a digest, never a key.

    ``priced``, ``partial`` and ``tokens_estimated`` mirror the like-named
    fields on :class:`~runbound.events.Event` — how a call was priced, and
    whether it ever actually finished. ``loop_exempt`` does not appear here on
    purpose: it is local bookkeeping for the loop window and has no meaning to
    the plane.
    """

    ts_wall: str = ""
    kind: str = ""
    key_hash: str | None = None
    step: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    tool_name: str | None = None
    args_hash: str | None = None
    duration_s: float = 0.0
    error_class: str | None = None
    priced: str | None = None
    partial: bool = False
    tokens_estimated: bool = False


@dataclass(frozen=True)
class WireAnomaly:
    """A detector's verdict and what the SDK did about it.

    ``reacted`` is one of ``"raise"``, ``"warn"``, ``"callback"``,
    ``"dry_run"``, ``"blocked"`` or ``"door"`` (refused at session entry).

    ``anomaly_id`` is the id :class:`~runbound.events.Anomaly` stamped on
    itself when the detector returned it — the same string on this anomaly's
    telemetry export and on the :class:`TripReport` that reports the same
    anomaly, so the plane files one row for the two. Empty when the anomaly
    came from somewhere that has no id to give (a plane one release ahead
    reading an older worker's payload sees the same empty string).
    """

    ts_wall: str = ""
    key_hash: str | None = None
    detector: str = ""
    severity: str = ""
    message: str = ""
    details: dict = field(default_factory=dict)
    reacted: str = ""
    anomaly_id: str = ""


@dataclass(frozen=True)
class EntryDecision:
    """The plane's answer when a session opens.

    ``allow`` false means the plane refuses this session at the door, with
    ``refusal`` saying why. ``fleet_spend_usd`` and ``fleet_tokens`` are what
    the rest of the fleet has already spent under this key — the offsets the
    budget detector adds to the local counters. ``latch``, when set, is a
    remote trip this worker must honor as if it had made it itself.

    ``baseline_*`` and ``rung_*`` belong here, on the per-*key* entry
    decision, rather than on :class:`Controls` (the per-*service* payload a
    heartbeat carries): both are facts about the one key this call is
    opening a session for, exactly like ``strikes``, ``generation`` and
    ``latch`` above — none of which ride ``Controls`` either, because
    ``Controls`` has no per-key dimension at all (one worker fetches one
    body per service, shared across every key it serves; a per-key field on
    it would mean every key's number riding every other key's request).
    The service-wide median (the "peer baseline") is the opposite shape —
    one number *for* the service, not for any one key — and rides
    :class:`Controls` instead.
    """

    allow: bool = True
    refusal: dict | None = None
    fleet_spend_usd: float = 0.0
    fleet_tokens: int = 0
    strikes: int = 0
    generation: int = 0
    latch: dict | None = None
    halt: bool = False
    policy_version: int = 0
    #: This key's own spike baseline, as the plane last had it —
    #: written by some worker's exit (never a live median taken mid-spike;
    #: see :class:`ExitDelta`), read back so a worker that never served this
    #: key before (a restart, or simply a different replica) judges its
    #: first call the way every other worker already would.
    #: ``baseline_samples`` is the plane's "is there really something here"
    #: flag: 0 means no worker has ever reported this key's baseline.
    baseline_duration_s: float = 0.0
    baseline_output_tokens: float = 0.0
    baseline_samples: int = 0
    #: The abuse ladder's own rung — ``spike_level`` (0 quiet, 1
    #: watching, 2 limited; 3/closed is never delivered here, since a
    #: closed key's cooldown already rides ``latch`` above) and, at level 2,
    #: the allowance left and what it started at. ``None`` when the level
    #: is not 2 — an allowance is only meaningful while limited. Restoring
    #: these three is what lets a worker that restarted mid-spike re-enter
    #: a key at the same rung instead of forgiving it back to 0.
    rung_level: int = 0
    rung_allowance: int | None = None
    rung_allowance_start: int | None = None


@dataclass(frozen=True)
class ExitDelta:
    """What one session block added, reported when it closes.

    Deltas, not totals: two workers on the same key each report their own
    share, and the plane adds them up. ``seq`` orders one worker's reports so
    a retry cannot be counted twice.

    ``steps_delta`` carries **model turns** (an agent step is one model
    turn, not every recorded event) — a session with 3 model calls and 7 tool
    calls reports ``steps_delta=3``, not ``10``. ``events_delta`` is that
    raw event count instead — one field per concept, each named for what
    it holds, rather than a second meaning riding ``steps_delta`` itself.

    ``errors_delta`` and ``tokens_cached_delta`` are the same kind of
    running-total diff as ``tokens_delta``: how many ``llm_error``/
    ``tool_error`` events, and how many cached input tokens, this block added.

    ``last_detector``, ``trigger_message`` and ``trigger_age_s`` describe
    the anomaly that last stopped this session — ``None`` for a
    session that never tripped (including every ``on_anomaly="warn"``
    session, which never latches). ``trigger_message`` is the SDK's own
    anomaly sentence, built entirely from hashes, counts, timing, money,
    names and error classes (see :func:`anomaly_to_wire`, which this reuses
    :func:`redact_key` from) — never prompt or tool-argument content, and
    never a raw session key unless ``send_session_keys`` is on.
    ``trigger_age_s`` is *this worker's own monotonic clock*, seconds since
    the anomaly fired: the SDK's clock and the plane's are not the same
    clock, so an age survives the trip across the wire where a timestamp
    would not — the plane stamps ``trigger_ts = now - trigger_age_s`` on
    its own clock on arrival.
    """

    key_hash: str = ""
    seq: int = 0
    spend_delta_usd: float = 0.0
    tokens_delta: int = 0
    steps_delta: int = 0
    tool_calls: dict = field(default_factory=dict)
    events_delta: int = 0
    errors_delta: int = 0
    tokens_cached_delta: int = 0
    last_detector: str | None = None
    trigger_message: str | None = None
    trigger_age_s: float | None = None
    #: A snapshot of this key's *held, trusted* spike baseline at the
    #: moment this block exited — ``state.spike_baseline`` — never the live
    #: medians taken while a spike train was still running (the held
    #: baseline is held for exactly that reason: see
    #: :func:`runbound.detectors._baseline`). ``baseline_samples`` is 0
    #: (the "nothing to report" default) whenever this worker did not learn
    #: this baseline itself — it never warmed up locally, or it was itself
    #: seeded from a restored or peer baseline — so the plane's stored
    #: baseline for this key is never overwritten with someone else's (or
    #: the whole service's) numbers reported back as if they were this
    #: key's own.
    baseline_duration_s: float = 0.0
    baseline_output_tokens: float = 0.0
    baseline_samples: int = 0
    #: A snapshot of the abuse ladder's rung at exit — ``spike_level``
    #: and, only while ``LEVEL_LIMITED`` (2), the allowance left and its
    #: starting point. Reported on every exit (not diffed, like
    #: ``last_detector`` above), so the plane always has this worker's
    #: latest word on where the key sits, ready for the next entry —
    #: anywhere, by any worker — to restore it from.
    rung_level: int = 0
    rung_allowance: int | None = None
    rung_allowance_start: int | None = None


@dataclass(frozen=True)
class TripReport:
    """A critical trip, told to the plane so the rest of the fleet learns it.

    ``anomaly_id`` repeats the id inside ``anomaly`` at the top level, where
    the plane's writer reads it: this report and the telemetry export of the
    same anomaly are one refusal, and the id is how the plane knows that
    without timing the two arrivals.

    ``worker_id`` is which worker was refused. The SDK leaves it empty and
    the plane fills it from the ``X-Runbound-Worker`` header the same call
    already carries (``routers/sdk._worker``); the field exists so a report
    the *plane* writes for its own door refusal, and a caller replaying one,
    have a documented place to say it.
    """

    key_hash: str | None = None
    anomaly: dict = field(default_factory=dict)
    latch_ttl_s: float | None = None
    strikes: int = 0
    generation: int = 0
    refused_at_door: bool = False
    anomaly_id: str = ""
    worker_id: str = ""


@dataclass(frozen=True)
class Controls:
    """The plane's execution-envelope controls for one service.

    Exactly the wire shape the control plane's own Controls service stores
    and ``/v1/controls`` answers with, field for field:

    * ``limits`` — ``{level: {field: number | None}}`` for
      ``level`` in ``("org", "service", "agent", "key", "run", "action")``
      and ``field`` in ``("budget_usd", "window_s", "max_steps",
      "max_events", "loop_threshold", "max_cost_per_call_usd",
      "max_call_seconds", "max_tokens_out_per_call")``. This worker
      collapses every level it can read (org, service, run — see
      :mod:`runbound.controls_merge`) into one effective number per field;
      ``agent``, ``key`` and ``action`` are carried and shown but not
      enforced by any worker yet.
    * ``capabilities`` — ``{class: "allow" | "approve" | "deny"}``, the same
      six classes and three verdicts :mod:`runbound.policy` and
      :mod:`runbound.posture` already use.
    * ``posture`` — one of :data:`runbound.posture.POSTURE_NAMES`, or
      ``None``. Carried here for completeness; the posture that actually
      reaches a worker rides :attr:`HelloReply.posture` instead, so
      nothing reads this field today.
    * ``detectors`` — ``{name: {"action": "stop" | "notify", "mode":
      "shadow" | "enforce"}}``. ``notify`` never latches; ``shadow`` never
      stops — see :mod:`runbound.controls_merge` and
      :meth:`runbound.engine.Engine.controls_detector_override`.
    * ``envelope`` — ``bool | None``. ``None`` ("not stated") and ``False``
      rank the same (the loosest); only ``True`` can tighten.
    * ``spike_enabled`` — does the spike
      detector's ratio-based judgement and the abuse ladder run at all for
      this service? This field's own wire default is ``False`` ("the plane
      states nothing"), but the *effective* value a worker actually uses is
      ``True`` even with no plane connected at all — see
      :meth:`runbound.engine.Engine._effective_spike_enabled`, which merges
      this field against the worker's own free, local ``spike_detection``
      default. Deliberately does not gate the per-call hard
      ceilings (``max_call_seconds``, ``max_tokens_out_per_call``,
      ``max_cost_per_call_usd``): those are free run limits, unrelated to
      the learned baseline, and keep firing from call one whatever this
      flag says.
    * ``spike`` — the eleven tuning knobs (``spike_detection``,
      ``on_spike`` and the
      nine under it: ``limit_calls``, ``cooldown_seconds``,
      ``max_strikes``, ``warmup_calls``, ``min_duration_s``,
      ``min_output_tokens``, ``window``, ``factor``, ``confirm``) now have
      no local field at all, the same "presence, not tightening" rule as
      ``circuit_rate``/``loop_shapes``/``budget_soft`` below. ``None``
      means every tunable defaults to the value the removed ``init()``
      keyword used to default to (``mode="notify"``, ``limit_calls=5``,
      ``cooldown_seconds=300.0``, ``max_strikes=3``, ``warmup_calls=4``,
      ``min_duration_s=2.0``, ``min_output_tokens=500``, ``window=50``,
      ``factor=10.0``, ``confirm=2``) when this worker's own local
      configuration states nothing either — read regardless of
      ``spike_enabled``, since a session's own bookkeeping (its allowance
      base, its baseline window size) needs a number whether or not the
      detector is currently gated on. See
      :meth:`runbound.engine.Engine._effective_config` and
      :func:`runbound.controls_merge.effective_spike`.
    * ``service_baseline_duration_s`` / ``service_baseline_output_tokens`` /
      ``service_baseline_keys`` — this service's median spike
      baseline, a median of every key's own baseline (one vote per key,
      never weighted by call volume, so a single high-volume key cannot
      define what "normal" means for a key that has never called before).
      ``service_baseline_keys`` is how many distinct keys the median is
      over; 0 means the plane has nothing for this service yet. Read by
      :meth:`runbound.shared.RemoteState.service_baseline`, which — unlike
      :meth:`~runbound.shared.RemoteState.controls_directive` — does not
      gate on ``dry_run``: a peer baseline is advisory context for a call
      already happening, not an enforcement directive that needs a
      rollout state to guard. Per-key facts — this key's own restored
      baseline, its ladder rung — are `EntryDecision`'s, not this
      payload's: see that class's own docstring for why. Read regardless
      of ``spike_enabled``: the median itself is harmless context, read
      only when the gated detector above is already about to act on it.
    * ``circuit_rate`` / ``circuit_posture`` / ``loop_shapes`` /
      ``budget_soft`` / ``max_actions_per_run`` — every one of these is a
      real, local ``init()`` keyword: this worker's own value, tightened by
      whatever this field states (see :mod:`runbound.controls_merge`'s
      ``effective_circuit_rate``/``effective_circuit_posture``/
      ``effective_loop_shapes``/``effective_budget_soft``/
      ``effective_max_actions_per_run``). ``circuit_rate`` is ``None``
      (nothing stated) or ``{"min_calls", "failure_rate",
      "slow_call_seconds", "slow_rate", "half_open_calls"}``;
      ``circuit_posture`` is a plain bool, independent of ``circuit_
      rate``'s mode — a count-mode breaker narrows the posture exactly
      like a rate-mode one, so nesting it inside ``circuit_rate`` would
      have silently forced rate mode on a plane that only meant to turn
      posture-narrowing on; ``loop_shapes`` is ``None`` (nothing stated)
      or ``{"shapes": [...], "max_period", "stall_turns"}``;
      ``budget_soft`` is ``None`` (nothing stated) or ``{"fraction",
      "reaction"}``; ``max_actions_per_run`` is ``None`` (nothing stated)
      or a positive int. ``circuit_fleet`` stays on the wire for schema
      parity with the plane's own Controls row but is no longer read here:
      it opts a worker in or out of the fleet-wide circuit fold, and that
      is a real local ``init()`` keyword again too
      (:meth:`runbound.shared.RemoteState._circuit_fleet` reads
      ``config.circuit_fleet`` directly — a
      feature that only ever does anything once there is a plane to fold
      with was never actually a control the plane needed to state).

    Never sent by this worker — this is what the plane answers ``/v1/
    controls`` with, decoded by :meth:`runbound.plane.PlaneClient.controls`.
    """

    limits: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    posture: str | None = None
    detectors: dict = field(default_factory=dict)
    envelope: bool | None = None
    spike_enabled: bool = False
    spike: dict | None = None
    service_baseline_duration_s: float = 0.0
    service_baseline_output_tokens: float = 0.0
    service_baseline_keys: int = 0
    circuit_rate: dict | None = None
    circuit_posture: bool = False
    circuit_fleet: bool = False
    loop_shapes: dict | None = None
    budget_soft: dict | None = None
    max_actions_per_run: int | None = None


@dataclass(frozen=True)
class HelloReply:
    """The plane's answer to a hello: who we are and what has changed.

    ``poll_s`` lets the plane slow a chatty fleet down without a redeploy.

    ``tools_known`` is the plane saying whether it already holds this worker's
    tool report; ``False`` makes the next heartbeat resend it in full. It
    defaults to **True**, and that default is the whole point: a plane too old
    to know about tool reports omits the field, :func:`from_wire` then takes
    this default, and a default of ``False`` would have every worker in the
    fleet resend its whole report on every heartbeat, forever, against a plane
    that was never going to store it. "Assume the plane has it unless the plane
    says otherwise" costs one stale report and nothing else.
    """

    org_id: str = ""
    plan: str = ""
    entitlements: dict = field(default_factory=dict)
    halt: bool = False
    policy_version: int = 0
    circuits: dict = field(default_factory=dict)
    poll_s: float = 0.0
    notice: str | None = None
    tools_known: bool = True
    #: The posture the plane states for this service, by name -- every
    #: worker is tightened to it on its next heartbeat. Tighten-only twice
    #: over: it sets the plane's own slot, so it can never lift a worker's
    #: manual narrowing, and the effective posture is the stricter of the two.
    #: ``None`` is "the plane states nothing", which is not the same as
    #: ``"full"``: a plane that says ``"full"`` is lifting its own narrowing.
    posture: str | None = None
    #: This scope's current :class:`Controls` version, in the
    #: same style as ``policy_version`` — a worker fetches the body from
    #: ``/v1/controls`` only when this changes. Zero means "the plane has
    #: nothing served for this scope" (an older plane omits the field
    #: entirely, which :func:`from_wire` also reads as zero, so an SDK newer
    #: than the plane it is talking to simply never fetches).
    controls_version: int = 0
    #: Which kind of ``halt`` is in effect — ``"stop"`` (refuse every
    #: guarded session at the door, today's whole behavior) or ``"narrow"``
    #: (posture ``restricted`` fleet-wide: model calls keep serving, every
    #: non-read tool refuses). Only meaningful while ``halt`` is true; a
    #: plane that predates this field, or a reply that omits it, is read as
    #: ``"stop"`` — the only kind of halt that existed before this, so an
    #: older plane's fleet-wide stop keeps meaning exactly what it always
    #: did. Kept entirely separate from ``posture`` on purpose (see
    #: :mod:`runbound.shared`'s ``RemoteState._absorb_halt_posture`` and
    #: :data:`runbound.state.POSTURE_SOURCES`'s ``"halt"`` entry): a Narrow
    #: halt and a Controls-stated posture are two independent plane sources
    #: that must tighten together and lift independently, never share one
    #: slot.
    halt_mode: str = "stop"


@dataclass(frozen=True)
class PlaneStatus:
    """What the SDK will tell a customer about its link to the plane.

    ``"local"`` means no plane is configured at all, ``"connected"`` that the
    last contact worked, ``"limited"`` that it worked but the org's plan is
    having entry decisions made locally, and ``"degraded"`` that it did not
    work and every answer is being made locally.

    ``entitlements`` is the last :class:`HelloReply` entitlements dict — what
    the plan allows, what it is denying and why — and is ``{}`` until a
    heartbeat has been answered.

    ``halt_stale_s`` is seconds since the last successful contact with the
    plane while a fleet-wide halt is being enforced, and ``None`` whenever no
    halt is currently enforced (none received, or ``stale_halt="release"``
    already let a stale one lapse).
    """

    mode: str = "local"
    last_contact_age_s: float | None = None
    consecutive_failures: int = 0
    notice: str | None = None
    entitlements: dict = field(default_factory=dict)
    halt_stale_s: float | None = None


def key_hash(key: str) -> str:
    """The sha256 hex digest of ``key``, the only form a key travels in.

    Encoded as UTF-8 with ``errors="replace"``, so a key carrying a lone
    surrogate (or anything else unencodable) hashes instead of raising in a
    hot path over a value the SDK did not choose. Non-string keys are
    stringified first.
    """
    if not isinstance(key, str):
        key = str(key)
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


def event_to_wire(event: Event, key_hash: str | None, ts_wall: str) -> WireEvent:
    """Convert one :class:`~runbound.events.Event` into its wire form.

    The event's ``error`` message never travels. What travels instead is
    ``error_class``, derived from that message by a deliberately narrow
    heuristic: the identifier in a leading ``"RateLimitError: ..."`` (the last
    segment of a dotted ``"openai.APITimeoutError: ..."``), or a whole message
    that is itself an exception class name — nothing else. A message with no
    such prefix ("connection reset by peer", "429 too many requests") yields
    ``None`` rather than a guess, because a guess would be a piece of the
    message, and the message may hold anything.

    ``priced``, ``partial`` and ``tokens_estimated`` pass straight through;
    ``loop_exempt`` is deliberately left off the wire — see :class:`WireEvent`.
    """
    return WireEvent(
        ts_wall=ts_wall,
        kind=event.kind,
        key_hash=key_hash,
        step=event.step,
        tokens_in=event.tokens_in,
        tokens_out=event.tokens_out,
        tokens_reasoning=event.tokens_reasoning,
        cost_usd=event.cost_usd,
        model=event.model,
        tool_name=event.tool_name,
        args_hash=event.args_hash,
        duration_s=event.duration_s,
        error_class=error_class_of(event.error),
        priced=event.priced,
        partial=event.partial,
        tokens_estimated=event.tokens_estimated,
    )


def error_class_of(error: str | None) -> str | None:
    """The exception class named by ``error``, or ``None`` when unclear.

    See :func:`event_to_wire` for why this is the only part of an error
    message allowed onto the wire.
    """
    if not isinstance(error, str) or not error:
        return None
    match = _ERROR_PREFIX_RE.match(error)
    if match:
        name = match.group(1).rsplit(".", 1)[-1]
        if name[:1].isupper():
            return name[:ERROR_CLASS_MAX]
        return None
    if _ERROR_BARE_RE.match(error):
        return error[:ERROR_CLASS_MAX]
    return None


def redacted_key(key: str, digest: str | None = None) -> str:
    """What a raw session key looks like once it has been redacted.

    The first :data:`REDACTED_HASH_CHARS` characters of the key's sha256 hex
    digest, then ``"…"`` — e.g. ``"3f2a9c1b04d7…"``. Long enough that two
    records about the same key still line up, far too short to reverse,
    and visibly not a key. ``digest`` is the full :func:`key_hash` when the
    caller already has it; it is computed here when not.
    """
    return (digest or key_hash(key))[:REDACTED_HASH_CHARS] + REDACTED_ELLIPSIS


def redact_key(value: Any, key: str | None, digest: str | None = None) -> Any:
    """``value`` with every occurrence of the raw ``key`` replaced by its stand-in.

    Takes either a string (an anomaly's message) or a ``details`` mapping, and
    returns the same shape with every *string* rewritten — dict values and
    list items included, to :data:`DETAIL_DEPTH_MAX` levels. The input is
    never mutated, and anything that is not a string, list or dict comes back
    untouched, so a caller can hand this a raw details dict before scrubbing.

    The match is a plain substring one (:meth:`str.replace`, never a regex):
    keys are chosen by our customers and routinely contain ``.``, ``*``,
    ``(`` and every other metacharacter, and a key that is a bad pattern must
    still be redacted. A key that is not a non-empty string is nothing to
    search for, and ``value`` is returned unchanged.
    """
    if not isinstance(key, str) or not key:
        return value
    return _redact(value, key, redacted_key(key, digest), DETAIL_DEPTH_MAX)


def anomaly_to_wire(
    anomaly: Anomaly,
    reacted: str,
    key_hash: str | None,
    ts_wall: str,
    *,
    send_session_keys: bool = False,
    key: str | None = None,
) -> WireAnomaly:
    """Convert one :class:`~runbound.events.Anomaly` into its wire form.

    ``details`` is scrubbed (see :func:`scrub_details`) and the message is
    truncated: a detector's message is written by us, but the numbers in it
    come from the run, and a run can produce a very long one.

    ``anomaly_id`` travels as the detector stamped it — a random hex string
    with nothing of the session in it — so the plane can recognise this
    anomaly's two reports as one. An object without one (a test double, an
    anomaly built by hand) sends an empty id and is deduped the old way.

    ``key`` is the session's raw key, and is what makes this safe: detectors
    put it in ``details["key"]`` and quote it in their messages, so unless
    ``send_session_keys`` is set every occurrence of it is replaced by
    :func:`redacted_key` first — *before* the truncation, so a key can never
    survive in half. With ``send_session_keys`` on, or with no key to look
    for, the anomaly travels exactly as the detector wrote it.
    """
    message: Any = anomaly.message
    details: Any = anomaly.details
    if not send_session_keys:
        message = redact_key(message, key, key_hash)
        details = redact_key(details, key, key_hash)
    return WireAnomaly(
        ts_wall=ts_wall,
        key_hash=key_hash,
        detector=str(anomaly.detector),
        severity=str(anomaly.severity),
        message=str(message)[:DETAIL_STRING_MAX],
        details=scrub_details(details),
        reacted=reacted,
        anomaly_id=_anomaly_id(anomaly),
    )


def _anomaly_id(anomaly: Any) -> str:
    """The id an anomaly stamped on itself, or ``""`` if it has none."""
    value = getattr(anomaly, "anomaly_id", "")
    return value if isinstance(value, str) else ""


def _redact(value: Any, key: str, stand_in: str, depth: int) -> Any:
    """One value with ``key`` replaced by ``stand_in``, to ``depth`` levels."""
    if isinstance(value, str):
        return value.replace(key, stand_in)
    if depth <= 0:
        return value
    if isinstance(value, dict):
        return {
            name: _redact(item, key, stand_in, depth - 1) for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, key, stand_in, depth - 1) for item in value]
    return value


def scrub_details(details: Any) -> dict:
    """A JSON-safe copy of a detector's ``details`` dict.

    Keeps strings (truncated to :data:`DETAIL_STRING_MAX`), numbers, booleans,
    ``None``, and lists and dicts of those, to a depth of
    :data:`DETAIL_DEPTH_MAX` and at most :data:`DETAIL_KEYS_MAX` entries each.
    Everything else — callables, sessions, exceptions, any object a detector
    happened to put there — is dropped rather than stringified, because
    stringifying it is how content escapes. Non-string dict keys go too. The
    input is never mutated, and a self-referencing dict simply runs out of
    depth.
    """
    scrubbed = _scrub(details, DETAIL_DEPTH_MAX)
    return scrubbed if isinstance(scrubbed, dict) else {}


def scrub_tags(tags: Any) -> dict:
    """A capped, stringified copy of a session's tags.

    At most :data:`TAG_KEYS_MAX` entries; keys and values are strings cut to
    :data:`TAG_VALUE_MAX`. A value that cannot be stringified is dropped, not
    guessed at.
    """
    if not isinstance(tags, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in tags.items():
        if len(out) >= TAG_KEYS_MAX:
            break
        if not isinstance(key, str):
            continue
        try:
            text = value if isinstance(value, str) else str(value)
        except Exception:
            continue
        out[key[:TAG_VALUE_MAX]] = text[:TAG_VALUE_MAX]
    return out


def to_wire(obj: Any) -> dict:
    """The JSON-ready dict for one wire dataclass instance."""
    return dataclasses.asdict(obj)


def from_wire(cls: Any, data: Any) -> Any:
    """Build ``cls`` from a plane payload, tolerating anything it says.

    A missing key takes the field's default, an unknown key is ignored, and a
    value of the wrong type takes the default too — an int is accepted for a
    float field (JSON has one number type) but a bool never stands in for an
    int. A payload that is not a dict at all yields an all-defaults instance.
    """
    if not isinstance(data, dict):
        return cls()
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for spec in dataclasses.fields(cls):
        if spec.name not in data:
            continue
        value = data[spec.name]
        hint = hints.get(spec.name, Any)
        if not _acceptable(value, hint):
            continue
        kwargs[spec.name] = _coerce(value, hint)
    return cls(**kwargs)


def _options(hint: Any) -> tuple:
    """The concrete types a hint allows (``str | None`` -> ``(str, None)``)."""
    return get_args(hint) or (hint,)


def _acceptable(value: Any, hint: Any) -> bool:
    """Whether ``value`` may be used for a field annotated ``hint``."""
    for option in _options(hint):
        if option is type(None):
            if value is None:
                return True
            continue
        if option is bool:
            if isinstance(value, bool):
                return True
            continue
        if isinstance(value, bool):
            continue  # a bool is not a number or a string here
        if option is float and isinstance(value, (int, float)):
            return True
        if isinstance(option, type) and isinstance(value, option):
            return True
    return False


def _coerce(value: Any, hint: Any) -> Any:
    """Widen an int to a float, and copy a dict so the payload stays ours."""
    options = _options(hint)
    if float in options and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _scrub(value: Any, depth: int) -> Any:
    """One value, reduced to JSON scalars, or :data:`_DROP` if it cannot be."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:DETAIL_STRING_MAX]
    if depth <= 0:
        return _DROP
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if len(out) >= DETAIL_KEYS_MAX:
                break
            if not isinstance(key, str):
                continue
            scrubbed = _scrub(item, depth - 1)
            if scrubbed is not _DROP:
                out[key[:DETAIL_STRING_MAX]] = scrubbed
        return out
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if len(items) >= DETAIL_KEYS_MAX:
                break
            scrubbed = _scrub(item, depth - 1)
            if scrubbed is not _DROP:
                items.append(scrubbed)
        return items
    return _DROP


class _Drop:
    """Sentinel: this value has no place on the wire."""


_DROP = _Drop()
