"""Customer-set refusal responses: runbound says *that* a call was refused,
never what the bot should say about it — until now the app had to invent the
HTTP status and the sentence an end user reads. This module lets the customer
state both, once, and carries them to wherever a
:class:`~runbound.exceptions.GuardrailTripped` is raised.

A *profile* is a plain dict, the same shape locally (``GuardrailConfig.refusals``)
and on the wire (the control plane's ``/v1/policy`` envelope's ``"refusals"``
key)::

    {"default": {"status": 429, "message": "..."},
     "policy":  {"status": 403, "message": "A human needs to approve that"},
     "budget":  {"status": 429}}   # message omitted -> falls through

Keys are ``"default"`` or the ``anomaly.detector`` string that names a
refusal. That string is *whatever the engine actually emits* — grepping
``detector=`` across the package turns up: ``"loop"``, ``"budget"``,
``"velocity"``, ``"steps"``, ``"error_storm"``, ``"timeout"``, ``"spike"``
(the detectors), ``"policy"`` (:class:`~runbound.exceptions.PolicyViolation`),
``"circuit"`` (:class:`~runbound.exceptions.CircuitOpen`), ``"halt"`` (an
org-wide halt refused at the door), ``"plane"`` (``on_plane_loss="refuse"``:
the control plane could not be asked at all), ``"fanout"`` (a fan-out limit
refused before a :func:`~runbound.session` block runs) and ``"inflight"`` (the
per-provider in-flight concurrency cap — the product describes this as a
"cap", but the wire key that actually overrides it is ``"inflight"``, because
that is the literal value ``Anomaly.detector`` carries; a profile keyed
``"cap"`` would silently never match anything). A door refusal for a latch
this worker heard about from the fleet, but whose original detector could not
be read, falls back to ``"fleet"``. Every key not present in a profile falls
through to that profile's own ``"default"`` entry, and from there to
:data:`BUILTIN`.

Either field, ``"status"`` (an int 200-599) or ``"message"`` (a str of at most
500 chars), may be omitted from an entry. A message may contain
``{retry_after_s}`` and ``{detector}``; formatting is safe by construction —
an unknown placeholder is left verbatim and a malformed template (a stray
``{``) is returned unformatted, never raised.

Precedence, highest first, checked independently for each field so a plane
profile that only overrides ``status`` does not blank out a customer's local
``message``:

1. the plane's profile (already the org/service merge — see ``controlplane``),
   this detector's own entry
2. the plane's profile, its ``"default"`` entry
3. the local profile (``GuardrailConfig.refusals``), this detector's entry
4. the local profile, its ``"default"`` entry
5. :data:`BUILTIN`

``source`` on the returned :class:`Refusal` names the highest tier that
contributed *anything* to the answer: ``"plane"`` if the remote profile had a
usable entry (even if only for one field), else ``"local"``, else
``"default"``.

Thread safety: :func:`set_remote` is called by the control-plane poller
thread while :func:`refusal_for` is called from request-handling threads.
The remote profile is a single dict reference guarded by a lock on both the
write and the read side, so a reader never observes a profile mid-write.

Import direction: this module sits below :mod:`runbound.api` (which wires
:func:`set_remote` from the poller and reads the local profile out of the
live config) so that :mod:`runbound.exceptions` can import it directly
without a cycle. The one place this module needs something ``api`` owns — the
session registry, to compute ``retry_after_s`` — is reached with a *lazy*,
function-local ``from . import api``, executed only when a refusal is actually
being resolved (by which point ``api`` has always finished importing).

Golden rule applies here like everywhere else: :func:`refusal_for` never
raises. Anything it cannot resolve is logged at DEBUG and answered from
:data:`BUILTIN`.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass

from .events import Anomaly

_LOG = logging.getLogger("runbound")

#: The documented fallback profile. Only the five keys with product copy are
#: listed; every other detector falls through to ``"default"`` here exactly
#: as it would in a customer profile.
BUILTIN: dict = {
    "default": {
        "status": 429,
        "message": (
            "This assistant can't continue this conversation right now. "
            "Please try again later."
        ),
    },
    "policy": {"status": 403, "message": "That action isn't allowed."},
    "circuit": {"status": 503, "message": "The assistant is temporarily unavailable."},
    "halt": {"status": 503, "message": "The assistant is paused for maintenance."},
    "plane": {"status": 503, "message": "The assistant is temporarily unavailable."},
}

_MAX_MESSAGE_CHARS = 500

_REMOTE_LOCK = threading.Lock()
_REMOTE_PROFILE: dict | None = None


@dataclass(frozen=True)
class Refusal:
    """What the app should tell the end user, and how, for one refusal.

    ``status``/``message`` are the customer's own words wherever they set
    them, else :data:`BUILTIN`'s bland default. ``retry_after_s`` is the
    latch's (or cooldown's) remaining time when it is known, else ``None`` —
    a permanent latch, a fresh trip whose session cannot be found, or a
    refusal (like a circuit or an in-flight cap) that is not tied to one
    session at all.
    """

    status: int
    message: str
    detector: str
    retry_after_s: float | None
    source: str  # "plane" | "local" | "default"

    @property
    def headers(self) -> dict[str, str]:
        """``{"Retry-After": "<seconds>"}`` when known, else ``{}``.

        Rounded up: a client told to wait 4.2s and retrying at 4.2s exactly
        would still find the latch held for those last few milliseconds.
        """
        if self.retry_after_s is None:
            return {}
        return {"Retry-After": str(math.ceil(self.retry_after_s))}

    def body(self) -> dict:
        """The JSON-able payload an app can hand straight to its framework."""
        return {
            "refused": True,
            "detector": self.detector,
            "message": self.message,
            "retry_after_s": self.retry_after_s,
        }


def set_remote(profile: dict | None) -> None:
    """Install (or, with ``None``, withdraw) the plane's refusal profile.

    Called by :meth:`runbound.shared.RemoteState._fetch_policy` whenever a
    policy poll answers — every time, so a profile withdrawn on the plane
    (``null``/absent) is withdrawn here too. Anything other than a dict or
    ``None`` is treated as "no profile": a malformed wire payload must cost
    this worker nothing, not a crash on the next refusal.
    """
    global _REMOTE_PROFILE
    resolved = dict(profile) if isinstance(profile, dict) else None
    with _REMOTE_LOCK:
        _REMOTE_PROFILE = resolved


def _get_remote() -> dict | None:
    with _REMOTE_LOCK:
        return _REMOTE_PROFILE


def has_remote() -> bool:
    """Is a plane refusal profile currently in effect? Read by ``coverage()``."""
    return _get_remote() is not None


def refusal_for(anomaly: Anomaly) -> Refusal:
    """Resolve the :class:`Refusal` an app should answer ``anomaly`` with.

    Reads the live plane profile, the live local config and :data:`BUILTIN`,
    in that precedence (see the module docstring), and resolves
    ``retry_after_s`` from whatever session the anomaly names. Never raises:
    any failure anywhere in resolution is logged at DEBUG and answered as
    :data:`BUILTIN`'s ``"default"`` entry with no retry hint.
    """
    detector = _detector_of(anomaly)
    try:
        remote = _get_remote()
        local = _local_profile()
        remote_fields = _profile_fields(remote, detector)
        local_fields = _profile_fields(local, detector)
        builtin_fields = _profile_fields(BUILTIN, detector)

        status = remote_fields.get(
            "status", local_fields.get("status", builtin_fields.get("status", 429))
        )
        template = remote_fields.get(
            "message",
            local_fields.get("message", builtin_fields.get("message", BUILTIN["default"]["message"])),
        )
        if remote_fields:
            source = "plane"
        elif local_fields:
            source = "local"
        else:
            source = "default"

        retry_after_s = _retry_after_s(anomaly)
        message = _safe_format(str(template), detector=detector, retry_after_s=retry_after_s)
        return Refusal(
            status=int(status),
            message=message,
            detector=detector,
            retry_after_s=retry_after_s,
            source=source,
        )
    except Exception:
        _LOG.debug("runbound could not resolve a refusal for %r; using BUILTIN", detector, exc_info=True)
        return _builtin_refusal(detector)


def _builtin_refusal(detector: str) -> Refusal:
    """The answer :func:`refusal_for` gives when resolution itself breaks."""
    entry = BUILTIN.get(detector, BUILTIN["default"])
    message = _safe_format(str(entry["message"]), detector=detector, retry_after_s=None)
    return Refusal(
        status=int(entry["status"]),
        message=message,
        detector=detector,
        retry_after_s=None,
        source="default",
    )


def _detector_of(anomaly: Anomaly) -> str:
    detector = getattr(anomaly, "detector", None)
    return str(detector) if detector else "default"


# --- profile lookup ----------------------------------------------------------


def _entry(profile: dict | None, key: str) -> dict:
    """One key's entry out of a profile, or ``{}`` if there is none usable."""
    if not isinstance(profile, dict):
        return {}
    entry = profile.get(key)
    return entry if isinstance(entry, dict) else {}


