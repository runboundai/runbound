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
    """

    ts_wall: str = ""
    key_hash: str | None = None
    detector: str = ""
    severity: str = ""
    message: str = ""
    details: dict = field(default_factory=dict)
    reacted: str = ""


@dataclass(frozen=True)
class EntryDecision:
    """The plane's answer when a session opens.

    ``allow`` false means the plane refuses this session at the door, with
    ``refusal`` saying why. ``fleet_spend_usd`` and ``fleet_tokens`` are what
    the rest of the fleet has already spent under this key — the offsets the
    budget detector adds to the local counters. ``latch``, when set, is a
    remote trip this worker must honor as if it had made it itself.
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


@dataclass(frozen=True)
class ExitDelta:
    """What one session block added, reported when it closes.

    Deltas, not totals: two workers on the same key each report their own
    share, and the plane adds them up. ``seq`` orders one worker's reports so
    a retry cannot be counted twice.

    ``steps_delta`` carries **model turns** (T134: an agent step is one model
    turn, not every recorded event) — a session with 3 model calls and 7 tool
    calls reports ``steps_delta=3``, not ``10``. ``events_delta`` (T146) is
    that raw event count instead — one field per concept, each named for
    what it holds (an EM ruling settled a draft that would have kept a
    second meaning on ``steps_delta`` itself; nothing was released, so there
    was no reader to protect by doing that).

    ``errors_delta`` and ``tokens_cached_delta`` (T139) are the same kind of
    running-total diff as ``tokens_delta``: how many ``llm_error``/
    ``tool_error`` events, and how many cached input tokens, this block added.

    ``last_detector``, ``trigger_message`` and ``trigger_age_s`` (T146)
    describe the anomaly that last stopped this session — ``None`` for a
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


@dataclass(frozen=True)
class TripReport:
    """A critical trip, told to the plane so the rest of the fleet learns it."""

    key_hash: str | None = None
    anomaly: dict = field(default_factory=dict)
    latch_ttl_s: float | None = None
    strikes: int = 0
    generation: int = 0
    refused_at_door: bool = False


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
    records about the same end user still line up, far too short to reverse,
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
    )


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
