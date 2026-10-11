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
import uuid
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

    Nothing about a model call is here: the SDK guards tool calls only. ``loop_exempt``
    does not appear either, on purpose: it is local bookkeeping for the loop window
    and has no meaning to the plane.
    """

    ts_wall: str = ""
    kind: str = ""
    key_hash: str | None = None
    step: int = 0
    tool_name: str | None = None
    args_hash: str | None = None
    duration_s: float = 0.0
    error_class: str | None = None


@dataclass(frozen=True)
class WireAnomaly:
    """A detector's verdict and what the SDK did about it.

    ``reacted`` is one of ``"raise"``, ``"warn"``, ``"callback"``,
    ``"dry_run"``, ``"blocked"`` or ``"door"`` (refused at session entry).

    ``anomaly_id`` is the id :class:`~runbound.events.Anomaly` stamped on
    itself when the detector returned it — the same string on every export of
    this anomaly, so the plane files one row for a re-sent batch. Empty when the anomaly
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
class PlaneUnavailable:
    """The body every SDK-path 503 the plane cannot answer normally carries
    — ``app.UNAVAILABLE``/``app.STORE_UNAVAILABLE`` on the plane, mirrored
    here so the SDK's own parsing (``PlaneClient._plane_loss_kind``) reads
    against one contract rather than a hand-written literal on each side.

    ``error`` is always ``"plane_unavailable"`` — the one field every such
    503 has carried since before this class existed, and the only one an
    SDK older than this field knows to read: that SDK still treats the
    whole body as plane loss, unchanged. ``cause`` is additive and empty
    (``""``, not ``None`` — plain strings round-trip through
    :func:`from_wire` more tolerantly than an optional field) for the
    generic case: a saturated connection pool, a handler bug, any other
    unhandled failure on an SDK path. It is ``"fleet_state_unavailable"``
    for exactly one, more specific case: the live store itself —
    ``StoreUnavailable``, not a bug — could not be reached. Only that one
    value means the plane's own *state*, not merely its process or its
    database, is what is gone; every other 503 with this shape must not be
    read as a state outage. See :data:`FLEET_STATE_UNAVAILABLE_CAUSE`.
    """

    error: str = "plane_unavailable"
    cause: str = ""