def _profile_fields(profile: dict | None, detector: str) -> dict:
    """``{"status": ..., "message": ...}`` this profile supplies for ``detector``.

    Checks the detector's own entry first, then the profile's ``"default"``
    entry, field by field — an entry that sets only ``status`` does not block
    ``"default"``'s ``message`` from being used. Only well-formed values count:
    a status outside 200-599 or a message over 500 chars is treated as absent,
    the same as if the field had been omitted.
    """
    result: dict = {}
    for key in (detector, "default"):
        entry = _entry(profile, key)
        for field in ("status", "message"):
            if field in result:
                continue
            value = entry.get(field)
            if _valid_field(field, value):
                result[field] = value
    return result


def _valid_field(field: str, value) -> bool:
    if value is None:
        return False
    if field == "status":
        return isinstance(value, int) and not isinstance(value, bool) and 200 <= value <= 599
    if field == "message":
        return isinstance(value, str) and len(value) <= _MAX_MESSAGE_CHARS
    return False


def _local_profile() -> dict | None:
    """The active ``GuardrailConfig.refusals``, or ``None`` before ``init()``."""
    try:
        from . import api

        with api._LOCK:
            engine = api._ENGINE
        if engine is None:
            return None
        refusals = getattr(engine.config, "refusals", None)
        return refusals if isinstance(refusals, dict) else None
    except Exception:
        _LOG.debug("runbound could not read the local refusal profile", exc_info=True)
        return None


# --- message formatting -------------------------------------------------------


class _SafeDict(dict):
    """A format mapping where a missing key formats as its own placeholder."""

    def __missing__(self, key):
        return "{" + key + "}"


def _safe_format(template: str, *, detector: str, retry_after_s: float | None) -> str:
    """``template.format(...)``, tolerant of anything a customer might type.

    An unknown placeholder (``{whatever}``) is left in the output verbatim
    rather than raising ``KeyError``, and a template that is not even valid
    ``str.format`` syntax (a stray ``{``) is returned unchanged rather than
    raising at all — the golden rule applies to a customer's own copy too.
    """
    try:
        return template.format_map(_SafeDict(detector=detector, retry_after_s=retry_after_s))
    except Exception:
        return template


# --- retry_after_s ------------------------------------------------------------


def _retry_after_s(anomaly: Anomaly) -> float | None:
    """Seconds left on the latch/cooldown this anomaly's session is under.

    ``None`` when nothing is known: the anomaly names no session (a circuit
    or in-flight-cap refusal, which are per-provider, not per-session), the
    session cannot be found any more (evicted, or this is a different worker
    than the one that latched it), the session is not actually latched, or
    the latch has no expiry at all (a permanent latch — ``latch_ttl_seconds``
    unset and no cooldown override). Never raises.
    """
    try:
        details = anomaly.details if isinstance(anomaly.details, dict) else {}
        key = details.get("key")
        session_id = details.get("session_id")
        if not key and not session_id:
            return None
        engine, state = _find_session(key, session_id)
        if engine is None or state is None:
            return None
        with state.lock:
            if state.tripped_by is None or state.tripped_at is None:
                return None
            ttl = state.latch_ttl_override
            if ttl is None:
                ttl = engine.config.latch_ttl_seconds
            tripped_at = state.tripped_at
        if ttl is None:
            return None
        remaining = float(ttl) - (time.monotonic() - tripped_at)
        return max(0.0, remaining)
    except Exception:
        _LOG.debug("runbound could not resolve retry_after_s", exc_info=True)
        return None


def _find_session(key, session_id):
    """``(engine, state)`` for the session ``key``/``session_id`` names.

    ``key`` is tried first — an exact, O(1) registry lookup — and is what a
    door refusal for *this* worker's own latch always has right. The
    ``session_id`` scan is the fallback for anomalies with no raw key (a halt
    anomaly carries only a hashed ``key_hash``) or for the default,
    unkeyed session. A remote latch relayed from another worker may carry a
    ``session_id`` that names a session on a *different* process; that scan
    then finds nothing here, and the caller answers ``None`` rather than a
    wrong number — the golden rule again.
    """
    from . import api

    with api._LOCK:
        engine = api._ENGINE
        if key:
            state = api._REGISTRY.get(key)
            if state is not None:
                return engine, state
        candidates = list(api._REGISTRY.values())
        default_session = api._SESSION
    if default_session is not None:
        candidates.append(default_session)
    if session_id:
        for candidate in candidates:
            if getattr(candidate, "session_id", None) == session_id:
                return engine, candidate
    return engine, None