#: The one ``PlaneUnavailable.cause`` value that means "the plane's own
#: state store, not merely its process, is unreachable" — see that class's
#: docstring and :meth:`~runbound.plane.PlaneClient._plane_loss_kind`.
FLEET_STATE_UNAVAILABLE_CAUSE = "fleet_state_unavailable"


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
    #: halt and a plane-stated posture are two independent plane sources
    #: that must tighten together and lift independently, never share one
    #: slot.
    halt_mode: str = "stop"
    #: Whether the plane's own fleet state (the live store every halt,
    #: latch and fleet budget lives in) could be read at all -- ``"ok"`` or
    #: ``"unavailable"``. A plane whose store is unreachable still answers
    #: the heartbeat itself (the link is fine), but every other field above
    #: (``halt``, ``halt_mode``, ``policy_version``,
    #: ``posture``) then carries only its wire
    #: default, never a real reading -- so ``"unavailable"`` is what tells
    #: :meth:`runbound.shared.RemoteState.apply_hello` to hold what it
    #: already knew rather than read that default as the plane's answer.
    #: Absence (an older plane) decodes to ``"ok"``, the same "nothing to
    #: report" reasoning every other field on this class follows.
    fleet_state: str = "ok"
    #: Deferred approvals a person approved that this worker may execute: ``[{"id", "tool",
    #: "args", "request_id", "seal", "service", "expires_at"}]``, one per reply. The worker
    #: verifies each item's seal before running anything; see :mod:`runbound.approvals`.
    approved: list = field(default_factory=list)
    #: Approval ids this worker asked to renew (``renew_claims`` on the hello) that are no longer its own:
    #: the claim lapsed, another worker holds it, or the request is done. Absent when none.
    claims_lost: list = field(default_factory=list)
    #: This worker's share of every service-scoped quota in the current window: ``[{"tool", "state", "limit",
    #: "window", "window_start", "window_end", "granted", "remaining"}]``, present only when there is one.
    quota_leases: list = field(default_factory=list)
    #: Why the plane is answering but not whole, when it says so (``"ledger_unreachable"``: it cannot reach its own database), else
    #: ``None``. Informational: it changes no decision the SDK makes; it is surfaced as ``PlaneStatus.plane_degraded`` and as one
    #: ``plane_degraded`` event per change. A plane that predates the field, or leaves it out, is read as ``None``. Any other key a
    #: reply carries is ignored, as it always was.
    plane_degraded: str | None = None


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

    ``entries_window`` is how the last minute's session-entry decisions were
    actually made — ``{"plane": n, "cache": n, "local": n, "local_causes":
    {"timeout": n, "plane_loss": n, "plane_unavailable": n, "error": n}}``
    — and ``entries_local_share`` is ``local / (plane + cache + local)``
    over that same window, ``0.0`` with nothing in it yet. A link can
    answer every heartbeat and still be having most entries decided
    locally (a plane slow enough to miss ``control_plane_timeout_s`` on
    the hot path, but fast enough to answer the background heartbeat) —
    that is exactly what these two catch. ``local_causes`` is why each of
    those local entries was decided locally: ``"timeout"`` (the client
    gave up, or the entry was skipped outright on an already-degraded
    link), ``"plane_loss"`` (a 503 naming
    :data:`FLEET_STATE_UNAVAILABLE_CAUSE` — the plane's own state store is
    unreachable), ``"plane_unavailable"`` (the same 503 shape with no such
    cause — the plane answered but could not help for some other reason: a
    saturated connection pool, a handler bug), or ``"error"`` (anything
    else — no plane-shaped answer at all). ``mode`` reads ``"degraded"``
    for the window case too, once it holds enough entries to mean
    something, and ``reason`` says which path is failing: ``"heartbeat
    failures"`` (the older, coarser failure count — wins when more than
    one applies), ``"fleet state unavailable"`` (the plane answers the
    heartbeat but cannot read its own live store — a hello reply's
    ``fleet_state == "unavailable"`` — *or* the entry window's own majority
    local cause is ``"plane_loss"``: the same fact, learned at a different
    door), ``"entry timeouts"`` (the window's majority cause is genuine
    timeouts), ``"plane unavailable"`` (the window's majority cause is the
    plane answering but not helping, for no state-specific reason), or
    ``"plane errors"`` (the window's majority cause is neither — a
    connection refused, a malformed reply, and so on), or ``None`` while
    nothing is wrong.

    ``applied_policy_version`` is the org policy version this worker has
    actually fetched and installed — what its decisions are made against —
    and ``None`` until one has been (and always without a plane). It is not
    the version the last heartbeat announced: an announced version whose
    fetch failed, or has not happened yet, is not applied. Read-only, and
    nothing about it travels on the wire.

    ``plane_degraded`` is the plane's own statement that it is answering but not whole (``"ledger_unreachable"``: it cannot
    reach its database), ``None`` when it says nothing or says it is fine. It is informational: no decision changes on it. Each
    change of it (set, changed, cleared) records ONE ``plane_degraded`` event in :func:`runbound.events` and to ``on_event``,
    never one per hello.
    """

    mode: str = "local"
    last_contact_age_s: float | None = None
    consecutive_failures: int = 0
    notice: str | None = None
    entitlements: dict = field(default_factory=dict)
    halt_stale_s: float | None = None
    entries_window: dict = field(default_factory=dict)
    entries_local_share: float = 0.0
    reason: str | None = None
    applied_policy_version: int | None = None
    #: Seconds since the on-disk policy cache this worker runs on was written (``None`` with none), and
    #: whether it is older than 24 h while the plane is not answering. Local only; nothing here is on the wire.
    cache_age_s: float | None = None
    stale: bool = False
    #: What the plane last said is wrong with itself while still answering hellos (``"ledger_unreachable"``), or ``None``: nothing
    #: said, or the last hello said nothing is. Not the link's own health (that is ``mode``): a plane can be ``connected`` and
    #: degraded at once. It rides the hello and is read-only; nothing about it is sent back.
    plane_degraded: str | None = None


#: Longest a ``batch_id`` may be — a plane column width, mirroring
#: ``ANOMALY_ID_MAX``'s own reasoning: the SDK sends 32 hex characters
#: (:func:`fresh_batch_id`'s own output), and this is simply how much
#: room the wire leaves for one, in case a future SDK's id shape grows.
BATCH_ID_MAX = 64


def fresh_batch_id() -> str:
    """A new id for one exported batch (``/v1/events``'s own envelope).

    The exporter calls this exactly once per *genuinely new* batch it
    assembles (:meth:`~runbound.export.Exporter._take`) and keeps the same
    id across that batch's own resends — a batch the plane could not
    confirm landed is retried with an unchanged id, so the plane can tell
    "this is the same batch again" from "this is a new one" without
    inspecting its contents. Random, not content-derived: two batches with
    identical contents (a customer's own retried duplicate telemetry, not
    this exporter's own resend) are still two different batches and must
    not be folded into one.
    """
    return uuid.uuid4().hex


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

    ``loop_exempt`` and the model fields (tokens, cost, model, provider) are
    deliberately left off the wire — see :class:`WireEvent`.
    """
    return WireEvent(
        ts_wall=ts_wall,
        kind=event.kind,
        key_hash=key_hash,
        step=event.step,
        tool_name=event.tool_name,
        args_hash=event.args_hash,
        duration_s=event.duration_s,
        error_class=error_class_of(event.error),
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
