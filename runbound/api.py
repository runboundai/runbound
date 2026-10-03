"""The public API: ``init``, ``reset``, ``session``, ``tool``, ``wrap``,
``llm``, ``record_call``, ``current_session``, ``is_tripped``,
``session_status``, ``tool_calls``, ``clear``, ``circuit_state``,
``inflight_calls``, ``active_sessions``.

This module owns runbound's one documented global: the engine and session
created by :func:`init`. Agents are threaded, so every read or mutation of that
global happens under ``_LOCK`` (held only for pointer swaps and registry
bookkeeping — never across detector or user code).

Work done inside a :func:`session` block is accounted to that key's own
:class:`SessionState` instead, tracked in a context variable so threads and
asyncio tasks each see their own without cooperating.

Two rules hold everywhere below:

* **Inert until initialized.** Without :func:`init`, decorated tools and
  wrapped clients run exactly as if runbound were not installed.
* **Fail-open.** Anything that goes wrong while observing is logged to the
  ``"runbound"`` logger and swallowed. The one deliberate exception is
  :class:`~runbound.exceptions.GuardrailTripped`.
"""

from dataclasses import asdict, dataclass, replace
import asyncio
import contextlib
import contextvars
import functools
import hashlib
import inspect
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Sequence
from typing import Any
from uuid import uuid4

from . import _coverage, autowrap, ladder, local_events, responses
from .config import GuardrailConfig
from .engine import (
    BUDGET_DETECTOR,
    ERROR_MAX_CHARS,
    HALT_DETECTOR,
    INFLIGHT_DETECTOR,
    SPIKE_DETECTOR,
    Engine,
    _latched,
    _now,
    provider_host,
    take_pending_delay as _engine_take_pending_delay,
)
from .events import Anomaly, Decision, Event
from .exceptions import GuardrailTripped
from .ladder import Effect, Transition
from .plane_types import DETAIL_STRING_MAX, ExitDelta, PlaneStatus, key_hash, redact_key
from .policy import CAPABILITIES, ToolCall, ToolRules, normalize_effects
from .pricing import price_call
from .shared import LocalState, build as _build_shared
from .state import BUDGET_WINDOW_AGE_WARN_SECONDS, Hold, SessionState
from .wrappers import PROVIDERS

_LOG = logging.getLogger("runbound")

#: Error strings are truncated before they are stored on an event; the limit
#: lives with the engine, which builds the events for failed model calls.
_SESSION_ID_CHARS = 12
_KEYED_ID_CHARS = 10

_LOCK = threading.Lock()
_ENGINE: Engine | None = None
_SESSION: SessionState | None = None

#: Mirrors the live engine's ``config.loop_ignore_tools``, written only
#: alongside ``_ENGINE`` (:func:`init`, :func:`_teardown_for_tests`) so
#: ``_is_loop_ignored`` can short-circuit the overwhelmingly common case —
#: nothing configured — without taking ``_LOCK`` on every tool call. A tuple
#: reassignment is atomic, so an unlocked read here only ever sees a complete
#: old or new value, never a partial one.
_LOOP_IGNORE_TOOLS: tuple[str, ...] = ()

#: What the rest of the fleet knows. :class:`~runbound.shared.LocalState`
#: until an :func:`init` with a ``control_plane_url`` replaces it, and again
#: after :func:`_teardown_for_tests` — so every read below is safe before
#: :func:`init` and needs no ``None`` check. Swapped under ``_LOCK``; its own
#: methods are called outside it, because they may touch the network.
_SHARED = LocalState()

#: Per-session totals as of that session's last reported exit, keyed by
#: session id: ``(seq, spend_usd, tokens, steps, tool_calls, events, errors,
#: tokens_cached)``. What makes an exit a *delta*
#: — two workers on one key each report their own share and the plane adds
#: them up. Read and written under ``_LOCK``, emptied with the registry, and
#: capped so a process churning through keys cannot grow it without bound.
_EXITS: "OrderedDict[str, tuple]" = OrderedDict()
_EXIT_MEMO_MAX = 4096

#: When the "the fleet is halted" warning was last logged, under
#: ``on_halt="warn"``. One line a minute, however many blocks are refused.
_HALT_WARNED_AT: float | None = None

#: How long between those warnings.
_HALT_WARN_INTERVAL_S = 60.0

#: The same one-line-a-minute throttle as ``_HALT_WARNED_AT``, for a
#: Narrow halt this worker's own ``on_halt="warn"`` is choosing not to apply.
_NARROW_WARNED_AT: float | None = None

#: Keyed sessions, most-recently-used last; capped at ``config.max_sessions``.
_REGISTRY: "OrderedDict[str, SessionState]" = OrderedDict()

#: How many times each key has been cleared. Part of that key's session id, so
#: :func:`clear` still starts a session the detectors have never seen. Read and
#: written under ``_LOCK``; emptied whenever the registry is, since a fresh
#: process starts every key at generation 0.
_GENERATIONS: dict[str, int] = {}

#: How many times each key has been rolled over by the abuse ladder. Kept
#: beside the registry rather than only on the session, so that evicting a
#: repeat offender's session — or the session it was rolled over into — does
#: not hand them a clean slate. Read and written under ``_LOCK``, and emptied
#: with the registry: strikes are forgiven by :func:`clear`, :func:`reset` and
#: a new process, exactly like the sessions they belong to.
_STRIKES: dict[str, int] = {}

#: Keyed :func:`session` blocks entered and not yet exited, process-wide —
#: what ``max_active_sessions`` is measured against and what
#: :func:`active_sessions` reports. The default session is not counted: it is
#: the process, not one of the things running inside it. Read and written
#: under ``_LOCK``, and put back to zero by :func:`init` and :func:`reset`.
_ACTIVE = 0

#: How many :func:`session` blocks this worker has refused at the door for
#: each key, because that key was latched. The local half of the ledger the
#: control plane keeps fleet-wide: what a latch is *saving* is the blocks it
#: turns away, and a customer asking "is it working?" is asking this number.
#: Read and written under ``_LOCK``, forgiven by :func:`clear`, and emptied
#: with the registry.
_DOOR_REFUSALS: dict[str, int] = {}

#: ``(session_id, rule)`` pairs a fan-out refusal has already been alerted on.
#: A refused caller (or a retrying agent) walks into the same wall on every
#: attempt, and the on-call wants to hear about the wall once. Emptied with the
#: registry, under ``_LOCK``.

#: The detector name a fan-out refusal is reported under. It is not a detector
#: in the ``check(state, event, config)`` sense — nothing has happened yet when
#: it fires — but to everything downstream (alerts, dedup, ``GuardrailTripped``)
#: it reads exactly like one.
FANOUT_DETECTOR = "fanout"

#: Calls in flight right now per provider label, process-wide — what
#: ``max_inflight_calls`` is measured against and what :func:`inflight_calls`
#: reports. One entry per label a guarded call has ever been made to; read and
#: written under ``_LOCK``, and emptied by :func:`init` and :func:`reset`.
_INFLIGHT: dict[str, int] = {}

#: Set once the process has been told that auto_wrap found no provider SDK.
#: Once is enough: the answer cannot change between two ``init()`` calls in the
#: same process unless an import happened in between, and that case announces
#: itself by patching something.
_NO_PROVIDER_ANNOUNCED = False

#: The session of the innermost enclosing :func:`session` block, per
#: thread and per asyncio task. ``None`` means "use the default session".
_CURRENT: contextvars.ContextVar[SessionState | None] = contextvars.ContextVar(
    "runbound_session", default=None
)

#: Mixed into every :func:`_args_hash` digest. Generated once at import and
#: kept across :func:`reset` (never regenerated for the life of the process),
#: so a hash is a stable equality token within one process — the same call
#: hashes the same way all run long — without being a fingerprint that means
#: anything outside it: two processes salt differently, and an argument value
#: cannot be recovered or matched against by anyone who only sees the digest.
_HASH_SALT: bytes = secrets.token_bytes(16)


# --- lifecycle --------------------------------------------------------------


#: Fields ``init()`` used to accept for outbound delivery, retired now
#: that the SDK does not send alerts at all — the control plane routes
#: and delivers, gated by plan, with adapters (Slack, PagerDuty, Opsgenie,
#: the signed webhook) richer than anything a truthiness check inside a
#: customer's own process could enforce. Named individually, each with one
#: sentence of where the setting actually lives now, so a caller upgrading
#: from an older runbound gets a kind ``ValueError`` instead of a cryptic
#: ``TypeError: unexpected keyword``.
_RETIRED_DELIVERY_FIELDS = {
    "slack_webhook": "delivery is an alert route on your runbound dashboard now",
    "pagerduty_routing_key": "delivery is an alert route on your runbound dashboard now",
    "webhook_url": "delivery is an alert route on your runbound dashboard now",
    "webhook_secret": "delivery is an alert route on your runbound dashboard now",
    "link_template": "link_template is a per-service field on your runbound dashboard now",
}


def _reject_retired_delivery_fields(kwargs: dict) -> None:
    """Raise a kind ``ValueError`` for any retired ``init()`` keyword.

    Checked before ``GuardrailConfig(**kwargs)`` even runs, so a caller who
    upgraded from an older runbound and kept, say, ``slack_webhook=...``
    around hears exactly where that setting went rather than a bare
    ``TypeError: unexpected keyword`` from the dataclass. No code here could
    ever send anything — it only rejects.
    """
    for name in _RETIRED_DELIVERY_FIELDS:
        if name in kwargs:
            raise ValueError(f"{name} is no longer a setting: {_RETIRED_DELIVERY_FIELDS[name]}")


def init(**kwargs: Any) -> None:
    """Configure runbound and start a session. Call once, at startup.

    Accepts every :class:`~runbound.config.GuardrailConfig` field as a keyword
    argument. Configuration errors raise ``ValueError`` (and unknown options
    ``TypeError``) on purpose: a misconfigured SDK must fail loudly here
    rather than quietly fail to guard anything in production. The five
    fields retired for delivery (``slack_webhook``, ``pagerduty_routing_key``,
    ``webhook_url``, ``webhook_secret``, ``link_template``) raise a
    ``ValueError`` naming where the setting went instead of that bare
    ``TypeError`` — delivery is not a setting on this call any more at all.
    Every local deterministic control — ``postures``, ``capabilities``, ``budget_soft``,
    ``on_budget_soft``, ``max_actions_per_run``, the eight circuit-rate
    knobs, the three loop-shape knobs, and the eleven spike/ladder knobs
    (``spike_detection``, ``on_spike`` and the nine tuning knobs under it)
    — is a real ``init()`` keyword again, free and local, with no account and
    no plane required. A control plane, when connected, can only tighten
    what the code already configured (or configure one from nothing) —
    never loosen it; see :mod:`runbound.controls_merge`.

    ``require_rules=True`` adds one more: every ``@runbound.tool`` already
    imported that states no rule is named in a ``ValueError`` here, and every
    one declared afterwards raises at its own decoration — the CI gate, since
    in a normal module the tools are defined below this call, not above it.

    Calling it again reconfigures the SDK and starts a fresh session and an
    empty keyed-session registry.

    Unless ``auto_wrap=False``, this is also where the provider SDKs are
    instrumented: ``openai`` and ``anthropic``, if they are importable, are
    patched at class level, so a client built anywhere in the process — by a
    framework, or by code written before runbound was installed — is guarded
    without a :func:`wrap` call. What was patched is logged at INFO, and
    :func:`coverage` reports it afterwards.

    With a ``control_plane_url`` this is also where fleet mode starts: a
    client, the telemetry exporter (unless ``export_events=False``) and a
    heartbeat thread. None of that is on the request path, none of it can fail
    an ``init()``, and a second ``init()`` stops the first one's threads.

    ``token`` is the credential from your runbound dashboard, and setting
    it is enough to turn fleet mode on by itself: a bare ``token`` with no
    ``control_plane_url`` points fleet mode at the hosted plane. What it does
    *not* do any more is send anything — this call still detects, stops,
    refuses and reports (to observers, and to the plane once one is
    connected) with no token at all; routing an anomaly on to Slack,
    PagerDuty or a webhook of your own is an alert route you configure on
    your runbound dashboard, not a keyword here.
    """
    _reject_retired_delivery_fields(kwargs)
    global _ENGINE, _SHARED, _LOOP_IGNORE_TOOLS
    config = GuardrailConfig(**kwargs)
    config.validate()
    local_events.configure(config.on_event)
    _check_required_rules(config)
    _coverage.warn_if_cannot_stop(config.on_anomaly in ("raise", "callback"))
    # "Which plane am I talking to" is the first question in every support
    # conversation, and a customer should not have to read our source to
    # answer it. Said once, at INFO, naming the mode and — for a self-hosted
    # plane — the url. Never the token.
    if config.plane_mode == "hosted":
        _LOG.info("[runbound] control plane: hosted")
    elif config.plane_mode == "self_hosted":
        _LOG.info("[runbound] control plane: self-hosted at %s", config.control_plane_url)
    # A plane profile learned under a previous init() (a different service, or
    # no plane at all) must not leak into this configuration; the poller
    # re-fetches it within poll_s if this config has a plane of its own.
    responses.set_remote(None)
    shared = _build_shared(config)
    engine = Engine(config, observers=shared.observers(), shared=shared)
    with _LOCK:
        _ENGINE = engine
        _LOOP_IGNORE_TOOLS = config.loop_ignore_tools
        previous, _SHARED = _SHARED, shared
        _forget_sessions()
        _start_session(engine._effective_config())
    # Outside the lock on purpose: importing somebody else's SDK, joining a
    # background thread and saying hello to a control plane are none of them
    # things to do while holding the lock every guarded call needs.
    previous.stop()
    shared.start(engine)
    _auto_wrap(config)
    _start_coverage_timer(config)


def reset() -> None:
    """Start a fresh session, keeping the current configuration and engine.

    Use it between agent runs in a long-lived process: counters go back to
    zero, every keyed session is forgotten, and detectors, which fire once per
    session, arm again. Also clears any plane refusal profile this worker had
    cached (:mod:`runbound.responses`) — the poller re-fetches it within
    ``poll_s`` if the plane still has one — so a test suite that calls
    :func:`reset` between cases never leaks one into the next. Otherwise a
    no-op if :func:`init` was never called.
    """
    responses.set_remote(None)
    with _LOCK:
        if _ENGINE is None:
            return
        _forget_sessions()
        _start_session(_ENGINE._effective_config())


def current_session() -> SessionState | None:
    """The session work is being accounted to right now.

    Inside a :func:`session` block that is the block's keyed session;
    otherwise the default session created by :func:`init`, or ``None`` before
    it.
    """
    keyed = _CURRENT.get()
    if keyed is not None:
        return keyed
    with _LOCK:
        return _SESSION


def _new_session(
    config: GuardrailConfig, key: str | None = None, tags: dict | None = None
) -> SessionState:
    """A session sized by the configuration, identified by its key.

    A keyed session's id is derived from the key, so every worker running the
    same code derives the same id for the same key: an incident that
    happens on eight replicas dedups into one alert instead of eight. The
    default session, which nobody else can name, keeps a random id. Callers
    hold ``_LOCK`` — the generation and strike counters are read under it.

    A key that has been rolled over by the abuse ladder starts its next
    session already carrying its strikes, and with the allowance a limited
    session gets halved once per strike (never below one): the second chance
    is real, but shorter than the first.
    """
    session_id = _keyed_id(key) if key is not None else uuid4().hex[:_SESSION_ID_CHARS]
    strikes = _STRIKES.get(key, 0) if key is not None else 0
    base = max(1, config.spike_limit_calls // 2**strikes) if strikes else None
    return SessionState(
        session_id,
        loop_window=config.loop_window,
        spike_window=config.spike_window,
        key=key,
        tags=tags,
        strikes=strikes,
        spike_allowance_base=base,
    )


def _keyed_id(key: str) -> str:
    """``<digest of the key>-<generation>``: stable per key, new after a clear.

    The generation is what keeps the two promises apart: the digest makes the
    id the same everywhere, and clearing a key changes the generation so its
    next session is one the fire-once detectors have never reported on.
    """
    digest = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()
    return f"{digest[:_KEYED_ID_CHARS]}-{_GENERATIONS.get(key, 0)}"


def _forget_sessions() -> None:
    """Drop every keyed session and the bookkeeping around it. Holds ``_LOCK``.

    Everything a keyed session accumulated goes at once — its state, its
    generation and strikes, the fan-out alerts it earned, and the counts of
    blocks and calls that were in flight when the world was restarted — so
    that :func:`init` and :func:`reset` really do start from nothing.
    """
    global _ACTIVE
    _REGISTRY.clear()
    _GENERATIONS.clear()
    _STRIKES.clear()
    _DOOR_REFUSALS.clear()
    _INFLIGHT.clear()
    _EXITS.clear()
    _ACTIVE = 0


def _start_session(config: GuardrailConfig) -> None:
    """Replace the default session. Caller must hold ``_LOCK``."""
    global _SESSION
    _SESSION = _new_session(config)


def _teardown_for_tests() -> None:
    """Return the module to its uninitialized state. Test-only.

    Also takes the class-level patches back off the provider SDKs, disarms the
    coverage timer, stops the control plane's threads and clears any plane
    refusal profile (:mod:`runbound.responses`): those all outlive an
    engine, and a test that left them behind would guard — or warn about, or
    export, or refuse-with — the next test's traffic.
    """
    global _ENGINE, _SESSION, _NO_PROVIDER_ANNOUNCED, _SHARED, _HALT_WARNED_AT
    global _LOOP_IGNORE_TOOLS, _NARROW_WARNED_AT
    with _LOCK:
        _ENGINE = None
        _SESSION = None
        _LOOP_IGNORE_TOOLS = ()
        previous, _SHARED = _SHARED, LocalState()
        _HALT_WARNED_AT = None
        _NARROW_WARNED_AT = None
        _forget_sessions()
    previous.stop()
    _CURRENT.set(None)
    _NO_PROVIDER_ANNOUNCED = False
    autowrap.unpatch_all()
    _coverage.reset_for_tests()
    local_events.clear_for_tests()
    responses.set_remote(None)


# --- auto-instrumentation and the coverage report ---------------------------


def _auto_wrap(config: GuardrailConfig) -> None:
    """Patch the provider SDKs themselves, and say once what that covered.

    Silent when there was nothing new to do — a second :func:`init` in the same
    process patches nothing and claims nothing. Fail-open: auto-instrumentation
    that goes wrong is logged and leaves the host exactly as it was, with
    :func:`wrap` still available by hand.
    """
    global _NO_PROVIDER_ANNOUNCED
    if not config.auto_wrap:
        return
    try:
        labels = autowrap.patch_all(_record_llm_call, _HOOKS)
        already = autowrap.patched()
    except Exception:
        _LOG.warning(
            "runbound could not auto-instrument the provider SDKs; "
            "wrap(client) still works",
            exc_info=True,
        )
        return
    if labels:
        _LOG.info("runbound: auto-instrumented %s", _instrumented(labels))
        return
    if already or _NO_PROVIDER_ANNOUNCED:
        return
    _NO_PROVIDER_ANNOUNCED = True
    _LOG.info(
        "runbound: auto_wrap: no supported provider SDK found — call "
        "runbound.wrap(client) or use @runbound.llm"
    )


def _instrumented(labels: "list[str]") -> str:
    """``"openai (chat, responses) and anthropic (messages)"`` from the labels.

    Grouped by provider because that is how a reader checks it: the question
    behind this line is "is my provider covered", not "how many methods".
    """
    grouped: "OrderedDict[str, list[str]]" = OrderedDict()
    for label in labels:
        provider, _, surface = label.partition(":")
        grouped.setdefault(provider, []).append(surface or provider)
    parts = [f"{provider} ({', '.join(surfaces)})" for provider, surfaces in grouped.items()]
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _coverage_settings() -> "tuple[bool, float]":
    """``(auto_wrap, seconds)`` for the coverage report, defaults before init.

    The seconds are only ever quoted in a message, so a check that is switched
    off (``coverage_check_seconds=None``) still quotes the default window
    rather than the word ``None``.
    """
    with _LOCK:
        engine = _ENGINE
    if engine is None:
        return True, _coverage.DEFAULT_CHECK_SECONDS
    seconds = engine.config.coverage_check_seconds
    return (
        bool(engine.config.auto_wrap),
        _coverage.DEFAULT_CHECK_SECONDS if seconds is None else float(seconds),
    )


def _start_coverage_timer(config: GuardrailConfig) -> None:
    """Arm the one-shot silent-zero check, replacing the previous one."""
    _coverage.cancel_silence_timer()
    if config.coverage_check_seconds is None:
        return
    _coverage.start_silence_timer(config.coverage_check_seconds, _warn_if_silent)


def _warn_if_silent() -> None:
    """Say at WARNING that a provider SDK is imported and nothing is guarded.

    Runs on the timer's own daemon thread, so it swallows everything: a
    diagnostic must never be the thing that kills a host's process.
    """
    try:
        message = _coverage.silence_warning(autowrap.patched(), **_coverage_kwargs())
        if message is not None:
            _LOG.warning("%s", message)
    except Exception:
        _LOG.debug("runbound: the coverage check could not run", exc_info=True)


def _coverage_kwargs() -> dict:
    """The two settings every coverage message is phrased with."""
    auto_wrap, seconds = _coverage_settings()
    return {"auto_wrap": auto_wrap, "seconds": seconds}


def coverage() -> dict:
    """What runbound can actually see right now, as plain numbers.

    ``init()`` computes nothing on its own: the sensors are :func:`wrap` (and
    the class-level ``auto_wrap``), ``@runbound.tool``, :func:`session` and
    :func:`record_call`. This is how a customer checks that at least one of
    them is wired, in a startup log, a health endpoint or a test::

        {"auto_wrapped": ["openai:chat", ...],  # classes patched at init()
         "wrapped_clients": 1,                  # wrap() calls that patched
         "decorated_tools": 3,                  # @runbound.tool decorations
         "guarded_calls": 128,                  # model calls runbound saw
         "tool_calls_seen": 41,                 # decorated tool calls attempted
         "keyed_sessions_seen": 12,             # keys registered
         "providers_imported": ["openai"],      # provider SDKs in sys.modules
         "providers_unguarded": [],             # ...that nothing has covered
         "last_guarded_call_age_s": 0.4,        # None if there has been none
         "warnings": [],                        # the silent-zero text, if any
         "refusals": "default",                 # highest refusal source in effect
         "fleet": "local protection active; fleet coordination: not connected",
         "fleet_pending": None}                 # events queued to post; None with no plane

    Counters are process-lifetime: :func:`init` and :func:`reset` do not clear
    them, because "has this process ever seen traffic?" is not a question a new
    session re-asks. Works before :func:`init` — everything reads zero — and
    fails open to zeros rather than raising.

    ``"refusals"`` is the odd one out — not a counter, but the highest of
    :mod:`runbound.responses`'s three sources currently answering a
    :class:`~runbound.exceptions.GuardrailTripped`'s ``.refusal``:
    ``"plane"`` when a control-plane profile is in effect, else ``"local"``
    when ``GuardrailConfig.refusals`` was set, else ``"default"`` (BUILTIN).

    ``"fleet"``: the one nudge — ``"connected"`` while a control plane is
    answering every path well, ``"connected; N% of entries in the last
    minute were decided locally (plane timeouts)"`` while it answers every
    heartbeat but is missing the entry timeout on the hot path, else the
    honest ``"local protection active; fleet coordination: not connected"``.
    Every control this SDK enforces is already free and already running
    locally (see :mod:`runbound.config`); this key says only whether it is
    coordinated across a fleet, never whether it is "unlocked".

    ``"fleet_pending"``: how many state/telemetry records this worker still
    has queued to post to the plane, or ``None`` with no plane configured —
    reported the same fail-open, best-effort way as ``"fleet"`` itself.
    """
    try:
        report = _coverage.snapshot(autowrap.patched(), **_coverage_kwargs())
        report["refusals"] = _refusals_source()
        report["safe_mode"] = _safe_mode_report()
        report["posture"] = posture()
        report["can_stop"] = _can_stop()
        report["fleet"] = _fleet_report()
        report["fleet_pending"] = _fleet_pending()
        return report
    except Exception:
        _LOG.warning("runbound could not read its own coverage", exc_info=True)
        zeros = _coverage.zeros()
        zeros["refusals"] = "default"
        zeros["can_stop"] = None
        zeros["fleet"] = _NO_FLEET
        zeros["fleet_pending"] = None
        return zeros


#: coverage()["fleet"]'s honest answer with no plane connected — never a
#: reason to think any control is weaker, only that it is not coordinated
#: across a fleet ("no feature is made artificially worse in the SDK to
#: create a paywall").
_NO_FLEET = "local protection active; fleet coordination: not connected"


def _fleet_report() -> str:
    """``coverage()["fleet"]``: see :func:`coverage`."""
    try:
        with _LOCK:
            shared = _SHARED
        status = shared.status()
        if status.mode == "connected":
            return "connected"
        if status.mode == "degraded" and status.reason == "fleet state unavailable":
            return (
                "connected; fleet state unavailable: deciding locally, "
                "last halt and posture held"
            )
        if status.mode == "degraded" and status.reason == "entry timeouts":
            pct = round(status.entries_local_share * 100)
            return (
                f"connected; {pct}% of entries in the last minute were "
                "decided locally (plane timeouts)"
            )
        return _NO_FLEET
    except Exception:
        return _NO_FLEET


def _fleet_pending() -> int | None:
    """``coverage()["fleet_pending"]``: see :func:`coverage`."""
    try:
        with _LOCK:
            shared = _SHARED
        return shared.pending_events()
    except Exception:
        return None


def events(n: int = 100) -> list:
    """This process's own anomalies, refusals, posture transitions and
    runtime changes, most recent last (local telemetry is free).

    An in-memory ring, readable with no account, no token and no control
    plane — the free, local half of what a connected plane's own
    centralized event log charges for. Nothing here is written to disk, and
    no record ever carries a call argument, a prompt or a reply. Without a
    control plane nothing leaves the process; with one, posture transitions
    and runtime changes are also sent to it, the session key as a hash. Process-lifetime like :func:`coverage`'s own
    counters: :func:`init` and :func:`reset` do not clear it. ``[]`` before
    :func:`init` or when nothing has happened yet. See also
    :func:`decisions` and the optional ``on_event=`` callback on
    :func:`init`.
    """
    try:
        return local_events.events(n)
    except Exception:
        _LOG.warning("runbound could not read its own local events", exc_info=True)
        return []


def decisions(n: int = 100) -> list:
    """This process's own admission :class:`~runbound.events.Decision`
    bodies, most recent last (local telemetry is free) — every one
    stamped onto a refusal, the same numbers
    ``exc.decision`` carries. See :func:`events` for the same guarantees
    (in-memory, process-lifetime, no content ever)."""
    try:
        return local_events.decisions(n)
    except Exception:
        _LOG.warning("runbound could not read its own local decisions", exc_info=True)
        return []


def _can_stop() -> bool | None:
    """``coverage()["can_stop"]``: can this process's own
    ``on_anomaly`` actually stop a run? ``None`` before :func:`init`."""
    with _LOCK:
        engine = _ENGINE
    if engine is None:
        return None
    return engine.config.on_anomaly in ("raise", "callback")


@dataclass(frozen=True)
class BudgetView:
    """What a session's dollar budget looks like right now.

    ``spent`` is what the ``budget`` detector compares — this process's total
    plus what the fleet already spent under the key — so ``remaining`` is the
    honest answer to "how much is left", floored at ``0.0`` once the call that
    crossed the line has landed. ``soft_at`` is the dollar amount of
    ``budget_soft``, or ``None``. ``window`` and ``resets_at`` are ``None``
    until budgets have windows; ``scope`` is ``"session"`` until they
    have scopes.

    ``reserved`` is money held by calls in flight on *this* worker —
    ``session.reserved.get("usd", 0.0)`` — given back the moment each call
    closes out, however it ends. It is worker-local, never folded into
    ``spent`` (a hold is not spend; it is a promise this worker is about to
    spend) and never sent to the plane, so ``remaining`` is the honest answer
    to "how much can a *new* call actually reserve right now."
    """

    limit: float
    spent: float
    remaining: float
    soft_at: float | None
    window: str | None
    resets_at: float | None
    scope: str
    reserved: float


def budget(key: str | None = None) -> BudgetView | None:
    """How much of the dollar budget is left, for ``key``'s session or this one.

    ``None`` before :func:`init`, without ``budget_usd``, and for a key that
    has no session. Reads the same numbers the ``budget`` detector compares, so
    it agrees with :func:`session_status` to the cent. Never creates a session.
    """
    try:
        if key is None:
            engine, state = _active()
        else:
            with _LOCK:
                engine = _ENGINE
                state = _REGISTRY.get(key)
        if engine is None or state is None:
            return None
        return _budget_view(engine, state)
    except Exception:
        _LOG.warning("runbound could not read the budget", exc_info=True)
        return None


def _budget_view(engine: Any, state: SessionState) -> BudgetView | None:
    """The :class:`BudgetView` of ``state`` under ``engine``, or ``None`` with
    no budget. Reads ``engine._limit("budget_usd")`` when ``engine``
    has one — this worker's own ``budget_usd`` tightened against the
    plane's — so this agrees to the cent with what
    :meth:`~runbound.engine.Engine._admit_budget` actually compares an
    estimate against; a bare config-like object with no ``_limit`` falls
    back to its own ``budget_usd`` unchanged, for callers with no engine.

    ``soft_at``: the soft line is a real, local ``init()`` knob —
    ``engine._effective_budget_soft()`` reads the merged fraction (this
    worker's own value, tightened by the plane's Controls for this
    service), ``None`` with no plane or no engine at all.
    """
    config = getattr(engine, "config", engine)
    limiter = getattr(engine, "_limit", None)
    limit = limiter("budget_usd") if callable(limiter) else config.budget_usd
    if limit is None:
        return None
    window = getattr(config, "budget_window", None)
    state.roll_budget_window(window)
    with state.lock:
        spent = state.total_cost_usd + state.spend_offset_usd
        reserved = state.reserved.get("usd", 0.0)
    soft_getter = getattr(engine, "_effective_budget_soft", None)
    soft = soft_getter() if callable(soft_getter) else None
    return BudgetView(
        limit=limit,
        spent=spent,
        remaining=max(limit - spent - reserved, 0.0),
        soft_at=None if soft is None else limit * soft,
        window=window,
        resets_at=state.budget_window_resets_at(window),
        # scope stays "session": a run/key distinction for BudgetView itself
        # is a wider change (parked, out of this task's scope) than the one
        # this field already documents -- only Decision.level distinguishes
        # "run" from "key" today, on an actual refusal.
        scope="session",
        reserved=reserved,
    )


def _budget_dict(engine: Any, state: SessionState) -> dict | None:
    """``session_status()["budget"]``: the same view, as a plain dict."""
    view = _budget_view(engine, state)
    return None if view is None else asdict(view)


def _safe_mode_report() -> dict:
    """``coverage()["safe_mode"]``: the process's, the plane's, and how many sessions."""
    with _LOCK:
        engine = _ENGINE
        states = list(_REGISTRY.values())
        if _SESSION is not None:
            states.append(_SESSION)
    process = engine.process_posture() if engine is not None else None
    plane = engine.plane_posture() if engine is not None else None
    return {
        "process": process.as_dict() if process is not None else None,
        "plane": plane is not None,
        "sessions": sum(1 for state in states if getattr(state, "posture", None) is not None),
        "applies_to": _coverage.SAFE_MODE_APPLIES_TO,
    }


def tools() -> list[dict]:
    """The tool report this worker would send, exactly as the plane sees it.

    Every tool this process knows — one entry per ``@runbound.tool`` and one
    per tool name a model asked for that no decorator declared — sorted by
    name::

        [{"name": "issue_refund",
          "decorated": True,               # False = nothing guards it
          "params": [{"name": "user", "annotation": "str", "required": True}],
          "doc": "Refund a customer.",     # the first line, never more
          "module": "acme.tools"}]

    Built from the code at import time, so it cannot drift from it, and
    returned rather than printed: this is the thing to assert on in a test, and
    a REPL displays it by itself. Works before :func:`init`, costs nothing to
    call, and fails open to ``[]``.

    Names, annotations rendered as strings and one docstring line are the whole
    of what leaves the process. Never an argument, never a default value,
    never a return value.
    """
    return _coverage.tool_report()


def _refusals_source() -> str:
    """``"plane" | "local" | "default"`` — see :func:`coverage`. Never raises."""
    try:
        if responses.has_remote():
            return "plane"
        with _LOCK:
            engine = _ENGINE
        if engine is not None and engine.config.refusals:
            return "local"
    except Exception:
        _LOG.debug("runbound could not read the refusal source", exc_info=True)
    return "default"


def assert_guarded() -> None:
    """Raise ``RuntimeError`` if a provider SDK is imported and nothing is guarded.

    The startup-check form of the same sentence :func:`coverage` reports and
    the coverage timer logs — for a test, a CI gate, or the line after
    ``init()`` in a service that would rather not boot blind::

        runbound.init(budget_usd=5.0)
        runbound.assert_guarded()

    Passes when any guarded call has been recorded, and when no provider SDK is
    imported at all: there is no traffic to be blind to. Never raises anything
    else — a broken check is not a reason to refuse to start.
    """
    try:
        message = _coverage.silence_warning(autowrap.patched(), **_coverage_kwargs())
    except Exception:
        _LOG.warning("runbound could not check its own coverage", exc_info=True)
        return
    if message is not None:
        raise RuntimeError(message)


def unpatch() -> None:
    """Undo ``auto_wrap``'s class-level patches. Never raises.

    For tests and for the rare host that wants the provider SDKs back as they
    shipped without restarting the process. Clients patched by :func:`wrap`
    keep their own guards; :func:`reset` does not unpatch anything.
    """
    autowrap.unpatch_all()


# --- keyed sessions ---------------------------------------------------------


@contextlib.contextmanager
def session(key: str, tags: dict | None = None) -> Iterator[SessionState | None]:
    """Account everything inside this block to ``key``'s own session.

    ::

        with runbound.session(user_id, tags={"plan": "free"}):
            reply = chat(client, message)

    One key means one :class:`SessionState`, reused across blocks, so budgets
    and baselines survive from request to request and only the offending
    key is ever stopped. At most ``max_sessions`` keys are kept; the
    least recently used is dropped, and re-entering a dropped key simply
    starts it over.

    Blocks nest: the enclosing session is restored on exit. The binding is a
    context variable, so a thread or asyncio task that opens its own block is
    unaffected by any other. Yields the session, or ``None`` before
    :func:`init` — where the block is inert and observes nothing, exactly as
    the rest of the SDK is.

    Under ``on_anomaly="raise"``, entering the block for a key that has already
    tripped raises :class:`~runbound.exceptions.GuardrailTripped` *before the
    body runs* — a blocked key costs the business nothing from its next
    request on. :func:`clear` is how that key is let back in — or, with
    ``latch_ttl_seconds`` configured, simply waiting out the window.

    Entering is also where the fan-out limits are enforced. With
    ``max_session_depth``, ``max_child_sessions`` or ``max_active_sessions``
    set, a block that would take the run past one of them raises
    :class:`~runbound.exceptions.GuardrailTripped` before its body runs —
    whatever ``on_anomaly`` says, because those are numbers the customer
    stated, and latching nothing, because what was wrong is the shape of the
    run rather than this key.

    Entering is also where the abuse ladder (``on_spike="limit"``) rolls a key
    over: a session the ladder closed is retired here and the key continues in
    a fresh one, on a cooldown and with a tighter allowance. Nothing about
    that reaches the app — the block is the same block, and the refusal during
    the cooldown is the same ``GuardrailTripped`` as any other.

    In fleet mode (``control_plane_url``) entering is where this worker learns
    what the *rest* of the fleet has spent under this key, whether another
    worker has already stopped it, and whether the org has halted everything —
    and leaving is where this block's own spend is reported back. Both are
    bounded by ``control_plane_timeout_s`` and fail open: a plane that does not
    answer costs the block that timeout, once, and nothing else.
    """
    state = _enter_session(key, tags)
    if state is None:
        yield None
        return
    state = _rolled_over(key, state)
    # The run's clock starts now, not at this session's first-ever creation:
    # max_session_seconds measures the run, and this key's own history is
    # what max_session_lifetime_seconds is for. A run's own budget resets
    # the same way, for the same reason: a fresh run_budget_usd (and
    # run_max_total_tokens) every entry, independent of the key's own
    # cumulative budget_usd.
    state.run_started_at = time.monotonic()
    state.run_cost_usd = 0.0
    state.run_tokens = 0
    _warn_if_budget_window_missing(state)
    _sync_entry(key, state)
    _refuse_fanout(state)
    _refuse_if_tripped(state)
    _record_return_if_served(state)
    _count_entry()
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)
        _count_exit()
        _flush_refusal_summaries(state)
        _sync_exit(key, state)


def _enter_session(key: str, tags: dict | None) -> SessionState | None:
    """The state for ``key``, created or promoted in the registry.

    ``None`` before :func:`init`, and also if the lookup itself fails: a
    broken registry costs the caller its per-key accounting, never its block.
    """
    try:
        with _LOCK:
            engine = _ENGINE
            if engine is None:
                return None
            return _registered(key, tags, engine._effective_config())
    except Exception:
        _LOG.warning(
            "runbound could not open session %r; continuing unkeyed", key, exc_info=True
        )
        return None


def _registered(key: str, tags: dict | None, config: GuardrailConfig) -> SessionState:
    """Get-or-create ``key``'s state as most-recently-used. Holds ``_LOCK``.

    Tags given on a later entry are merged in, so a session picked up again
    can be labelled with what the new request knows.
    """
    state = _REGISTRY.get(key)
    if state is not None:
        _REGISTRY.move_to_end(key)
        if tags:
            state.tags.update(tags)
        return state

    state = _new_session(config, key=key, tags=tags)
    _coverage.session_started()
    _REGISTRY[key] = state
    while len(_REGISTRY) > config.max_sessions:
        _REGISTRY.popitem(last=False)
    return state


def _warn_if_budget_window_missing(state: SessionState) -> None:
    """A keyed key with a real budget and no window, alive over a day, warns once.

    A ``budget_usd`` with no ``budget_window`` accumulates for as long as the
    key is reused, which is the pre-existing, unbounded behavior and exactly
    right for plenty of customers (a key meant to track lifetime spend). It
    is also an easy configuration mistake for a key meant to represent a
    person or a tenant across months: this is the one nudge, once per
    session, never repeated and never enforced — nothing here refuses
    anything or changes what the budget does.
    """
    try:
        if getattr(state, "key", None) is None or state._budget_window_age_warned:
            return
        with _LOCK:
            engine = _ENGINE
        if engine is None:
            return
        config = engine.config
        if config.budget_usd is None or config.budget_window is not None:
            return
        age = time.monotonic() - state.started_at
        if age < BUDGET_WINDOW_AGE_WARN_SECONDS:
            return
        state._budget_window_age_warned = True
        _LOG.warning(
            "runbound: session %r has held a budget_usd of $%.4f with no "
            "budget_window for over 24h; spend keeps accumulating for as "
            "long as this key is reused. Set budget_window if that key is "
            "meant to track a person or a tenant over time rather than a "
            "single, permanent lifetime total.",
            state.key,
            config.budget_usd,
        )
    except Exception:
        _LOG.warning(
            "runbound could not check the budget window's age for %r",
            getattr(state, "key", None),
            exc_info=True,
        )


# --- the fleet: what the plane says at the door, and what we say leaving -----


def _sync_entry(key: str, state: SessionState) -> None:
    """Apply what the rest of the fleet knows about ``key`` to this session.

    One call to the plane, bounded by ``control_plane_timeout_s`` and made
    outside ``_LOCK``. What comes back is folded into the session — the fleet's
    spend and tokens as offsets the budget detector adds, the strikes this key
    has already earned, a latch another worker set — and then the halt is
    enforced.

    A no-op without a control plane, and fail-open with one: anything that goes
    wrong is logged and the block is entered on this worker's own numbers, the
    single-process behavior. The deliberate exception is
    :class:`~runbound.exceptions.GuardrailTripped` for a ``"stop"`` halt under
    ``on_halt="raise"``, which must reach the caller before the body runs. A
    ``"narrow"`` halt never refuses here at all — it states posture
    ``restricted`` instead (:meth:`~runbound.engine.Engine.halt_posture`),
    read the moment a decorated tool is judged, later in the same block.
    """
    try:
        with _LOCK:
            engine = _ENGINE
            shared = _SHARED
        if engine is None or not getattr(shared, "fleet", True):
            return
        decision = shared.enter(key, state, engine.config)
        refused = None
        if decision is not None:
            if getattr(decision, "allow", True):
                _apply_decision(key, state, decision)
                _apply_baseline(key, state, decision, engine._effective_config(), shared)
            else:
                # A refusal may carry the latch that caused it, and with it
                # real fleet facts (offsets, strikes, generation): adopt them
                # first, so ``_refuse_if_tripped`` refuses the block the way
                # it always has, ``origin="fleet"`` and all. But the refusal
                # stands on its own: a latch that did not take — expired ttl,
                # a shape this worker cannot read — must not turn the plane's
                # "no" into an admission.
                if getattr(decision, "latch", None):
                    _apply_decision(key, state, decision)
                refused = decision
        # An EntryDecision's own ``halt`` only ever means "stop" (it
        # never rides ``/enter`` for a Narrow — see RemoteState._absorb), so
        # it always refuses the door when true. The heartbeat's halt can be
        # either mode; ``mode_fn`` is read defensively (``getattr``, as every
        # other optional SharedState extra is) so a test double or an older
        # in-tree fake that only implements ``halted()`` keeps behaving as a
        # "stop" always did before this task.
        mode_fn = getattr(shared, "halt_mode", None)
        mode = mode_fn() if mode_fn is not None else None
        door_halted = (decision is not None and decision.halt) or (
            shared.halted() and mode != "narrow"
        )
        narrowed = mode == "narrow" and shared.halted()
    except Exception:
        _LOG.warning(
            "runbound could not sync session %r with the control plane; "
            "continuing on local state",
            key,
            exc_info=True,
        )
        return
    # Both raises live outside the try above on purpose: a GuardrailTripped
    # caught by that except would turn a refusal into an admission.
    if refused is not None:
        with state.lock:
            already_latched = state.tripped_by is not None
        if not already_latched:
            _refuse_plane_decision(engine, state, refused)
        # else: the adopted latch refuses it in _refuse_if_tripped, as before.
        return
    if door_halted:
        _refuse_halted(engine, state)
        return
    if narrowed and engine.config.on_halt != "raise":
        # on_halt="warn": the same "say it, keep serving" contract a Stop
        # halt gets, applied to a Narrow one -- Engine.halt_posture() already
        # refuses to read the directive under this config, so nothing here
        # is actually narrowed; this only tells the operator so.
        _warn_narrowed(engine)


#: A boundary this worker can state for a refusal it did not compute itself
#: — decided by the plane, or by another worker and only relayed here as
#: data. Known detector names map to the boundary that kind of refusal
#: always carries when this worker judges one locally; a name outside this
#: table states no boundary at all — ``None`` — rather than guess one that
#: might be wrong: ``exc.reason`` still resolves correctly from the
#: detector itself (see ``runbound.exceptions._infer_reason``), it just does
#: not get the more specific boundary-driven answer.
_RELAYED_BOUNDARIES = {"budget": "money", "plane": "plane", "halt": "halt"}


#: What a relayed refusal says in words. A Decision has to read on its own
#: long after the moment has passed, so "no numbers reached this worker" is
#: never a reason to leave the sentence empty.
_RELAYED_REASONS = {
    "halt": "halted by the control plane",
    "plane": "the control plane could not be asked, and on_plane_loss is 'refuse'",
    "budget": "the organisation's budget, as decided by the control plane, is spent",
}


def _relayed_decision(
    detector: str, level: str, reason: "str | None" = None, **facts: Any
) -> Decision:
    """A :class:`~runbound.events.Decision` for a refusal this worker did not
    compute itself — the plane's own answer to an entry question, an org
    budget it decided, or a latch relayed from another worker. Unlike every
    admission stage, none of these ever has ``limit``/``used``/``reserved``/
    ``estimate``/``remaining`` to state: what reached this worker is a
    verdict and a detector name, not the numbers behind it, and inventing
    numbers to fill the shape would be less honest than an evaluation that
    says only what is actually known. ``provider_called`` is always
    ``False`` — every one of these is decided *before* any provider is
    reached, the plane's own atomic entry check or another worker's own
    door, never a call this process (or, so far as the relayed fact says,
    any process) has made yet.

    ``reason`` is never empty: a caller's own sentence, else the detector's
    entry in :data:`_RELAYED_REASONS`. ``facts`` are whatever did reach this
    worker alongside the verdict (a halt's mode, say) and go into
    ``evaluation`` as they are.
    """
    evaluation = {"provider_called": False, **facts}
    return Decision(
        verdict="deny",
        kind="entry",
        boundary=_RELAYED_BOUNDARIES.get(detector),
        level=level,
        reason=reason or _RELAYED_REASONS.get(detector, "refused by the control plane"),
        detector=detector,
        evaluation=evaluation,
    )


def _refuse_plane_decision(engine: Engine, state: SessionState, decision) -> None:
    """Refuse at the door when the plane's answer itself is a refusal.

    Two answers arrive this way: the org's daily budget is spent (detector
    ``budget``, ``origin="plane"``, ``rule="org_budget"``, decided by the
    plane's atomic entry script), and — under ``on_plane_loss="refuse"`` —
    the plane could not be asked at all (detector ``plane``). An earlier
    revision read only the fleet facts on a decision (offsets, strikes,
    latch) and never ``allow``, so a plane that said "no" was silently
    overruled by the worker — fixed here.

    Deliberately skips :func:`_apply_decision`: this is one answer to one
    entry question, not fleet state, so nothing here writes to ``state`` — no
    offset, no generation, no strike, no latch. A plane-loss refusal is never
    cached, so the next entry asks again; a real plane refusal sits in the
    entry cache like any decision, so entries inside the cache window are
    refused without another round trip. Not
    :func:`_remote_anomaly`, which describes a latch another worker made and
    stamps ``origin="fleet"`` on it; this is the plane's own refusal.

    Fail-open even here: a refusal we cannot describe is not enforced,
    because refusing a block with a broken exception would be worse than
    admitting one.
    """
    try:
        refusal = decision.refusal if isinstance(decision.refusal, dict) else {}
        details = refusal.get("details")
        details = dict(details) if isinstance(details, dict) else {}
        details.setdefault("origin", "plane")
        detector = str(refusal.get("detector") or "plane")
        built = _relayed_decision(detector, "fleet")
        key = getattr(state, "key", None)
        details["decision"] = built.as_dict()
        details["key_hash"] = None if key is None else key_hash(key)
        anomaly = Anomaly(
            detector=detector,
            severity=str(refusal.get("severity") or "critical"),
            message=str(refusal.get("message") or "Control plane refused this session"),
            details=details,
        )
        engine.notify_door(state, anomaly)
    except Exception:
        _LOG.warning("runbound could not apply a plane refusal", exc_info=True)
        return
    raise GuardrailTripped(anomaly)


def _apply_decision(key: str, state: SessionState, decision) -> None:
    """Fold one entry decision into this key's session. Never raises."""
    try:
        with state.lock:
            # The plane's totals include what this worker has already
            # reported, so its own spend is subtracted out — and the offset
            # only ever rises, because a decision served from the entry cache
            # is older than the local counters it is being compared against,
            # and lowering it there would hide this session's own spending.
            state.spend_offset_usd = max(
                state.spend_offset_usd,
                float(decision.fleet_spend_usd) - state.total_cost_usd,
                0.0,
            )
            state.tokens_offset = max(
                state.tokens_offset,
                int(decision.fleet_tokens) - state.total_tokens,
                0,
            )
            state.fleet_generation = int(decision.generation)
            strikes = max(int(getattr(state, "strikes", 0) or 0), int(decision.strikes))
            state.strikes = strikes
        with _LOCK:
            _STRIKES[key] = max(_STRIKES.get(key, 0), strikes)
        _apply_remote_latch(state, decision.latch)
    except Exception:
        _LOG.warning(
            "runbound could not apply the fleet decision for %r", key, exc_info=True
        )


def _apply_baseline(
    key: str, state: SessionState, decision, config: GuardrailConfig, shared
) -> None:
    """Seed the plane-delivered baseline, service median and ladder rung.

    This key's own restored baseline and rung come off ``decision`` — the
    per-key ``EntryDecision`` this entry already fetched — exactly like
    ``strikes``/``generation``/``latch`` do, because that is what they are:
    facts about the one key opening a session, not about the service. The
    service-wide median is a different shape (the "peer baseline") and
    comes off ``shared.service_baseline()`` instead — the Controls body a
    heartbeat delivers — because a per-key field has no home on a
    payload that is fetched once per *service* and shared across every key
    a worker serves.

    The service-wide median is advisory context — never, on its own, what a
    call is judged against — so it is refreshed on every entry regardless.
    This key's own restored baseline and rung are different: they change
    what the very next call is judged against and what rung it starts on,
    so they are only ever applied to a session that has made no local calls
    yet (:func:`_fresh_for_baseline`) — a freshly created ``SessionState``,
    which is exactly what a worker restarted mid-spike gets on its next
    entry for that key. A session already alive in this process (a second
    request for a key this worker has been serving all along) has long
    since moved past that, so a decision served from the entry cache — or a
    fresh one, for that matter — can never clobber what this worker has
    since learned or done to it. Never raises.
    """
    try:
        service = _shared_service_baseline(shared)
        with state.lock:
            if service is not None:
                state.service_baseline = service
            if not _fresh_for_baseline(state):
                return
            baseline = _decision_baseline(decision)
            if baseline is not None:
                state.spike_baseline = baseline
                state.spike_baseline_source = "restored"
                # Not learned by this worker -- never re-reported as if it
                # had measured this key's own calls itself.
                state.spike_baseline_samples = None
            # Restoring a rung is only meaningful once the ladder is
            # actually enabled -- nothing reads spike_level at all
            # otherwise. ``spike_enabled`` is read defensively: a resolved
            # config (Engine._effective_config) always carries the merged
            # value; a bare one (this function's own unit tests) falls
            # back to the real ``spike_detection`` field (default True),
            # the same fallback runbound.detectors.SpikeDetector
            # uses.
            spike_enabled = getattr(config, "spike_enabled", None)
            if spike_enabled is None:
                spike_enabled = config.spike_detection
            if spike_enabled and getattr(config, "on_spike", "notify") == "limit":
                _seed_rung(state, decision, config)
    except Exception:
        _LOG.warning(
            "runbound could not seed the restored baseline for %r", key, exc_info=True
        )


def _shared_service_baseline(shared) -> tuple[float, float] | None:
    """This service's median baseline, off whatever ``shared`` (a
    :class:`~runbound.shared.SharedState`) currently holds. ``None`` for
    anything that does not implement it (:class:`~runbound.shared.LocalState`,
    a bare test double) or raises — the same fail-open contract as every
    other optional ``shared`` read in this module.
    """
    method = getattr(shared, "service_baseline", None)
    if method is None:
        return None
    try:
        return method()
    except Exception:
        return None


def _fresh_for_baseline(state: SessionState) -> bool:
    """True only for a session that has not made a local call yet.

    Caller holds ``state.lock``. This is what stops a restored baseline or
    rung from ever overwriting what this worker's own process has already
    learned or done — a session's fresh, just-created ``SessionState`` is
    the only shape that satisfies it (alongside a genuinely brand-new key,
    for which applying either is a harmless no-op: there is nothing to
    restore that would differ from where it already sits).
    """
    return (
        state.spike_baseline is None
        and state.spike_level == 0
        and state.spike_allowance is None
    )


def _decision_baseline(decision) -> tuple[float, float] | None:
    """This key's own baseline off an entry decision, or ``None``.

    ``baseline_samples`` is the plane's own "is there really something here"
    flag: zero means no worker has ever reported this key's baseline, and a
    negative or unreadable value is treated the same way — fail open to
    "nothing to restore" rather than seed a session with junk.
    """
    try:
        samples = int(decision.baseline_samples)
    except (TypeError, ValueError, AttributeError):
        return None
    if samples <= 0:
        return None
    try:
        duration = float(decision.baseline_duration_s)
        output = float(decision.baseline_output_tokens)
    except (TypeError, ValueError):
        return None
    if max(duration, output) <= 0:
        return None
    return duration, output


def _seed_rung(state: SessionState, decision, config: GuardrailConfig) -> None:
    """Restore the ladder's own rung: the level and the allowance —
    the two facts about a limited session that ``strikes``/``generation``
    do not already carry across a restart. Caller holds ``state.lock``.

    Only ``LEVEL_WATCHING`` and ``LEVEL_LIMITED`` are ever restored. A
    ``LEVEL_CLOSED`` key's cooldown is already served by the remote latch
    (:func:`_apply_remote_latch`, applied separately from the same
    decision); a rolled-over key's fresh session always starts at 0
    regardless of what came before. Entering ``LEVEL_LIMITED`` also narrows
    the posture the ladder would have narrowed it to, with the ladder named
    as the source, exactly as :meth:`~runbound.detectors.SpikeDetector._climb`
    does the first time it sets it — a restart must restore what the ladder
    did, never merely its number.
    """
    try:
        level = int(decision.rung_level)
    except (TypeError, ValueError, AttributeError):
        return
    if level not in (ladder.LEVEL_WATCHING, ladder.LEVEL_LIMITED):
        return
    state.spike_level = level
    if level == ladder.LEVEL_LIMITED:
        allowance = getattr(decision, "rung_allowance", None)
        start = getattr(decision, "rung_allowance_start", None)
        default = max(1, int(getattr(state, "spike_allowance_base", None) or config.spike_limit_calls))
        state.spike_allowance = int(allowance) if allowance is not None else default
        state.spike_allowance_start = int(start) if start is not None else state.spike_allowance
        enter = getattr(state, "_enter_posture", None)
        if enter is not None:
            enter(
                "restricted",
                "restored: session limited before restart",
                source="ladder",
                level=ladder.LEVEL_NAMES[ladder.LEVEL_LIMITED],
            )
    state.record_ladder_transition(0, level, "restored")


def _apply_remote_latch(state: SessionState, latch) -> None:
    """Stop this session on a trip another worker made, if it is still running.

    The remote anomaly is latched exactly as a local one would be, with what
    is left of its ttl as this session's own expiry — so ``_refuse_if_tripped``
    refuses the block, ``is_tripped()`` reports the real reason, and the
    key is let back in when the fleet's cooldown runs out rather than when
    this worker happened to hear about it. A latch with nothing left on it is
    not applied at all.
    """
    anomaly = _remote_anomaly(latch, state)
    if anomaly is None:
        return
    ttl = latch.get("ttl_remaining_s")
    ttl = None if ttl is None else float(ttl)
    if ttl is not None and ttl <= 0:
        return
    with state.lock:
        if state.tripped_by is not None:
            return
        state.tripped_by = anomaly
        state.tripped_at = _now()
        state.latch_ttl_override = ttl
        _mark_relayed_rollover(state, anomaly, ttl)


def _mark_relayed_rollover(state: SessionState, anomaly: Anomaly, ttl: float | None) -> None:
    """Note that a rollover another worker made is now this session's cooldown.

    The key's next request may land on any worker, so each one that holds a
    rollover's cooldown records the stop's return when it serves it (the plane
    tolerates the repeats). Only a rollover that expires counts: budget,
    wall-trip and admin latches have no stop to return from, and the last
    strike's block (``action == "blocked"``, no expiry) never returns. A
    rollover already recorded on this session is not armed again, however
    often the plane repeats it. The caller holds ``state.lock``.
    """
    details = anomaly.details if isinstance(anomaly.details, dict) else {}
    if ttl is None or details.get("action") != "rollover":
        return
    try:
        strikes = int(details.get("strikes", state.returned_strikes + 1))
    except (TypeError, ValueError):
        strikes = state.returned_strikes + 1
    if strikes > state.returned_strikes:
        state.returns_from = "stopped"
        state.return_strikes = strikes


def _remote_anomaly(latch, state: "SessionState | None" = None) -> Anomaly | None:
    """The anomaly a remote latch describes, or ``None`` if it describes none.

    Tolerant by design: the plane is a different program on a different release
    cycle, and a field it renames must cost this worker a default, not a
    refused session it cannot explain. Carries a :class:`~runbound.events.Decision`
    (``level="fleet"``, ``provider_called=False``) built from the relayed
    detector name alone — see :func:`_relayed_decision` — since the
    originating worker's own boundary does not travel, only its detector and
    message do.
    """
    if not isinstance(latch, dict):
        return None
    details = latch.get("details")
    details = dict(details) if isinstance(details, dict) else {}
    details.setdefault("origin", "fleet")
    detector = str(latch.get("detector") or "fleet")
    key = getattr(state, "key", None) if state is not None else None
    details["decision"] = _relayed_decision(detector, "fleet").as_dict()
    details["key_hash"] = None if key is None else key_hash(key)
    return Anomaly(
        detector=detector,
        severity=str(latch.get("severity") or "critical"),
        message=str(latch.get("message") or "Stopped by another worker in the fleet"),
        details=details,
    )


def _refuse_halted(engine: Engine, state: SessionState) -> None:
    """Enforce an org-wide halt at the door, in the mode the customer chose.

    ``on_halt="raise"`` refuses the block — alerted once per session, like any
    other door refusal — and ``"warn"`` lets it run and says so at most once a
    minute, which is what a customer rolling the switch out wants: proof that
    the halt reached this worker, without stopping the business.

    Fail-open even here: a halt we cannot describe is a halt we do not enforce,
    because refusing a block with a broken exception would be worse than
    missing one.
    """
    try:
        anomaly = _halt_anomaly(engine, state)
        raising = engine.config.on_halt == "raise"
        if raising:
            engine.notify_door(state, anomaly)
    except Exception:
        _LOG.warning("runbound could not apply the fleet halt", exc_info=True)
        return
    if not raising:
        _warn_halted(anomaly)
        return
    raise GuardrailTripped(anomaly)


def _halt_anomaly(engine: Engine, state: SessionState) -> Anomaly:
    """Describe the halt this worker is enforcing. Carries no raw key.

    Carries a :class:`~runbound.events.Decision` (``boundary="halt"``,
    ``level="fleet"``) whose ``evaluation`` states ``provider_called=False``
    and the ``halt_mode`` the plane sent. A halt reaches a worker as a flag and
    a mode: there is no ``limit``/``used`` to state and no directive id, so
    the Decision says what it knows, in words, and nothing it does not.
    """
    config = engine.config
    key = getattr(state, "key", None)
    mode = None
    try:
        mode = engine.shared.halt_mode()
    except Exception:
        _LOG.debug("runbound could not read the halt's mode", exc_info=True)
    mode = mode or "stop"
    decision = _relayed_decision(
        HALT_DETECTOR,
        "fleet",
        reason=f"halted by the control plane (mode: {mode})",
        halt_mode=mode,
    )
    return Anomaly(
        detector=HALT_DETECTOR,
        severity="critical",
        message="Fleet halted by the control plane",
        details={
            "service": config.service,
            "worker_id": config.resolved_worker_id(),
            "key_hash": None if key is None else key_hash(key),
            "session_id": getattr(state, "session_id", ""),
            "on_halt": config.on_halt,
            "decision": decision.as_dict(),
        },
    )


def _warn_halted(anomaly: Anomaly) -> None:
    """Log the halt at most once a minute, whatever the traffic."""
    global _HALT_WARNED_AT
    now = _now()
    with _LOCK:
        last = _HALT_WARNED_AT
        if last is not None and now is not None and now - last < _HALT_WARN_INTERVAL_S:
            return
        _HALT_WARNED_AT = now
    _LOG.warning(
        "[runbound] %s (on_halt='warn': sessions keep running)", anomaly.message
    )


def _warn_narrowed(engine: Engine) -> None:
    """Log a Narrow halt this worker's ``on_halt="warn"`` is not applying.

    The Narrow-halt version of :func:`_warn_halted`: at most once a minute,
    whatever the traffic, so an operator can see from this worker's own
    logs that the fleet was narrowed and that this process chose not to be.
    """
    global _NARROW_WARNED_AT
    now = _now()
    with _LOCK:
        last = _NARROW_WARNED_AT
        if last is not None and now is not None and now - last < _HALT_WARN_INTERVAL_S:
            return
        _NARROW_WARNED_AT = now
    _LOG.warning(
        "[runbound] Fleet narrowed by the control plane for %r "
        "(on_halt='warn': tools keep running unrestricted)",
        engine.config.service,
    )


def _flush_refusal_summaries(state: SessionState) -> None:
    """Report the refusals this session suppressed, ahead of its exit record.
    Never raises."""
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is not None:
            engine.flush_refusal_summaries(state)
    except Exception:
        _LOG.warning("runbound could not summarise suppressed refusals", exc_info=True)


def _sync_exit(key: str, state: SessionState) -> None:
    """Report what this block added to the fleet's tally. Never raises.

    Deltas, not totals, and asynchronous: the exporter queues them and a
    background thread posts them in batches, so closing a block costs the
    request a dict copy and nothing on the network. Without a control plane
    not even that: there is nobody to report to, so nothing is computed.
    """
    try:
        with _LOCK:
            shared = _SHARED
        if not getattr(shared, "fleet", True):
            return
        delta = _exit_delta(key, state)
        if delta is not None:
            shared.exit(key, state, delta)
    except Exception:
        _LOG.warning(
            "runbound could not report the exit of session %r", key, exc_info=True
        )


def _exit_delta(key: str, state: SessionState) -> "ExitDelta | None":
    """What ``state`` has added since its last reported exit.

    ``seq`` rises per session, so the plane can drop a duplicate delivery
    instead of counting a block's spend twice. Counters only ever go up, so
    every delta is clamped at zero: a session reset underneath us costs the
    plane one empty report, never a negative budget.

    ``steps_delta`` carries model turns (an agent step is a turn, not
    every recorded event) — ``state.turns`` here, not ``state.event_count``.
    ``events_delta``, ``errors_delta`` and ``tokens_cached_delta`` are
    diffed the same way, off ``state.event_count``, ``state.total_errors``
    and ``state.tokens_cached_in``.

    ``last_detector``, ``trigger_message`` and ``trigger_age_s`` are not
    diffed — they are a snapshot of ``state.tripped_by``/``tripped_at``,
    the anomaly that latched this session (``None`` for a session that never
    tripped, including every ``on_anomaly="warn"`` session, which never
    latches at all). ``trigger_message`` goes through the same
    :func:`~runbound.plane_types.redact_key` an exported anomaly's message
    does, so a key a detector named in its own sentence does not reach the
    plane unless ``send_session_keys`` is on — everything else in that
    sentence is already hashes, counts, timing, money, names and error
    classes, never prompt or tool-argument content.

    ``baseline_*`` and ``rung_*`` are likewise a snapshot, not a
    diff: this session's *held* baseline (never live medians taken mid-spike
    -- see :func:`~runbound.detectors._baseline`) and its ladder rung, both
    read straight off ``state`` under the same lock. The baseline is
    reported only when this worker actually learned it itself
    (``spike_baseline_source == "local"``) -- a restored or peer baseline is
    someone else's number (or the whole service's), and reporting it back
    as this key's own would let one worker's peer fallback become every
    other worker's "fact" about a key it has never even seen a call from.
    """
    with state.lock:
        spend = float(state.total_cost_usd)
        tokens = int(state.total_tokens)
        steps = int(state.turns)
        events = int(state.event_count)
        errors = int(state.total_errors)
        tokens_cached = int(state.tokens_cached_in)
        tools = dict(state.tool_calls)
        tripped = state.tripped_by
        tripped_at = state.tripped_at
        baseline = state.spike_baseline
        baseline_source = state.spike_baseline_source
        baseline_samples = state.spike_baseline_samples
        rung_level = int(state.spike_level or 0)
        rung_allowance = state.spike_allowance
        rung_allowance_start = state.spike_allowance_start
    session_id = getattr(state, "session_id", "")
    with _LOCK:
        (
            seq,
            last_spend,
            last_tokens,
            last_steps,
            last_tools,
            last_events,
            last_errors,
            last_cached,
        ) = _EXITS.get(session_id, (0, 0.0, 0, 0, {}, 0, 0, 0))
        seq += 1
        _EXITS[session_id] = (
            seq, spend, tokens, steps, tools, events, errors, tokens_cached,
        )
        _EXITS.move_to_end(session_id)
        while len(_EXITS) > _EXIT_MEMO_MAX:
            _EXITS.popitem(last=False)
        engine = _ENGINE
        send_session_keys = bool(engine.config.send_session_keys) if engine else False
    khash = key_hash(key)
    last_detector = None
    trigger_message = None
    trigger_age_s = None
    if tripped is not None:
        last_detector = str(tripped.detector)
        message: Any = tripped.message
        if not send_session_keys:
            message = redact_key(message, key, khash)
        trigger_message = str(message)[:DETAIL_STRING_MAX]
        now = _now()
        if tripped_at is not None and now is not None:
            trigger_age_s = max(0.0, now - tripped_at)
    return ExitDelta(
        key_hash=khash,
        seq=seq,
        spend_delta_usd=max(0.0, spend - last_spend),
        tokens_delta=max(0, tokens - last_tokens),
        steps_delta=max(0, steps - last_steps),
        tool_calls=_tool_delta(tools, last_tools),
        events_delta=max(0, events - last_events),
        errors_delta=max(0, errors - last_errors),
        tokens_cached_delta=max(0, tokens_cached - last_cached),
        last_detector=last_detector,
        trigger_message=trigger_message,
        trigger_age_s=trigger_age_s,
        **_exit_baseline_fields(baseline, baseline_source, baseline_samples),
        rung_level=rung_level,
        rung_allowance=rung_allowance,
        rung_allowance_start=rung_allowance_start,
    )


def _exit_baseline_fields(
    baseline: tuple[float, float] | None, source: str | None, samples: int | None
) -> dict:
    """The ``ExitDelta`` baseline kwargs for one session's exit.

    ``{}`` (every default: zero samples) unless this worker actually learned
    the baseline itself — see :func:`_exit_delta`'s own docstring for why a
    restored or peer baseline is never reported back as this key's own.
    """
    if source != "local" or baseline is None or not samples or samples <= 0:
        return {}
    try:
        duration, output = float(baseline[0]), float(baseline[1])
    except (TypeError, ValueError, IndexError):
        return {}
    return {
        "baseline_duration_s": duration,
        "baseline_output_tokens": output,
        "baseline_samples": int(samples),
    }


def _tool_delta(tools: dict, previous: dict) -> dict:
    """Per-tool call counts since the last exit, dropping the unchanged ones."""
    delta = {}
    for name, count in tools.items():
        added = count - previous.get(name, 0)
        if added > 0:
            delta[name] = added
    return delta


def plane_status() -> PlaneStatus:
    """Where this worker's link to the control plane stands, right now::

        PlaneStatus(mode="connected", last_contact_age_s=1.2,
                    consecutive_failures=0, notice=None)

    ``mode`` is ``"local"`` when no plane is configured (and before
    :func:`init`), ``"connected"`` while it is answering, and ``"degraded"``
    when it is not — in which case every answer is being made locally and the
    SDK behaves exactly as it does without a plane. Safe to call from a health
    endpoint: it reads cached state and never opens a socket.

    ``halt_stale_s`` is seconds since the last successful contact with the
    plane while a fleet-wide halt is being enforced, and ``None`` whenever no
    halt is currently enforced — including a stale one that
    ``stale_halt="release"`` (the default) already let lapse.
    """
    try:
        with _LOCK:
            shared = _SHARED
        return shared.status()
    except Exception:
        _LOG.warning("runbound could not read the control plane status", exc_info=True)
        return PlaneStatus(mode="degraded")


def fleet_status(key: str) -> dict | None:
    """What the plane last said about one key, or ``None`` if nothing recent::

        {"fleet_spend_usd": 4.8, "fleet_tokens": 9000, "strikes": 1,
         "generation": 3, "halt": False, "latched": True,
         "policy_version": 4, "age_s": 1.2, "door_refusals": 4}

    The other half of :func:`session_status`: that one reports what *this*
    worker knows about one key, this one what the whole fleet does. Reads
    the entry-decision cache only, so it never opens a socket and answers
    ``None`` for a key this worker has not opened a session for in the last few
    seconds — and always, without a control plane.

    ``door_refusals`` is the exception, and is local: how many blocks *this*
    worker has turned away for this key while it was latched. The plane adds
    the fleet's up.
    """
    try:
        with _LOCK:
            shared = _SHARED
            refusals = _DOOR_REFUSALS.get(key, 0)
        status = shared.fleet_status(key)
        if status is not None:
            status["door_refusals"] = refusals
        return status
    except Exception:
        _LOG.warning("runbound could not read the fleet status of %r", key, exc_info=True)
        return None


# --- fan-out ----------------------------------------------------------------


def active_sessions() -> int:
    """How many keyed :func:`session` blocks are open right now, process-wide.

    Counts blocks, not keys: the same key entered twice, in two threads or
    nested, counts twice, because each of those is a piece of work in flight.
    The default session is never counted — it is the process itself — so this
    reads ``0`` in a program that uses no keys, and before :func:`init`.
    """
    with _LOCK:
        return _ACTIVE


def _count_entry() -> None:
    """Record that one more keyed block is open. Never counts a refused one."""
    global _ACTIVE
    with _LOCK:
        _ACTIVE += 1


def _count_exit() -> None:
    """Record that a keyed block has closed, however its body ended.

    Floored at zero: :func:`init` and :func:`reset` put the count back to zero
    while blocks may still be open, and their exits must not drive it negative.
    """
    global _ACTIVE
    with _LOCK:
        _ACTIVE = max(0, _ACTIVE - 1)


def _refuse_fanout(state: SessionState) -> None:
    """Record this block's lineage, then enforce the fan-out limits.

    The cascade this guards against — an agent opening sub-agents that open
    sub-agents — is an operational blunder with no single expensive call in
    it, so the numbers that describe the *shape* of the run are the ones a
    customer states: how many sessions may be open at once, how deep blocks
    may nest, how many children one session may open.

    Enforcement is at the door and unconditional: like the per-call caps, an
    explicit limit is enforced whatever ``on_anomaly`` says, and unlike a trip
    it latches nothing — the next block for this key is judged on its own
    shape, not held behind a wall.

    Fail-open: anything that goes wrong deciding is logged and the block is
    entered, exactly as it would have been with no limits configured.
    """
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is None:
            return
        parent = _CURRENT.get()
        _record_lineage(state, parent)
        anomaly = _fanout_anomaly(state, parent, engine.config)
    except Exception:
        _LOG.warning(
            "runbound could not check the fan-out limits for session %r; continuing",
            getattr(state, "key", None),
            exc_info=True,
        )
        return
    if anomaly is None:
        return
    _alert_fanout(engine, anomaly, state)
    raise GuardrailTripped(anomaly)


def _record_lineage(state: SessionState, parent: SessionState | None) -> None:
    """Note where a session was first opened, once and only once.

    A session's depth and parent are where it was *born*: a key re-entered
    later under a different parent keeps the lineage it already has, so a
    shared helper session used from everywhere cannot inflate anybody's child
    count. Re-entering the same child under the same parent counts nothing
    either — the child is one child however many times it is used.
    """
    if parent is None or parent is state:
        return
    with parent.lock:
        parent_depth = int(parent.depth)
    with state.lock:
        if state.parent_key is not None:
            return
        state.parent_key = parent.key or "<default>"
        state.depth = parent_depth + 1
    with parent.lock:
        parent.children += 1


def _fanout_anomaly(
    state: SessionState, parent: SessionState | None, config: GuardrailConfig
) -> Anomaly | None:
    """The limit this block would break, or ``None`` if it breaks none.

    Checked in the order they cost money: how much is running at once, how
    deep the nesting has gone, how wide one session has spread.
    """
    key = getattr(state, "key", None)
    if config.max_active_sessions is not None:
        with _LOCK:
            open_blocks = _ACTIVE + 1
        if open_blocks > config.max_active_sessions:
            return _fanout(
                state,
                "active",
                open_blocks,
                config.max_active_sessions,
                f"Too many sessions open at once: {open_blocks} with "
                f"{key!r} entering, limit {config.max_active_sessions}",
            )
    if config.max_session_depth is not None:
        with state.lock:
            depth = int(state.depth)
        if depth > config.max_session_depth:
            return _fanout(
                state,
                "depth",
                depth,
                config.max_session_depth,
                f"Sessions nested too deep: {key!r} is at depth {depth}, "
                f"limit {config.max_session_depth}",
            )
    if config.max_child_sessions is not None and parent is not None:
        with parent.lock:
            children = int(parent.children)
        if children > config.max_child_sessions:
            return _fanout(
                state,
                "children",
                children,
                config.max_child_sessions,
                f"Too many child sessions: {parent.key or '<default>'!r} has "
                f"opened {children} with {key!r}, "
                f"limit {config.max_child_sessions}",
            )
    return None


def _fanout(
    state: SessionState, rule: str, count: int, limit: int, message: str
) -> Anomaly:
    """One refused block, in the terms an on-call human asks in.

    Carries a :class:`~runbound.events.Decision` — ``boundary=
    "blast_radius"``, the same word used elsewhere for this shape of
    limit — whatever ``rule`` (active/depth/children) actually tripped.
    """
    decision = Decision(
        verdict="deny",
        kind="entry",
        boundary="blast_radius",
        detector=FANOUT_DETECTOR,
        reason=message,
        evaluation={"limit": limit, "used": count, "rule": rule, "provider_called": False},
    )
    key = getattr(state, "key", None)
    return Anomaly(
        detector=FANOUT_DETECTOR,
        severity="critical",
        message=message,
        details={
            "session_id": getattr(state, "session_id", ""),
            "key": key,
            "tags": dict(getattr(state, "tags", None) or {}),
            "rule": rule,
            "count": count,
            "limit": limit,
            "decision": decision.as_dict(),
            "key_hash": key_hash(key) if key else None,
        },
    )


def _alert_fanout(engine: Engine, anomaly: Anomaly, state: SessionState) -> None:
    """Record one branch refused at the fan-out door, as its own record.

    Every branch a limit turns away is a refusal and is evidence: it is
    recorded and exported as its own anomaly and Decision through the same
    capped path every other refusal takes (per session, detector, rule and
    tool, with a summary past the cap; see
    :meth:`~runbound.engine.Engine.record_door_refusal`). Before, only the
    first refusal of a rule per session reached the record, however many
    branches were refused. Paging stays deduplicated, by the control plane.
    Fail-open, like every other notification path: a refusal is never lost to
    a broken alerter.
    """
    try:
        engine.record_door_refusal(state, anomaly)
    except Exception:
        _LOG.warning("runbound could not record a fan-out refusal", exc_info=True)


# --- the abuse ladder's rollover --------------------------------------------


def _rolled_over(key: str, state: SessionState) -> SessionState:
    """``key``'s session, replaced first if the ladder closed the old one.

    The ladder's last rung latches a session with ``action="rollover"``: the
    detector has decided this key's session is over, and retiring it is
    the api's half of the deal — a new generation of the key, one more strike,
    half the allowance, and a cooldown to serve before the fresh session runs.
    The strike outlives the session it was earned on, so a repeat offender
    cannot be forgiven by an eviction.

    Only entry rolls a key over. An event arriving on a closed session simply
    re-applies that session's latch, as any latched session does, so the swap
    always happens between requests rather than in the middle of one. And only
    ``on_spike="limit"`` has a ladder at all: every other configuration walks
    straight past this, into the entry check it has always had.

    What a closed key's entry costs — one more strike and a cooldown, or the
    last strike and a block — is :func:`runbound.ladder.transition`'s to say;
    this reads the facts it needs (is the session closed, how many strikes
    has the key spent) and applies the effects it hands back.

    Fail-open: anything that goes wrong is logged and the old state is entered
    exactly as it would have been without the ladder.
    """
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is None:
            return state
        # on_spike/spike_max_strikes have no local field of their own --
        # the effective config always carries
        # them (defaulted, or plane-stated), the same shim the detector
        # itself reads through.
        config = engine._effective_config()
        if not engine._effective_spike_enabled() or config.on_spike != "limit":
            return state
        latched = _latched(state, config, engine.detectors)
        if latched is None:
            _cooldown_served(state)
            return state
        strikes = _rollover_strikes(state, latched)
        if strikes is None:
            return state
        strikes = min(strikes, config.spike_max_strikes)
        move = ladder.transition(
            _ladder_level(state),
            ladder.entry_observation(strikes, config),
            config,
        )
        if Effect.ROLLOVER not in move.effects:
            return state
        return _roll_over(key, state, latched, strikes, config, move)
    except Exception:
        _LOG.warning(
            "runbound could not roll session %r over; continuing on the old one",
            key,
            exc_info=True,
        )
        return state


def _rollover_strikes(state: SessionState, latched: Anomaly) -> int | None:
    """The strike this latch calls for, or ``None`` if it calls for none.

    A rollover anomaly is carried by two sessions: the one it closed and the
    fresh one it latches as a cooldown. Only the first has yet to pay for it,
    and the strike count is what tells them apart — a session created by a
    rollover already carries the strike written into the anomaly, so retrying
    all through the cooldown costs the caller nothing extra.
    """
    details = latched.details
    if not isinstance(details, dict) or details.get("action") != "rollover":
        return None
    with state.lock:
        carried = int(getattr(state, "strikes", 0) or 0)
    try:
        strikes = int(details.get("strikes", carried + 1))
    except (TypeError, ValueError):
        strikes = carried + 1
    return strikes if strikes > carried else None


def _ladder_level(state: SessionState) -> int:
    """The rung this session sits on, read the way every counter is read."""
    with state.lock:
        return int(getattr(state, "spike_level", 0) or 0)


def _cooldown_served(state: SessionState) -> None:
    """Retire a cooldown this session has now outlived.

    ``latch_ttl_override`` is one cooldown's expiry, not a policy the session
    keeps: once the session is running again its next latch is an ordinary
    one, permanent unless ``latch_ttl_seconds`` says otherwise.
    """
    with state.lock:
        if state.tripped_by is None:
            state.latch_ttl_override = None


def _record_return_if_served(state: SessionState) -> None:
    """Record a stopped session's return, once its cooldown is served and the
    entry has been admitted.

    Only a session the ladder started as a rollover cooldown carries
    ``returns_from`` (a latch relayed from another worker does not: it has an
    expiry but no stop of this worker's to return from). It is called after
    the entry's refusals, so a cooldown that ran out but whose key is refused
    again on the same entry records nothing yet. Once per cooldown: the mark
    is spent as it is recorded.
    """
    with state.lock:
        previous = state.returns_from
        if previous is None or state.tripped_by is not None:
            return
        state.returns_from = None
        state.returned_strikes = max(state.returned_strikes, state.return_strikes)
        level = ladder.LEVEL_NAMES.get(int(getattr(state, "spike_level", 0) or 0))
    state.record_return(previous, "ladder", "cooldown served", level)


def _roll_over(
    key: str,
    old: SessionState,
    anomaly: Anomaly,
    strikes: int,
    config: GuardrailConfig,
    move: Transition,
) -> SessionState:
    """Retire ``old`` and put the key's next session in its place, latched.

    Either the swap happens whole — new generation, strike recorded, fresh
    session registered and latched — or the key is left exactly as it was
    found and the failure is re-raised for the caller to fail open on.

    ``move`` is the ladder's answer for this entry (``strikes`` already
    capped at ``spike_max_strikes``); it carries the
    :class:`~runbound.ladder.Effect`\\ s :func:`_latch_rollover` applies.
    """
    with _LOCK:
        generation = _GENERATIONS.get(key, 0)
        previous = (_REGISTRY.pop(key, None), generation, _STRIKES.get(key))
        _GENERATIONS[key] = generation + 1
        _STRIKES[key] = strikes
        try:
            fresh = _registered(key, dict(old.tags), config)
        except Exception:
            _restore(key, previous)
            raise
    _latch_rollover(fresh, old, key, anomaly, strikes, config, move)
    return fresh


def _restore(key: str, previous: tuple) -> None:
    """Undo a failed rollover's bookkeeping. Caller holds ``_LOCK``."""
    state, generation, strikes = previous
    _GENERATIONS[key] = generation
    if strikes is None:
        _STRIKES.pop(key, None)
    else:
        _STRIKES[key] = strikes
    if state is not None:
        _REGISTRY[key] = state


def _latch_rollover(
    fresh: SessionState,
    old: SessionState,
    key: str,
    anomaly: Anomaly,
    strikes: int,
    config: GuardrailConfig,
    move: Transition,
) -> None:
    """Start the fresh session stopped: a cooldown, or the final block.

    Which of the two is ``move``'s to say — :attr:`~runbound.ladder.Effect.BLOCK`
    is the ladder's terminus. Below ``spike_max_strikes`` the session is
    latched on the rollover anomaly itself for ``spike_cooldown_seconds`` —
    the key is refused for that long and then served again, on tighter terms.
    At the last strike there is no expiry: the key stays blocked until the
    business clears it.

    ``old``'s ladder facts — its history, heal count, and the timestamps of
    its last limit and close — move to ``fresh`` here, with one more entry
    appended recording the rollover (or block) itself: the key's "why was I
    limited" story is continuous across a strike, even though the session
    object underneath it was just replaced. Only the baseline and the current
    allowance start fresh, because a new session judges its own calls from
    scratch.
    """
    blocked = Effect.BLOCK in move.effects
    with old.lock:
        old_level = int(getattr(old, "spike_level", 0) or 0)
        history = deque(getattr(old, "ladder_history", ()), maxlen=10)
        healed_times = int(getattr(old, "healed_times", 0) or 0)
        closed_at = getattr(old, "spike_closed_at", None)
        trigger = getattr(old, "spike_trigger", None)
        limited_at = getattr(old, "spike_limited_at", None)
    with fresh.lock:
        fresh.tripped_by = (
            _blocked_anomaly(fresh, key, strikes, config) if blocked else anomaly
        )
        fresh.tripped_at = _now()
        fresh.latch_ttl_override = None if blocked else config.spike_cooldown_seconds
        # A rollover happens only to a session the ladder closed, which it stopped.
        fresh.returns_from = None if blocked else "stopped"
        fresh.return_strikes = strikes
        fresh.ladder_history = history
        fresh.healed_times = healed_times
        fresh.spike_closed_at = closed_at
        fresh.spike_trigger = trigger
        fresh.spike_limited_at = limited_at
        fresh.spike_level = move.next_level
        fresh.record_ladder_transition(old_level, move.next_level, move.reason)
    if blocked:
        _LOG.info(
            "runbound: session for key %r rolled over (strike %d of %d); "
            "blocked until clear()",
            key,
            strikes,
            config.spike_max_strikes,
        )
        return
    _LOG.info(
        "runbound: session for key %r rolled over (strike %d of %d); cooldown %.0fs",
        key,
        strikes,
        config.spike_max_strikes,
        config.spike_cooldown_seconds,
    )


def _blocked_anomaly(
    state: SessionState, key: str, strikes: int, config: GuardrailConfig
) -> Anomaly:
    """The latch a key that has spent every strike is held on.

    Level 4 is the ladder's terminus: no cooldown expires it, only
    :func:`clear`. Shaped like the detector's own spike anomalies so alerting,
    logging and :func:`session_status` read it the same way.
    """
    return Anomaly(
        detector=SPIKE_DETECTOR,
        severity="critical",
        message=(
            f"Session for key {key!r} is blocked after {strikes} of "
            f"{config.spike_max_strikes} strikes; clear() is the way back in"
        ),
        details={
            "level": 4,
            "action": "blocked",
            "strikes": strikes,
            "max_strikes": config.spike_max_strikes,
            "key": key,
            "tags": dict(getattr(state, "tags", None) or {}),
            "session_id": getattr(state, "session_id", ""),
        },
    )


def _refuse_if_tripped(state: SessionState) -> None:
    """Raise at the door for an already-tripped session under ``"raise"``.

    Only ``on_anomaly="raise"`` refuses entry; the other modes enter normally
    and re-apply their reaction on the block's first event. A latch that has
    outlived ``latch_ttl_seconds`` expires here, so a healed key walks in
    without spending an event on it. Anything that goes wrong deciding is
    logged and the block is entered — the guard's own bugs never close a
    business.

    A refusal that does happen is *reported* before it is raised, by
    :func:`_refused_at_door` — the latch may have been set by another worker
    minutes ago, and the blocks it turns away here are the only evidence the
    fleet has that it is working.
    """
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is None or engine.config.on_anomaly != "raise":
            return
        tripped = _latched(state, engine.config, engine.detectors)
    except Exception:
        _LOG.warning(
            "runbound could not check the latch for session %r; continuing",
            getattr(state, "key", None),
            exc_info=True,
        )
        return
    if tripped is None:
        return
    _refused_at_door(engine, state, tripped)
    raise GuardrailTripped(tripped)


def _refused_at_door(engine: Engine, state: SessionState, anomaly: Anomaly) -> None:
    """Report a block refused at the door for an already-latched key.

    Three things, in the order they matter, and none of them able to fail the
    refusal itself:

    * the on-call is told through :meth:`~runbound.engine.Engine.notify_door`
      — deduped like every other door refusal, so a retrying agent pages once;
    * the fleet is told, *every* time. A door refusal is a fact about this
      block, not about the latch, and the plane counts them: eight workers
      turning a key away is what a fleet-wide latch looks like from the
      outside, and "door_refusals=0" is what it looks like when it is broken.
      The report carries no ttl — the latch it enforces was set elsewhere and
      is already counting down, and restating an expiry here would move it;
    * this worker's own count goes up, which is what :func:`fleet_status`
      reports as ``door_refusals``.

    Fail-open throughout, and a no-op on the plane's side without one:
    :class:`~runbound.shared.LocalState` takes the trip and does nothing.
    """
    key = getattr(state, "key", None)
    try:
        engine.notify_door(state, anomaly)
    except Exception:
        _LOG.warning("runbound could not alert on a refused session", exc_info=True)
    try:
        with _LOCK:
            shared = _SHARED
            if key is not None:
                _DOOR_REFUSALS[key] = _DOOR_REFUSALS.get(key, 0) + 1
        shared.trip(key, state, anomaly, None, True)
    except Exception:
        _LOG.warning(
            "runbound could not report a refused session to the fleet", exc_info=True
        )


def is_tripped(key: str | None = None) -> Anomaly | None:
    """The anomaly a session is latched on, or ``None`` if it is running.

    With a ``key``, reports that keyed session — ask before doing expensive
    work for a caller, or to render "you have hit your limit" without
    catching an exception. Never creates a session, so an unknown key (or a
    key evicted from the registry) simply reads ``None``.

    Without one, reports the session work is being accounted to right now: the
    enclosing :func:`session` block's, else the default session's. ``None``
    before :func:`init`, like the rest of the SDK.

    Under ``latch_ttl_seconds`` this reads through the expiry: a latch older
    than the ttl is retired here and the session reports as running, the same
    answer its next event would have produced.
    """
    try:
        with _LOCK:
            engine = _ENGINE
            state = None if key is None else _REGISTRY.get(key)
        if key is None:
            state = current_session()
        if state is None or engine is None:
            return None
        return _latched(state, engine.config, engine.detectors)
    except Exception:
        _LOG.warning("runbound could not read the latch for %r", key, exc_info=True)
        return None


def session_status(key: str) -> dict | None:
    """Where a keyed session stands right now, or ``None`` if there is none.

    Everything the abuse ladder knows about one key, in the terms a
    dashboard or a support agent asks in::

        {"level": 2,                  # 0 quiet, 1 watching, 2 limited, 3 closed
         "strikes": 1,                # rollovers this key has already earned
         "allowance_left": 1,         # abnormal calls left before it closes
         "cooldown_remaining_s": 0.0, # seconds until it is served again
         "tripped_by": "spike",       # the detector holding it, or None
         "generation": 1,             # how many sessions this key has had
         "why": {...},                # the facts behind the numbers above
         "history": [...]}            # the transitions that got it here

    ``why`` answers "why was this user limited?" without anyone reading our
    code::

        {"trigger": {"metric": "duration", "value": 41.2, "median": 2.0,
                     "factor": 5.0, "at_s_ago": 12.4} or None,
         "limited_at_s_ago": 12.4,    # 0.0 if it has never been limited
         "allowance_start": 2,        # the limit's own starting allowance, or None
         "healed_times": 0,           # how many times level 2 has healed to 1
         "closed_at_s_ago": 0.0,      # 0.0 if it has never been closed
         "baseline": {"duration_s": 2.1, "output_tokens": 118.0} or None}

    ``trigger`` is the abnormal call that last moved the level up — untouched
    by a call that only spends allowance or heals — and ``baseline`` is the
    held or live median the detector is judging calls against. Both are
    ``None`` before there is one to report.

    ``history`` is up to the last 10 level transitions, oldest first, as
    ``(level_from, level_to, at_s_ago, reason)`` with ``reason`` one of
    ``"first_abnormal"``, ``"confirmed"``, ``"healed"``, ``"allowance_spent"``,
    ``"rollover"`` or ``"blocked"``. Both ``why`` and
    ``history`` survive a rollover — the fresh session's own entry is
    appended to what the closed one had already earned — so the story reads
    as one continuous climb rather than resetting at every strike.
    :func:`clear` stamps a ``"cleared"`` transition onto the session it is
    forgetting, but the key is already gone from the registry by then, so it
    is visible only to a caller holding its own reference to that session's
    state — never through this function.

    A blocked key reads ``level: 0`` here, because the session behind it is
    fresh — a permanent latch does not raise this field. What identifies a
    blocked key is ``strikes == spike_max_strikes`` together with a
    permanent latch (``tripped_by == "spike"``, ``cooldown_remaining_s ==
    0.0``) and a last ``history`` entry with reason ``"blocked"``. "Level 4"
    is a label on the anomaly's ``details["level"]`` when it trips — never a
    value this field takes.

    Never creates a session and never counts as using one, so polling it does
    not keep an idle key alive in the registry. ``None`` before :func:`init`
    and for a key with no session — unknown, cleared, or evicted.

    Like :func:`is_tripped`, it reads through an expired latch: a cooldown
    that has run out reports as running with nothing remaining, which is the
    answer the key's next entry would give.
    """
    try:
        with _LOCK:
            engine = _ENGINE
            state = _REGISTRY.get(key)
            generation = _GENERATIONS.get(key, 0)
        if engine is None or state is None:
            return None
        tripped = _latched(state, engine.config, engine.detectors)
        with state.lock:
            now = time.monotonic()
            status = {
                "level": state.spike_level,
                "strikes": state.strikes,
                "allowance_left": state.spike_allowance,
                "cooldown_remaining_s": _cooldown_remaining(state, tripped),
                "tripped_by": tripped.detector if tripped is not None else None,
                "generation": generation,
                "why": _why(state, now),
                "history": _ladder_history(state, now),
                "posture": state.posture.as_dict() if state.posture else None,
                "budget": _budget_dict(engine, state),
                "actions": {
                    "requested": state.requested_actions,
                    "admitted": state.admitted_actions,
                    "executed": state.executed_actions,
                    "refused": state.refused_actions,
                },
            }
        return status
    except Exception:
        _LOG.warning("runbound could not read the status of %r", key, exc_info=True)
        return None


def _why(state: SessionState, now: float) -> dict:
    """Build ``session_status``'s ``why``. Caller holds ``state.lock``.

    Every timestamp on ``state`` is monotonic; converting to "seconds ago"
    here, at read time, is what lets a polled dashboard show a number that
    keeps moving even though nothing about the session has changed since.
    """
    trigger = state.spike_trigger
    baseline = state.spike_baseline
    service_baseline = getattr(state, "service_baseline", None)
    return {
        "trigger": None
        if not trigger
        else {
            "metric": trigger["metric"],
            "value": trigger["value"],
            "median": trigger["median"],
            "factor": trigger["factor"],
            "vs_service": trigger.get("vs_service"),
            "at_s_ago": _s_ago(now, trigger["at"]),
        },
        "limited_at_s_ago": _s_ago(now, state.spike_limited_at),
        "allowance_start": state.spike_allowance_start,
        "healed_times": state.healed_times,
        "closed_at_s_ago": _s_ago(now, state.spike_closed_at),
        "baseline": None
        if baseline is None
        else {
            "duration_s": baseline[0],
            "output_tokens": baseline[1],
            # Where this baseline came from — "local" (this session's
            # own live or held median), "restored" (this key's own baseline,
            # delivered by the plane) or "peer" (the service-wide median,
            # standing in for a key with no baseline of its own yet).
            "source": getattr(state, "spike_baseline_source", None) or "local",
        },
        # The service-wide median the plane last delivered for this
        # session's service, or None without a plane. Read alongside
        # ``baseline`` for "N× its own normal, M× this service's".
        "service_baseline": None
        if service_baseline is None
        else {"duration_s": service_baseline[0], "output_tokens": service_baseline[1]},
    }


def _ladder_history(state: SessionState, now: float) -> list[tuple[int, int, float, str]]:
    """``state.ladder_history`` with its instants read as seconds-ago.

    Caller holds ``state.lock``. A fresh copy every call, so nothing external
    can rewrite the deque a customer's dashboard is reading.
    """
    return [
        (level_from, level_to, _s_ago(now, at), reason)
        for level_from, level_to, at, reason in state.ladder_history
    ]


def _s_ago(now: float, ts: float | None) -> float:
    """Seconds between a monotonic instant and ``now``; ``0.0`` if there is none."""
    return 0.0 if ts is None else max(0.0, now - ts)


def tool_calls(key: str | None = None) -> dict[str, int]:
    """How many times each tool has been *attempted*, per tool name.

    The policy-side companion of :func:`session_status`: what
    ``tool_policy``'s ``max_calls`` is measured against, so a dashboard — or a
    test — can see the tally without provoking a violation::

        {"send_email": 2, "issue_refund": 1}

    Attempts, not successes: a call refused by the policy or by a tripped
    session is counted, because it was made. With a ``key``, reports that keyed
    session; without one, the session work is being accounted to right now.

    A copy, so the caller cannot rewrite the count a policy enforces. Never
    creates a session: ``{}`` before :func:`init`, for an unknown or evicted
    key, and for a session that has called nothing.
    """
    try:
        if key is None:
            state = current_session()
        else:
            with _LOCK:
                state = _REGISTRY.get(key)
        if state is None:
            return {}
        with state.lock:
            return dict(state.tool_calls)
    except Exception:
        _LOG.warning("runbound could not read the tool calls of %r", key, exc_info=True)
        return {}


def _cooldown_remaining(state: SessionState, tripped: Anomaly | None) -> float:
    """Seconds left on a cooldown latch; ``0.0`` when nothing is counting down.

    Only a latch with the session's own ``latch_ttl_override`` — a rollover
    cooldown — has a countdown to report. A permanent block, an ordinary trip
    and a running session all read ``0.0``. Caller holds ``state.lock``.
    """
    ttl = state.latch_ttl_override
    if tripped is None or ttl is None or state.tripped_at is None:
        return 0.0
    now = _now()
    if now is None:
        return float(ttl)
    return max(0.0, float(ttl) - (now - state.tripped_at))


def clear(key: str) -> None:
    """Forget ``key``'s session entirely: explicit forgiveness for one key.

    The business decides when a blocked key is let back in — after a
    payment, a support review, or a billing period. The next :func:`session`
    block for that key starts a brand-new session — a new generation of that
    key's id: the latch is gone, counters are back at zero and the
    once-per-session detectors are armed again, so the same user can be
    stopped a second time on their own merits.

    Forgiveness is total: the strikes the abuse ladder counted against the key
    go too, so a cleared user starts again at the full allowance rather than
    one rollover away from being blocked.

    In fleet mode the plane is told too, so the key is let back in on every
    worker rather than only this one — best-effort, like every other call to
    it, and outside the lock.

    A no-op for a key with no session and before :func:`init`. The default
    session is not a key and is untouched; :func:`reset` covers that one.
    """
    try:
        with _LOCK:
            if _ENGINE is None:
                return
            shared = _SHARED
            state = _REGISTRY.pop(key, None)
            if state is not None:
                state.returns_from = None  # forgiveness is not a cooldown served
            _STRIKES.pop(key, None)
            _DOOR_REFUSALS.pop(key, None)
            _EXITS.pop(_keyed_id(key), None)
            _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        if state is not None:
            with state.lock:
                state.record_ladder_transition(state.spike_level, 0, "cleared")
            # Whatever narrowed this key is lifted with it, and said so: a
            # cleared key is the end of its containment.
            state._exit_posture(reason="cleared")
    except Exception:
        _LOG.warning("runbound could not clear session %r", key, exc_info=True)
        return
    shared.clear(key)


# --- the observation path ---------------------------------------------------


def _active() -> "tuple[Engine | None, SessionState | None]":
    """The engine and the session this call is accounted to, as one pair.

    Read together under ``_LOCK`` so that observing an event and enforcing a
    policy on the same call cannot land on two different sessions if
    :func:`init` runs in between. ``(None, None)`` before :func:`init`, which
    is what makes everything downstream inert.
    """
    state = _CURRENT.get()
    with _LOCK:
        engine = _ENGINE
        if state is None:
            state = _SESSION
    return (None, None) if engine is None or state is None else (engine, state)


def _observe(**fields: Any) -> None:
    """Build an event from ``fields`` and run it through the engine.

    Does nothing before :func:`init`. Accounts the event to the enclosing
    :func:`session` block's state, or to the default session outside one, and
    assigns the event's monotonic timestamp and its 1-based step number (one
    per event, numbered within that session).

    Swallows every failure except :class:`GuardrailTripped`, which is the
    circuit breaker doing its job and must reach the host.
    """
    try:
        engine, state = _active()
        if engine is None or state is None:
            return
        step = state.next_step()
        engine.process(state, Event(ts=time.monotonic(), step=step, **fields))
    except GuardrailTripped:
        raise
    except Exception:
        _LOG.warning("runbound failed to record an event; continuing", exc_info=True)


def _args_hash(tool_name: str, args: tuple, kwargs: dict) -> str | None:
    """sha256 of a salted canonical repr of ``(tool_name, args, sorted kwargs)``.

    Keyword order never changes the digest, so ``f(a=1, b=2)`` and
    ``f(b=2, a=1)`` are recognized as the same action. Only the digest is ever
    stored — raw arguments never leave the caller's process.

    Mixed with :data:`_HASH_SALT`, a value generated once per process, so the
    digest is an equality token good for spotting a repeat *inside this
    process* and not a fingerprint of the arguments themselves: the same call
    hashes identically every time this process makes it, but two processes
    (or the same call before and after a restart) hash it differently, and
    nobody who only sees the digest can work backward to what was called with.

    Returns ``None`` when the arguments cannot be repr'd (that call simply
    carries no loop signal, rather than failing).
    """
    try:
        canonical = repr((tool_name, args, tuple(sorted(kwargs.items()))))
        payload = _HASH_SALT + canonical.encode("utf-8", "replace")
        return hashlib.sha256(payload).hexdigest()
    except Exception:
        _LOG.warning("runbound could not hash arguments for tool %r", tool_name, exc_info=True)
        return None


def _record_tool_error(
    tool_name: str, exc: BaseException, *, retryable: bool = False
) -> None:
    """Emit a ``tool_error`` event for a tool that raised.

    A trip discovered while recording the failure is logged and dropped: the
    tool's own exception is the one the caller needs, and swapping it for
    :class:`GuardrailTripped` would hide the real failure.

    ``retryable`` is ``@runbound.tool(retryable=True)``'s own
    declaration, carried onto the event so the "retry" loop shape can grant
    this tool's failures a grace (see ``Event.retryable``).
    """
    try:
        detail = str(exc)[:ERROR_MAX_CHARS]
    except Exception:
        detail = type(exc).__name__
    try:
        _observe(kind="tool_error", tool_name=tool_name, error=detail, retryable=retryable)
    except GuardrailTripped:
        _LOG.warning(
            "runbound tripped while recording a failed tool call; "
            "re-raising the tool's own exception instead",
            exc_info=True,
        )


def _record_llm_call(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    duration_s: float = 0.0,
    tokens_reasoning: int = 0,
    tokens_cached_in: int = 0,
    tokens_cache_write_in: int = 0,
    *,
    partial: bool = False,
    tokens_estimated: bool = False,
    provider: str | None = None,
    tokens_cache_write_1h_in: int = 0,
    price_multiplier: float = 1.0,
) -> None:
    """Price a model call and emit its ``llm_call`` event.

    ``duration_s`` is how long the call took and ``tokens_reasoning`` how many
    of ``tokens_out`` were thinking tokens — a subset of it, not an addition,
    so pass the provider's completion count whole; both default to the
    "not measured" zero, so a caller that only knows the token counts still
    reports a complete event. ``tokens_cached_in`` is the subset of
    ``tokens_in`` that was a provider cache *read* hit, and
    ``tokens_cache_write_in`` the subset that wrote a new cache entry
    (Anthropic only; always 0 from OpenAI). Both default to 0, so every
    caller written before either existed (``record_call()``, an older
    wrapper, a 5- or 6-argument ``@runbound.llm``) still reports a complete
    event, just with nothing to price at a cache rate.

    Priced by :func:`~runbound.pricing.price_call` under
    ``config.on_unpriced_model``: an unpriced model is $0.00 (warned once per
    model, by default) under "zero", the ``unpriced_price_per_1m_usd`` fallback
    under "estimate", and — since the door (:meth:`_Hooks.before`) is what
    actually refuses under "refuse" — whichever of those two a call that
    reaches here anyway falls back to. The event's ``priced`` field is
    ``"estimated"`` exactly when the fallback pair was used. ``tokens_cached_in``
    prices at the model's published cached-*read* rate when there is one, and
    ``tokens_cache_write_in`` at its published cache-*write* rate — a premium,
    never the read discount — each falling back to the plain input rate when
    unpublished (never a guessed rate either way). ``tokens_cache_write_1h_in`` is the
    one-hour part of ``tokens_cache_write_in`` (priced at the fifth column), and
    ``price_multiplier`` the request's own multiplier (``inference_geo="us"``,
    ``speed="fast"``; see :func:`runbound.pricing.request_multiplier`) that scales the whole cost.

    ``partial`` and ``tokens_estimated`` are for a call that never actually
    finished — an abandoned stream reported by :meth:`_Hooks.abandoned` — and
    are carried straight onto the event; every other caller leaves them at
    their default of ``False``.

    ``provider`` is the endpoint label the call went to, when the caller
    knows it (every wrapper does, and :func:`record_call`); it rides on the
    event, and it and ``model`` are each noted as this process's current
    value, so a model or provider that changes under a running service is
    recorded once as a ``"runtime_change"`` (see
    :func:`runbound.local_events.note_runtime_value`).
    """
    # Counted before the engine check: the coverage report asks whether a
    # sensor fired, which is true whether or not anything was configured to
    # listen.
    _coverage.guarded_call()
    with _LOCK:
        engine = _ENGINE
    if engine is None:
        _tell_sinks_of_call(model, provider, None, "ok", None)
        return
    _note_runtime(model, provider)
    cost, estimated = price_call(
        model,
        tokens_in,
        tokens_out,
        engine.config.custom_prices,
        tokens_cached_in=tokens_cached_in,
        tokens_cache_write_in=tokens_cache_write_in,
        on_unpriced_model=engine.config.on_unpriced_model,
        unpriced_price_per_1m_usd=engine.config.unpriced_price_per_1m_usd,
        tokens_cache_write_1h_in=tokens_cache_write_1h_in,
        multiplier=price_multiplier,
    )
    _observe(
        kind="llm_call",
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        duration_s=duration_s,
        tokens_reasoning=tokens_reasoning,
        tokens_cached_in=tokens_cached_in,
        priced=("estimated" if estimated else None),
        partial=partial,
        tokens_estimated=tokens_estimated,
        provider=provider,
    )
    _tell_sinks_of_call(model, provider, cost, "ok", "estimated" if estimated else None)


def _tell_sinks_of_call(model: "str | None", provider: "str | None", cost: object, outcome: str, priced: "str | None") -> None:
    """Hand a guarded call to the sinks (the OpenTelemetry counters). Fail-open: it never costs the call."""
    try:
        local_events.record_call(model, provider, float(cost) if isinstance(cost, (int, float)) else None, outcome, priced)
    except Exception:
        _LOG.warning("runbound could not tell the sinks about a call; continuing", exc_info=True)


def _note_runtime(model: str | None, provider: str | None) -> None:
    """Note this call's model and provider as the process's current ones.

    Fail-open: a change that cannot be recorded never costs the call.
    """
    try:
        local_events.note_runtime_value("model", model or None)
        local_events.note_runtime_value("provider", provider or None)
    except Exception:
        _LOG.warning("runbound could not note a runtime change; continuing", exc_info=True)


# --- the wrappers' hooks ----------------------------------------------------


class _Hooks:
    """What a client wrapper calls around a provider request.

    The wrappers know how to read someone else's SDK; everything about what a
    failure *means* lives here and in the engine. The moments::

        before(provider, model=None, request=None)  # may raise CircuitOpen/GuardrailTripped
        success(provider, duration_s=0.0)    # the call worked (duration_s: for a rate-mode slow check)
        error(model, exc, duration_s, provider)  # the call failed
        release(provider)                    # the call is over, however it ended
        tool_request(name, args_hash)        # the model asked for a tool
        quota(provider, headers)             # the response carried rate-limit headers
        take_pending_delay()                 # a throttle delay owed to this task, if any
        abandoned(model, tokens_in, tokens_out, duration_s, provider, estimated)
            # a stream nobody finished reading

    ``provider`` is the endpoint label the wrapper worked out at ``wrap()``
    time — ``"openai@localhost:11434"`` — so two OpenAI-compatible servers in
    one process are counted and refused apart. ``model`` is passed when the
    wrapper knows it before the request goes out; without it (``None``, the
    default, or an older wrapper that only passes ``provider``)
    ``on_unpriced_model="refuse"`` cannot check the model's price here and the
    call proceeds — pricing still catches it after the fact, in
    :func:`_record_llm_call`.

    Every one of them is fail-open: before :func:`init`, or if anything inside
    runbound goes wrong, the host's call proceeds exactly as if none of this
    were installed. The deliberate exceptions are
    :class:`~runbound.exceptions.CircuitOpen`, the in-flight, unpriced-model
    and (opt-in) admission-budget refusals from :meth:`before`, and a
    :class:`~runbound.exceptions.GuardrailTripped` raised by detection in
    :meth:`error` or :meth:`tool_request` — the product doing its job.
    :meth:`abandoned` is the one exception to "GuardrailTripped propagates":
    it is called from a finalizer on an arbitrary thread that has nowhere to
    catch it, so it never lets one out.
    """

    @property
    def estimate_tokens(self) -> bool:
        """Should a wrapper estimate tokens the endpoint did not report?

        Read from the live configuration on every call rather than captured at
        ``wrap()`` time, so ``init(estimate_tokens=True)`` reaches clients that
        were wrapped before it. ``False`` before :func:`init` and whenever the
        answer cannot be read.
        """
        try:
            with _LOCK:
                engine = _ENGINE
            return engine is not None and bool(engine.config.estimate_tokens)
        except Exception:
            return False

    def before(
        self,
        provider: str,
        model: str | None = None,
        request: dict | None = None,
    ) -> "Hold | None":
        """Vet a call before it goes out: admission, then the in-flight cap.

        ``Engine.admit`` is the named admission phase: the circuit,
        then ``on_unpriced_model="refuse"``, then — opt-in, ``budget_admission``
        — an estimate of what this call would cost. It is handed the session
        this call is being made under (:func:`current_session`) and
        ``request``, the raw call kwargs a wrapper's ``create`` was given (an
        older wrapper, or the ``@runbound.llm`` decorator, which has no
        kwargs dict, passes ``None`` — admission's estimate then falls back to
        ``config.admission_output_tokens`` alone with no request text to
        count, still correct, just less informed).

        ``max_inflight_calls`` runs after, unchanged from before admission
        existed: on a
        self-hosted endpoint the scarce resource is GPU concurrency, not
        dollars, and a call that would take this label past the cap is
        refused here — before the request goes out and whatever
        ``on_anomaly`` says, because the number is one the customer stated. A
        refused call takes no slot; an allowed one takes exactly one, given
        back by :meth:`release`. It stays a separate, api-owned step (not
        part of ``admit``) because it is process-wide bookkeeping the engine
        does not otherwise keep — see ``_reserve_inflight``.

        ``admit`` may return a money :class:`~runbound.state.Hold`,
        taken as its very last step, before the in-flight check runs here. If
        the in-flight check then refuses, the call never happens at all, and a
        hold left standing would sit on the session for the rest of the run —
        so a refusal here releases it before the exception leaves this
        method. Returns the hold (or ``None``) on the ordinary path, for the
        caller to give back whenever this call closes out.

        Raises :class:`~runbound.exceptions.CircuitOpen` or
        :class:`~runbound.exceptions.GuardrailTripped` for a refusal at any
        phase; every phase is otherwise fail-open, and a call with no engine
        at all (before :func:`init`) is not vetted, exactly as before.
        """
        try:
            with _LOCK:
                engine = _ENGINE
            if engine is None:
                return None
            session = current_session()
        except Exception:
            _LOG.warning(
                "runbound could not look up the session for %r; the call proceeds",
                provider,
                exc_info=True,
            )
            return None
        hold = None
        if session is not None:
            hold = engine.admit(session, provider, model, request)
        try:
            _reserve_inflight(engine, provider)
        except BaseException:
            _release_hold(hold)
            raise
        return hold

    def release(self, provider: str) -> None:
        """Give back the in-flight slot :meth:`before` took. Never raises."""
        _release_inflight(provider)

    def take_pending_delay(self) -> float:
        """The throttle delay the engine decided for the current task, cleared.

        ``on_loop="throttle"`` under a running event loop cannot
        ``time.sleep`` without blocking every other request this worker is
        serving, so the engine stashes the delay instead of sleeping on it; an
        async caller — the ``@runbound.tool`` wrapper, or a wrapped client's
        async request loop — calls this right after the event that might have
        set it and ``await asyncio.sleep``s whatever comes back. Returns
        ``0.0`` when nothing is pending, before :func:`init` (nothing sets it
        without an engine), and whenever reading it fails: a lost delay skips
        one throttle rather than blocking a call that should have gone
        through.
        """
        try:
            return _engine_take_pending_delay()
        except Exception:
            return 0.0

    def abandoned(
        self,
        model: str | None,
        tokens_in: int,
        tokens_out: int,
        duration_s: float,
        provider: str,
        estimated: bool,
    ) -> None:
        """Record a stream nobody finished reading, as one partial call.

        Called from a ``weakref.finalize`` callback when a stream proxy is
        garbage-collected without having emitted or failed — on whatever
        thread the collector happens to run on, at a moment with no session
        context and no caller able to react to anything this raises. The call
        is recorded through the normal path (:func:`_record_llm_call`, marked
        ``partial=True``) so every budget, detector, latch and callback sees
        it exactly like a call that returned normally; ``estimated`` says
        whether ``tokens_out`` is the provider's own usage or a chars/4 guess
        made from what streamed by before the stream was abandoned, and is
        carried onto the event as ``tokens_estimated``.

        Deliberately never calls :meth:`success` or :meth:`error`: a call
        whose real outcome was never observed is not evidence the provider
        either worked or failed, so the circuit is left exactly as it was.

        Never raises, not even :class:`~runbound.exceptions.GuardrailTripped`
        from a budget or latch this call trips — there is nothing upstream of
        a finalizer that could catch it, so it is logged at DEBUG and
        swallowed instead. Detection, alerting and the latch itself still ran
        before that happens; only the exception is held back.
        """
        try:
            _record_llm_call(
                model,
                tokens_in,
                tokens_out,
                duration_s,
                partial=True,
                tokens_estimated=estimated,
                provider=provider,
            )
        except GuardrailTripped:
            _LOG.debug(
                "runbound: a trip fired while recording an abandoned stream "
                "call; swallowed (called from a finalizer, nothing to catch it)",
                exc_info=True,
            )
        except Exception:
            _LOG.debug("runbound: could not record an abandoned stream call", exc_info=True)

    def success(self, provider: str, duration_s: float = 0.0) -> None:
        """Report a call that worked: an open circuit closes. Never raises.

        ``duration_s`` is how long the call took; only a rate-mode circuit
        with ``circuit_slow_call_seconds`` set ever reads it, to mark
        a slow success against the breaker's rate window.
        """
        _coverage.provider_seen(provider)
        try:
            with _LOCK:
                engine = _ENGINE
            if engine is None:
                return
            engine.record_llm_success(provider, duration_s)
        except Exception:
            _LOG.warning(
                "runbound could not record a successful call to %r", provider, exc_info=True
            )

    def error(
        self, model: str | None, exc: BaseException, duration_s: float, provider: str
    ) -> None:
        """Report a failed model call to its session and to its provider.

        Raises only :class:`~runbound.exceptions.GuardrailTripped`, when this
        failure is the one that makes a retry storm: the wrapper is inside its
        own ``except`` block, so that trip replaces the provider's exception
        deliberately — the app has been retrying a wall and needs to be told to
        stop, not told again what it already knows.
        """
        _coverage.guarded_call()
        _coverage.provider_seen(provider)
        _tell_sinks_of_call(model, provider, None, "error", None)
        try:
            engine, state = _active()
            if engine is None or state is None:
                return
            engine.record_llm_error(state, model, exc, duration_s, provider)
        except GuardrailTripped:
            raise
        except Exception:
            _LOG.warning("runbound failed to record a failed model call", exc_info=True)

    def quota(self, provider: str, headers: Any) -> None:
        """Let the circuit read the rate-limit headers a response carried.

        A no-op unless ``circuit_reads_quota`` is on, and a no-op before
        :func:`init`. The wrapper only calls it when there were headers to
        read at all, which on a success path means the customer's own call
        went through ``with_raw_response`` / ``.parse()`` — an ordinary call
        returns a parsed model with no headers anywhere on it, and runbound
        will not change how the call is made to get at them.

        Never raises: the response is already in the caller's hands.
        """
        try:
            with _LOCK:
                engine = _ENGINE
            if engine is None:
                return
            engine.note_quota(provider, headers)
        except Exception:
            _LOG.warning(
                "runbound could not read the quota headers for %r", provider, exc_info=True
            )

    def tool_request(self, name: str, args_hash: str | None) -> None:
        """Record a tool call the model asked for, before anyone dispatches it.

        The hash is the wrapper's, already in the ``"req:"`` namespace, so a
        model asking for the same tool with the same arguments over and over is
        a loop even when the developer dispatches those calls by hand and
        runbound never sees a ``@tool``. ``loop_exempt`` is set when ``name``
        is listed in ``loop_ignore_tools`` — the ``@runbound.tool`` decorator
        applies the same config by name, but a model *request* has no
        decorator to carry ``polling=True`` on, so config is the only way
        to mark one exempt here.

        The name also joins the tool report: a tool the model can ask for that
        no ``@runbound.tool`` declared is a coverage gap, and this is the only
        place a process ever hears about it.
        """
        _coverage.tool_requested(name)
        _observe(
            kind="tool_request",
            tool_name=name,
            args_hash=args_hash,
            loop_exempt=_is_loop_ignored(name),
        )


#: The one hooks object; wrappers hold it, tests may call it directly.
_HOOKS = _Hooks()


def _release_hold(hold: Any) -> None:
    """Give back a money hold :meth:`_Hooks.before` took, without ever raising.

    Fail-open and duck-typed: a no-op for ``None`` and for anything with no
    ``release`` (an older caller, or a hold-shaped test double), and a broken
    ``release`` is logged and swallowed rather than let out — the host's call
    must proceed whatever runbound's own bookkeeping does.
    """
    try:
        release = getattr(hold, "release", None)
        if release is not None:
            release()
    except Exception:
        _LOG.warning("runbound could not release a budget hold", exc_info=True)


def _is_loop_ignored(tool_name: str) -> bool:
    """Is ``tool_name`` listed in this engine's ``loop_ignore_tools``?

    ``False`` before :func:`init` and whenever the answer cannot be read —
    the same fail-open default as every other read of live configuration
    here: losing this exemption costs one tool an extra loop-window entry, no
    worse than not having configured it.

    Checked on every ``tool_call``/``tool_request`` event, so the
    overwhelmingly common case — no ``loop_ignore_tools`` configured at all —
    returns before ever touching ``_LOCK``: :data:`_LOOP_IGNORE_TOOLS` mirrors
    the live config and an empty tuple is falsy.
    """
    if not _LOOP_IGNORE_TOOLS:
        return False
    try:
        return tool_name in _LOOP_IGNORE_TOOLS
    except Exception:
        return False


#: Circuit states from worst to best. A shape prefix answers with the worst of
#: its endpoints: one dead box in a pool is news, and reporting the pool as
#: healthy because two of three answer would hide it.
_CIRCUIT_SEVERITY = ("open", "half_open", "closed")


def circuit_state(provider: str = "openai") -> str:
    """Where one provider's circuit stands: closed, open or half-open.

    ``"closed"`` — calls go out normally; ``"open"`` — the provider has been
    failing and the cooldown is still running; ``"half_open"`` — the cooldown
    has passed and the next call is the probe that decides. Reads ``"closed"``
    before :func:`init`, and whenever the answer cannot be read: a health
    endpoint asking runbound how things are must never be the thing that
    breaks.

    Circuits are keyed per endpoint — ``"openai@localhost:11434"`` — and this
    takes either form. A full label answers for that endpoint alone; a bare
    shape (``"openai"``) answers with the **worst** state among the endpoints
    of that shape, so a health check written before runbound knew about
    endpoints keeps meaning what it meant.

    The state is counted under every configuration; whether an open circuit
    actually refuses calls is ``on_provider_failure``.
    """
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is None:
            return "closed"
        keys = _circuit_keys(engine)
        if provider in keys:
            return engine.circuit.state(provider)
        matching = [key for key in keys if key.startswith(provider + "@")]
        states = {engine.circuit.state(key) for key in matching}
        return next((state for state in _CIRCUIT_SEVERITY if state in states), "closed")
    except Exception:
        _LOG.warning(
            "runbound could not read the circuit state for %r", provider, exc_info=True
        )
        return "closed"


def _circuit_keys(engine: Engine) -> list[str]:
    """Every label this engine's breaker has ever seen, or none.

    The breaker is a plain counter keyed by string and has no listing of its
    own; a shape prefix needs one to resolve. Unreadable reads as empty, which
    answers ``"closed"`` — the same fail-open answer as everything else here.
    """
    try:
        return list(getattr(engine.circuit, "_keys", None) or ())
    except Exception:
        return []


# --- the in-flight cap ------------------------------------------------------


def inflight_calls(provider: str = "openai") -> int:
    """How many guarded calls to one provider are in flight right now.

    Takes a full endpoint label (``"openai@localhost:11434"``) for that
    endpoint alone, or a bare shape (``"openai"``) for the sum across its
    endpoints — the same resolution :func:`circuit_state` uses. Counts calls,
    not clients: a streamed call is in flight until the stream ends, and a
    stream that is never exhausted or closed is never given back.

    Reads ``0`` before :func:`init`, when no ``max_inflight_calls`` is
    configured (nothing is counted then), and whenever the answer cannot be
    read.
    """
    try:
        with _LOCK:
            return sum(
                count
                for label, count in _INFLIGHT.items()
                if label == provider or label.startswith(provider + "@")
            )
    except Exception:
        _LOG.warning(
            "runbound could not read the in-flight count for %r", provider, exc_info=True
        )
        return 0


def _reserve_inflight(engine: Engine, provider: str) -> None:
    """Take one in-flight slot for ``provider``, or refuse this call.

    Nothing at all happens without ``max_inflight_calls``: no counter, no
    lock contention, no behavior change for the hosted-API case this was never
    about. With it, the count is raised under ``_LOCK`` and the call that
    would take it past the cap is refused instead — alerted once per endpoint,
    latching nothing, whatever ``on_anomaly`` says.

    Raises :class:`~runbound.exceptions.GuardrailTripped`, and nothing else:
    a bug in the accounting lets the call through.
    """
    try:
        limit = engine.config.max_inflight_calls
        if limit is None:
            return
        with _LOCK:
            running = _INFLIGHT.get(provider, 0)
            refused = running + 1 > limit
            if not refused:
                _INFLIGHT[provider] = running + 1
        if not refused:
            return
        anomaly = _inflight_anomaly(provider, running, limit)
        decision = Decision(
            verdict="deny",
            kind="entry",
            boundary="concurrency",
            level="process",
            detector=INFLIGHT_DETECTOR,
            reason=anomaly.message,
            evaluation={"limit": limit, "used": running + 1, "provider_called": False},
        )
        anomaly = replace(
            anomaly, details={**anomaly.details, "decision": decision.as_dict()}
        )
    except Exception:
        _LOG.warning(
            "runbound could not apply the in-flight cap for %r; the call proceeds",
            provider,
            exc_info=True,
        )
        return
    _alert_inflight(engine, anomaly)
    raise GuardrailTripped(anomaly)


def _inflight_anomaly(provider: str, running: int, limit: int) -> Anomaly:
    """Describe the call the in-flight cap is about to refuse."""
    return Anomaly(
        detector=INFLIGHT_DETECTOR,
        severity="critical",
        message=(
            f"In-flight cap exceeded for provider {provider!r}: "
            f"{running} calls already running, limit {limit}"
        ),
        details={
            "provider": provider,
            "host": provider_host(provider),
            "count": running,
            "limit": limit,
        },
    )


def _alert_inflight(engine: Engine, anomaly: Anomaly) -> None:
    """Page once per endpoint, then stay quiet however often it is hit.

    A saturated endpoint refuses every caller that arrives while it is full,
    and the on-call learns nothing from the hundredth. The engine dedups on
    ``(detector, provider)`` for exactly this reason. Fail-open: a refusal is
    never lost to a broken alerter.
    """
    try:
        with _LOCK:
            state = _SESSION
        state = _CURRENT.get() or state
        if state is not None:
            engine.notify_door(state, anomaly)
        _LOG.warning("[runbound] %s", anomaly.message)
    except Exception:
        _LOG.warning("runbound could not alert on an in-flight refusal", exc_info=True)


def _release_inflight(provider: str) -> None:
    """Give one slot back, floored at zero, without ever raising.

    Floored because :func:`init` and :func:`reset` empty the counters while
    calls may still be in flight, and their releases must not drive the next
    run negative — a negative count would quietly raise the real cap.
    """
    try:
        with _LOCK:
            running = _INFLIGHT.get(provider)
            if running is None:
                return
            if running <= 1:
                _INFLIGHT.pop(provider, None)
            else:
                _INFLIGHT[provider] = running - 1
    except Exception:
        _LOG.warning(
            "runbound could not release an in-flight slot for %r", provider, exc_info=True
        )


# --- inference runbound cannot wrap ---------------------------------------


def _current_settled_spend() -> "float | None":
    """This process's settled spend right now, or ``None`` with no session.

    Read under the session's own lock, so a snapshot taken here agrees with
    whatever else is writing ``total_cost_usd`` at the same instant. Used to
    bracket a body :func:`llm` runs, before and after, so the difference is
    what that call actually cost — see :func:`_settle_hold`.
    """
    session = current_session()
    if session is None:
        return None
    with session.lock:
        return session.total_cost_usd


def _settle_hold(hold: Any, spent_before: "float | None") -> None:
    """Settle a money hold with what the guarded body actually spent.

    ``spent_before`` is :func:`_current_settled_spend` taken just before the
    body ran; the delta against the same read taken now is evidence only —
    floored at ``0.0`` so another thread's own spend landing in between can
    never look like a negative cost — because
    :meth:`~runbound.state.Hold.settle` never gives back more than the
    reservation whatever the actual was: an imprecise delta (concurrent
    spend on the same session) can shift the *evidence* but never the money
    the ledger returns. Falls back to a plain release, and never raises,
    when anything about reading the delta goes wrong — a hold must always be
    given back one way or another.
    """
    if hold is None:
        return
    try:
        spent_after = _current_settled_spend()
        if spent_before is None or spent_after is None:
            hold.release()
            return
        hold.settle(max(spent_after - spent_before, 0.0))
    except Exception:
        _LOG.warning(
            "runbound could not settle a budget hold; releasing it instead",
            exc_info=True,
        )
        _release_hold(hold)


def record_call(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    duration_s: float = 0.0,
    *,
    provider: str = "custom",
    error: BaseException | None = None,
) -> None:
    """Record one model call runbound did not make.

    The escape hatch for inference that never goes through a client
    :func:`wrap` understands: a local ``llama.cpp`` or ``transformers`` call
    in-process, a bespoke HTTP client, a gateway with its own SDK. What is
    recorded is exactly what a wrapped client records, so every detector,
    budget and limit sees it the same way::

        runbound.record_call("llama-3.1-8b", tokens_in=812, tokens_out=210,
                               duration_s=1.9, provider="llama.cpp@local")

    With ``error`` it records a *failed* call instead — an ``llm_error`` event,
    which feeds the retry-storm detector, and one mark against that provider's
    circuit. Token counts are ignored then: a call that failed produced none.

    ``provider`` is the label the circuit and the in-flight cap key on, and
    reads best as ``"<runtime>@<where>"`` (the wrappers use
    ``"openai@localhost:11434"``); the default is ``"custom"``.

    Never takes a money hold: the call this describes is already over
    by the time anyone tells us about it, so there is nothing left to hold
    for — the post-call ``budget`` wall is the only check that applies here.

    Raises :class:`~runbound.exceptions.GuardrailTripped` when this call is
    the one that trips the session — a budget spent, a storm confirmed — and
    is otherwise fail-open and inert before :func:`init`.
    """
    if error is not None:
        _HOOKS.error(model, error, duration_s, provider)
        return
    _record_llm_call(model, tokens_in, tokens_out, duration_s, provider=provider)
    _HOOKS.success(provider, duration_s)


def llm(
    fn: Callable | None = None,
    *,
    model: str | None = None,
    provider: str = "custom",
    tokens: Callable[[Any], tuple[int, int]] | None = None,
) -> Callable:
    """Record every call to a function that performs inference itself.

    ::

        @runbound.llm(model="llama-3.1-8b", provider="llama.cpp@local",
                        tokens=lambda out: (out.n_prompt, out.n_generated))
        def generate(prompt: str) -> Output: ...

    The wrapper times the call, applies the circuit, ``on_unpriced_model``
    (``model`` is known up front here, so ``"refuse"`` can act on it), the
    opt-in ``budget_admission`` estimate and the in-flight cap *before* the
    body runs, and records what came out through the same path :func:`wrap`
    uses: on success ``tokens(result)`` — a step with no tokens when no
    callback is given, or when the callback fails — and on an exception an
    ``llm_error`` plus a mark against ``provider``'s circuit, before the
    original exception is re-raised untouched.

    Under ``budget_admission`` a money hold is taken before the body
    runs and given back in the ``finally``, settled with whatever the body's
    own recording actually added to this session's spend — so the assumed
    cap is held for exactly the body's lifetime, not just for the moment
    ``before`` and the recording call run.

    An ``async def`` function gets an ``async def`` wrapper, so frameworks
    dispatching on ``inspect.iscoroutinefunction`` keep routing it as they
    did. Usable bare (``@runbound.llm``) or with any of the three options.
    :class:`~runbound.exceptions.GuardrailTripped` and
    :class:`~runbound.exceptions.CircuitOpen` propagate; everything else
    runbound does here is swallowed.
    """

    def decorate(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                hold = _HOOKS.before(provider, model)
                spent_before = _current_settled_spend()
                started_at = time.monotonic()
                try:
                    try:
                        result = await func(*args, **kwargs)
                    except Exception as exc:
                        _record_inference_error(model, exc, started_at, provider)
                        raise
                    _record_inference(model, result, started_at, provider, tokens)
                    return result
                finally:
                    _settle_hold(hold, spent_before)
                    _release_inflight(provider)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            hold = _HOOKS.before(provider, model)
            spent_before = _current_settled_spend()
            started_at = time.monotonic()
            try:
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    _record_inference_error(model, exc, started_at, provider)
                    raise
                _record_inference(model, result, started_at, provider, tokens)
                return result
            finally:
                _settle_hold(hold, spent_before)
                _release_inflight(provider)

        return wrapper

    return decorate if fn is None else decorate(fn)


def _record_inference(
    model: str | None,
    result: Any,
    started_at: float,
    provider: str,
    tokens: Callable[[Any], tuple[int, int]] | None,
) -> None:
    """Record one successful decorated inference, tokens if they can be read."""
    tokens_in, tokens_out = _read_tokens(tokens, result)
    record_call(
        model, tokens_in, tokens_out, max(time.monotonic() - started_at, 0.0),
        provider=provider,
    )


def _record_inference_error(
    model: str | None, exc: BaseException, started_at: float, provider: str
) -> None:
    """Record one failed decorated inference, then let its exception through."""
    record_call(
        model, 0, 0, max(time.monotonic() - started_at, 0.0), provider=provider, error=exc
    )


def _read_tokens(
    tokens: Callable[[Any], tuple[int, int]] | None, result: Any
) -> tuple[int, int]:
    """``tokens(result)`` as two non-negative ints, ``(0, 0)`` if it says nothing.

    The callback is the caller's own code reading their own runtime's output,
    so it is treated like any other user code here: whatever it raises or
    returns, the call it belongs to has already succeeded and must not fail
    because the counting did.
    """
    if tokens is None:
        return (0, 0)
    try:
        tokens_in, tokens_out = tokens(result)
        return max(int(tokens_in), 0), max(int(tokens_out), 0)
    except Exception:
        _LOG.warning(
            "runbound could not read the token counts of a call; recording zero",
            exc_info=True,
        )
        return (0, 0)


# --- decorating tools -------------------------------------------------------


def tool(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    polling: bool = False,
    idempotent: bool = False,
    retryable: bool = False,
    blocked: bool = False,
    max_calls: int | None = None,
    constraint: Callable[[ToolCall], bool] | None = None,
    require_approval: Callable[[ToolCall], bool] | None = None,
    reviewed: bool = False,
    effects=None,
) -> Callable:
    """Record every call to a tool, and state the rule it must obey.

    ::

        @runbound.tool
        def search(query): ...

        @runbound.tool(name="web_search")
        def search(query): ...

        @runbound.tool(polling=True)
        def poll_job_status(job_id): ...

    **The rule lives on the tool**, in the same line and the same diff as the
    function it governs — there is no policy file and no CLI::

        @runbound.tool(max_calls=1, constraint=under_500)
        def issue_refund(user: str, amount: float): ...

        @runbound.tool(blocked=True)          # the model may ask; it never runs
        def send_email(to: str): ...

        @runbound.tool(require_approval=ask_a_human)
        def wire_money(account: str, amount: float): ...

        @runbound.tool
        def lookup_order(order_id: str): ...  # known, allowed, no rule

    ``blocked=True`` refuses the tool outright (reported as the ``deny`` rule).
    ``max_calls=n`` allows ``n`` attempts per session, counting the one being
    judged. ``constraint`` and ``require_approval`` each take a
    ``predicate(call) -> bool`` receiving a :class:`~runbound.policy.ToolCall`;
    returning ``False`` — or raising, which is fail-**closed** — refuses the
    call. Each tool's ``require_approval`` is its own: two tools ask two
    different humans.

    ``effects`` is the **set of capability classes** this tool carries,
    from :data:`runbound.policy.CAPABILITIES`; left unset it is unclassified.
    It is not a rule and restricts nothing on its own. What reads it is the
    **posture**: a tool is refused before its body runs, with
    :class:`~runbound.exceptions.SafeModeViolation`, when the effective posture
    (or a class rule from ``init(capabilities=...)``) denies any one of its
    classes — and an unclassified tool is refused by every posture but
    ``full``, because nothing said what it does. Model calls go on. Under
    ``require_rules=True`` a tool carrying a consequential class needs a rule
    that can refuse it; ``reviewed=True`` alone does not pass that gate::

        @runbound.tool(effects={"read"}, reviewed=True)
        def lookup_order(order_id): ...

        @runbound.tool(effects={"financial"}, max_calls=1)
        def issue_refund(user, amount): ...

    ``reviewed=True`` states that this tool was looked at and is deliberately
    unrestricted. It is **not** ``ToolPolicy.allow``, which is a fleet-wide
    inverting allow-list; it restricts nothing and exists so a tool that needs
    no rule can still satisfy ``require_rules``.

    Rules fold into the policy the engine enforces the moment they are
    declared, so a tool decorated after :func:`init` — the usual ordering — is
    enforced from its first call. Where a tool's rule is stated both here and
    in ``init(tool_policy=...)``, this one wins and a warning says so.

    With ``init(require_rules=True)`` a tool that states no rule at all raises
    ``ValueError`` here, at decoration time: the CI gate, failing any step that
    imports the app. An unenforceable keyword (``max_calls=0``, a
    ``constraint`` that is not callable) raises here too, loudly, exactly as a
    bad ``init()`` argument does — misconfiguration is the one thing in
    runbound that is never swallowed.

    The ``tool_call`` event is emitted *before* the function runs, so a loop is
    broken on the repeat that would have made it — with ``on_anomaly="raise"``
    the tool body never executes on the tripping call. If the tool raises, a
    ``tool_error`` event is emitted and the tool's own exception is re-raised
    unchanged.

    ``polling=True`` (equivalently, listing this tool's name in
    ``loop_ignore_tools``) marks a tool that is *supposed* to run with the
    same arguments over and over — polling a job's status, say — so its calls
    never feed the loop window and can never trip ``loop`` detector's
    "repeat" or "sequence" shapes, however many times they repeat.
    They still count towards ``tool_calls()`` and any action policy's
    ``max_calls``: this marks polling, it does not raise a limit, so it is
    not the setting to reach for to dodge a real loop. Replaces
    ``repeatable=True`` (removed, not aliased, in 0.4.0 — the package was
    unannounced, so there is no compatibility burden; passing ``repeatable=``
    now raises ``TypeError``).

    ``idempotent=True`` states that calling this tool twice with the
    same arguments does the same thing once as it does twice — a customer
    fact about the tool's own semantics that nothing enforces on its own. It
    is reported in the tool report (:func:`coverage`) and the control
    surface for a human or the plane to read; runbound does not yet act on it.

    ``retryable=True`` states that this tool is expected to be
    retried by the caller's own code sometimes — a network call with its own
    backoff, say — and grants it a grace on the "retry" loop shape: its
    failures still count (towards ``error_storm`` too, unaffected), but
    "retry" does not fire on them until *twice* ``loop_threshold`` rather
    than once. It is reported alongside ``idempotent`` in the tool report
    and the control surface.

    An ``async def`` tool gets an ``async def`` wrapper, so the decorated
    function is still a coroutine function: frameworks that dispatch on
    ``inspect.iscoroutinefunction`` (FastAPI, LangChain) keep routing it the
    way they did. The event is emitted before the body is awaited; if that
    event trips a ``on_loop="throttle"`` policy while a loop is running, the
    delay the engine stashed (:meth:`_Hooks.take_pending_delay`) is awaited
    here, before the body runs, instead of the ``time.sleep`` a synchronous
    caller gets. A failed await is recorded exactly as a failed call is.
    Cancellation is not a tool failure and is re-raised untouched.
    """

    rules = _tool_rules(blocked, max_calls, constraint, require_approval, reviewed, effects)

    def decorate(func: Callable) -> Callable:
        tool_name = name or getattr(func, "__name__", "<tool>")
        _require_rule(tool_name, rules)
        _coverage.tool_decorated(tool_name)
        _coverage.tool_declared(
            tool_name, func, rules, polling=polling, idempotent=idempotent, retryable=retryable
        )

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _announce_call(
                    tool_name, args, kwargs, polling=polling, effects=rules.effects, decorated=True
                )
                delay = _HOOKS.take_pending_delay()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    _record_tool_error(tool_name, exc, retryable=retryable)
                    raise

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _announce_call(
                tool_name, args, kwargs, polling=polling, effects=rules.effects, decorated=True
            )
            # Discarded, not slept: this function is synchronous, so if it is
            # itself being called on an event-loop thread (a sync tool invoked
            # directly from a coroutine) it has no way to await the delay the
            # engine may have just stashed, and leaving it stashed would hand
            # it to whatever unrelated task looks next (see _PENDING_DELAY).
            # Off the loop, nothing was stashed here in the first place — the
            # engine used time.sleep directly — so this is a no-op there.
            _HOOKS.take_pending_delay()
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                _record_tool_error(tool_name, exc, retryable=retryable)
                raise

        return wrapper

    return decorate if fn is None else decorate(fn)


def _tool_rules(
    blocked: bool,
    max_calls: int | None,
    constraint: Callable | None,
    require_approval: Callable | None,
    reviewed: bool,
    effects=None,
) -> ToolRules:
    """The decorator's rule keywords, checked and packed. Raises on nonsense.

    Checked here rather than at the fold because this is where the customer
    wrote it: ``max_calls=0`` is a rule that can never be satisfied and
    ``constraint="under_500"`` is a rule that can never be asked, and both
    should fail the import that declares them, not the tool call that trips
    over them months later.
    """
    for label, value in (("blocked", blocked), ("reviewed", reviewed)):
        if not isinstance(value, bool):
            raise ValueError(
                f"@runbound.tool({label}=...) must be True or False, got {value!r}"
            )
    if blocked and reviewed:
        raise ValueError(
            "@runbound.tool(blocked=True, reviewed=True) contradicts itself: "
            "blocked refuses the tool, reviewed says it is deliberately unrestricted"
        )
    if max_calls is not None and (
        isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1
    ):
        raise ValueError(
            f"@runbound.tool(max_calls=...) must be an int >= 1, got {max_calls!r}"
        )
    for label, value in (
        ("constraint", constraint),
        ("require_approval", require_approval),
    ):
        if value is not None and not callable(value):
            raise ValueError(
                f"@runbound.tool({label}=...) must be a callable taking a "
                f"ToolCall and returning a bool, got {value!r}"
            )
    return ToolRules(
        blocked=blocked,
        max_calls=max_calls,
        constraint=constraint,
        require_approval=require_approval,
        reviewed=reviewed,
        effects=normalize_effects(effects),
    )


def _require_rule(tool_name: str, rules: ToolRules) -> None:
    """Under ``require_rules``, refuse to decorate a tool that states no rule.

    The CI gate. ``init(require_rules=True)`` names every unruled tool already
    imported; this catches every one declared afterwards, which in a normal
    module is all of them — ``init()`` runs at the top of the file and the
    tools are defined below it.
    """
    with _LOCK:
        engine = _ENGINE
    if engine is None or not getattr(engine.config, "require_rules", False):
        return
    if not rules.needs_rule():
        return
    if rules.stated():
        raise ValueError(_no_rule_message([], consequential=[(tool_name, rules.consequential())]))
    raise ValueError(_no_rule_message([tool_name]))


def _no_rule_message(tools: Sequence[str], consequential: Sequence = ()) -> str:
    """What ``require_rules`` says about the tools that fail it, and the fix for each."""
    parts = []
    if tools:
        parts.append(
            f"require_rules=True, but no rule is stated for: {', '.join(tools)}. "
            "Give each one a rule on its @runbound.tool (blocked=True, max_calls=, "
            "constraint=, require_approval=), or reviewed=True to say it was looked at "
            "and is deliberately unrestricted."
        )
    if consequential:
        named = ", ".join(
            f"{name} ({'/'.join(classes)})" if classes else name for name, classes in consequential
        )
        parts.append(
            f"require_rules=True, and these tools carry a consequential capability class "
            f"with no rule that can refuse them: {named}. reviewed=True is not enough for an "
            "action that cannot be undone or unpaid: give each one blocked=True, max_calls=, "
            "constraint= or require_approval=."
        )
    return " ".join(parts)


def _check_required_rules(config: GuardrailConfig) -> None:
    """Fail an ``init(require_rules=True)`` that already has unruled tools.

    Raised before anything is configured or started, so a process that cannot
    pass its own gate never begins guarding traffic under it.
    """
    if not config.require_rules:
        return
    unruled = _coverage.unruled_tools()
    consequential = _coverage.unenforced_consequential_tools()
    if unruled or consequential:
        raise ValueError(_no_rule_message(unruled, consequential))


def _announce_call(
    tool_name: str,
    args: tuple,
    kwargs: dict,
    *,
    polling: bool = False,
    effects: frozenset = frozenset(),
    decorated: bool = False,
) -> None:
    """Record and vet a call that is about to be made.

    Raises :class:`GuardrailTripped` when this call is the one that trips the
    session, or :class:`~runbound.exceptions.PolicyViolation` when it breaks
    the configured action policy — in both cases before the tool body runs,
    which is the whole point of doing this here rather than after.

    The event goes first: the attempt counts towards the loop window, the step
    count and this tool's own tally whether or not the policy then refuses it,
    and ``max_calls`` is measured against a tally that includes this attempt.
    A detector may therefore trip on the attempt before the policy sees it,
    which is the right order — the session is already over. ``loop_exempt`` is
    set when this call came from ``@runbound.tool(polling=True)`` or when
    ``tool_name`` is listed in ``loop_ignore_tools`` — either way the attempt
    still counts everywhere except the loop window.
    """
    _coverage.tool_called()
    _observe(
        kind="tool_call",
        tool_name=tool_name,
        args_hash=_args_hash(tool_name, args, kwargs),
        loop_exempt=polling or _is_loop_ignored(tool_name),
    )
    _admit_action(tool_name, args, kwargs, effects, decorated)


def _admit_action(
    tool_name: str, args: tuple, kwargs: dict, effects: frozenset, decorated: bool
) -> None:
    """Vet one attempted tool call: posture, a class rule, the envelope's own
    ``max_actions_per_run`` cap, then the customer's own action policy.

    One :func:`_active` resolution and one :class:`~runbound.policy.ToolCall`
    build for both checks — this folds what used to be two separate
    functions (the api's own ``_admit_posture`` and ``_enforce_policy``),
    each resolving the session and failing open on its own, into this one.
    :meth:`~runbound.engine.Engine.admit` (posture, a class rule, the action
    cap) runs first and :meth:`~runbound.engine.Engine.enforce_policy` runs
    last — so a call the posture already refuses is never handed to a
    ``require_approval`` callback — but the policy itself is unchanged: it
    still evaluates and reacts exactly as it always has.

    Looking the session up and building the ``ToolCall`` is fail-open like
    everything else here: a bug here logs and lets the call through. The
    refusals themselves — a posture denial, a class rule, the action cap, or
    a policy violation — are raised on purpose, from inside the engine.

    Whichever of the two calls below refuses (both raise
    ``GuardrailTripped`` — ``SafeModeViolation``/a plain trip from
    ``engine.admit``, ``PolicyViolation`` from ``engine.enforce_policy``)
    counts this attempt as refused, never executed; clearing both counts it
    as admitted and executed, right before the tool's own body runs. This is
    the one place either outcome is decided, so it is the one place either
    counter moves — a refused attempt must never read as having run, whether
    that refusal came from the posture, a capability rule, the action cap or
    the customer's own tool policy.
    """
    try:
        engine, state = _active()
        if engine is None or state is None:
            return
        call = ToolCall(tool_name, args, kwargs, state.key, dict(state.tags or {}))
    except Exception:
        _LOG.warning(
            "runbound could not apply the posture or the tool policy to %r; "
            "the call was allowed",
            tool_name,
            exc_info=True,
        )
        return
    try:
        engine.admit(state, kind="action", tool=tool_name, effects=effects, decorated=decorated)
        engine.enforce_policy(state, call)
    except GuardrailTripped:
        state.mark_action_refused()
        raise
    state.mark_action_admitted()


def posture() -> str:
    """The posture in force right now, by name.

    The current session's narrowing, the process's and the control plane's,
    tightened together: the strictest wins. ``"full"`` before :func:`init`, when nothing has
    narrowed anything, and on any error reading it.
    """
    try:
        engine, state = _active()
        if engine is None:
            return "full"
        return engine.effective_posture(state).name
    except Exception:
        _LOG.warning("runbound could not read the posture", exc_info=True)
        return "full"


def safe_mode() -> bool:
    """Is this agent operating with less than its full autonomy?

    Exactly ``posture() != "full"``. ``False`` before :func:`init`.
    """
    return posture() != "full"


def envelope(key: str | None = None) -> dict | None:
    """The execution envelope for ``key``'s session, or this one.

    Local execution safety is free and local, and the envelope is the
    single picture of it. The same object also rides the heartbeat so the
    plane can show it without a call of its own.

    The single answer to "what is this agent allowed to do right now",
    composed from :func:`budget`, the session's own counters, :func:`posture`
    and the configured class rules — never a separate source of truth::

        {"agent": "refunds-prod", "scope": "user:8842", "posture": "restricted",
         "budget": {"remaining": 4.20, "reserved": 0.80, "max_request": 4.20},
         "execution": {"steps_remaining": 13, "seconds_remaining": 41,
                       "actions_remaining": 4, "descendants_remaining": 7},
         "capabilities": {"read": "allow", "write": "allow", "external": "deny",
                           "financial": "deny", "destructive": "deny", "privileged": "deny"}}

    ``agent`` is ``config.agent`` where a future release states one,
    else ``config.service`` — the closest thing to a logical workload
    identity this release has. ``scope`` is the ``key`` asked for (``None``
    for the current, unkeyed default session). Every ``*_remaining`` field is
    ``None`` when its limit is not configured, exactly like ``budget()``'s
    own fields; ``max_request`` is what a single call may still cost and be
    admitted — today, exactly ``budget["remaining"]``, since that is the
    number :meth:`~runbound.engine.Engine._admit_budget` compares an
    estimate against. ``capabilities`` reads the same combined
    posture-and-class-rule verdict :func:`~runbound.api.tool`'s own door
    (:meth:`~runbound.engine.Engine.judge_action`) judges an action by, one
    class at a time, so this table can never disagree with what a tool call
    actually gets refused for.

    ``None`` before :func:`init`, and for a key with no session — the same
    fail-open answer as :func:`budget` and :func:`session_status`, which this
    always agrees with to the cent (they read the exact same numbers under
    the same lock).
    """
    try:
        if key is None:
            engine, state = _active()
        else:
            with _LOCK:
                engine = _ENGINE
                state = _REGISTRY.get(key)
        if engine is None or state is None:
            return None
        config = engine.config
        view = _budget_view(engine, state)
        with state.lock:
            turns = state.turns
            executed = state.executed_actions
            requested = state.requested_actions
            admitted = state.admitted_actions
            refused = state.refused_actions
            children = state.children
            run_started_at = state.run_started_at
        max_steps = engine._limit("max_steps")
        steps_remaining = None if max_steps is None else max(max_steps - turns, 0)
        seconds_remaining = None
        if config.max_session_seconds is not None:
            elapsed = time.monotonic() - run_started_at
            seconds_remaining = max(config.max_session_seconds - elapsed, 0.0)
        max_actions_per_run = engine._effective_max_actions_per_run()
        actions_remaining = (
            None
            if max_actions_per_run is None
            else max(max_actions_per_run - executed, 0)
        )
        descendants_remaining = (
            None
            if config.max_child_sessions is None
            else max(config.max_child_sessions - children, 0)
        )
        budget_dict = None
        if view is not None:
            budget_dict = {
                "remaining": view.remaining,
                "reserved": view.reserved,
                "max_request": view.remaining,
            }
        capabilities = {
            name: engine.judge_action(state, frozenset({name}))["verdict"]
            for name in CAPABILITIES
        }
        return {
            "agent": getattr(config, "agent", None) or config.service,
            "scope": key,
            "posture": engine.effective_posture(state).name,
            "budget": budget_dict,
            "execution": {
                "steps_remaining": steps_remaining,
                "seconds_remaining": seconds_remaining,
                "actions_remaining": actions_remaining,
                "descendants_remaining": descendants_remaining,
                # Four counters, never one -- actions_requested is what
                # the model asked for (tool_request events), actions_admitted
                # cleared posture/a class rule/the action cap and the tool
                # policy, actions_executed is the body actually running
                # (today always equal to admitted -- nothing sits between the
                # two -- kept separate because the contract names both), and
                # actions_refused is everything admission or the policy
                # turned away. tool_calls()/the loop window still count every
                # *attempt*, admitted or not; only max_actions_per_run reads
                # actions_executed.
                "actions_requested": requested,
                "actions_admitted": admitted,
                "actions_executed": executed,
                "actions_refused": refused,
            },
            "capabilities": capabilities,
        }
    except Exception:
        _LOG.warning("runbound could not build the execution envelope", exc_info=True)
        return None


def enter_safe_mode(reason: str = "manual", posture: str = "restricted") -> None:
    """Narrow the whole process to ``posture`` by hand.

    Every ``@runbound.tool`` whose declared classes that posture denies is
    refused before its body runs until :func:`exit_safe_mode`; model calls go
    on. For one session only, call ``enter_safe_mode()`` on the state
    :func:`session` yields. Raises ``ValueError`` for a posture name nothing
    defines. A no-op before :func:`init`.
    """
    with _LOCK:
        engine = _ENGINE
    if engine is not None:
        engine.enter_safe_mode(reason, posture)


def exit_safe_mode() -> None:
    """Put the process back to ``full``, lifting only its own narrowing.

    A session's posture and the plane's are their own;
    :func:`runbound.clear` lifts a session's.
    """
    with _LOCK:
        engine = _ENGINE
    if engine is not None:
        engine.exit_safe_mode()


# --- wrapping clients -------------------------------------------------------


def wrap(client: Any) -> Any:
    """Guard an LLM client's calls, in place, and return the same client.

    Recognizes clients by shape, not by type: ``.chat.completions.create``
    and/or ``.responses.create`` (OpenAI and every OpenAI-compatible
    endpoint), or ``.messages.create`` (Anthropic). Every surface a client
    exposes is patched — guarding chat completions while the Responses API
    flows past uncounted would report success while counting nothing — and
    which ones were patched is logged at INFO. Because the methods are patched
    on the client's own resource objects, the returned client *is* the one
    passed in — everything else about it, including attributes runbound has
    never heard of, is untouched.

    Wrapping an already-wrapped client is a no-op. An unrecognized client
    raises ``ValueError``: silently guarding nothing would be worse. If the
    patch itself fails, the failure is logged and the client is returned
    unguarded — the host's calls keep working.

    A wrapped client also reports what it *cannot* do: a call that raises is
    recorded as a failed one (a run of them is a retry storm) and counted
    against that provider's circuit. Under ``on_provider_failure="open"`` the
    next call to a provider whose circuit has opened raises
    :class:`~runbound.exceptions.CircuitOpen` before the request goes out;
    under the default it is only reported.

    Sync and async clients are both supported, and ``async def`` methods keep
    their ``async def`` signature. Streamed responses are guarded too: chunks
    pass through untouched and one call is recorded when the stream ends (with
    usage when the provider supplies it — for OpenAI, set
    ``stream_options={"include_usage": True}``); a stream abandoned before it
    is exhausted, closed or exited still counts, as one ``partial`` call, once
    the stream proxy is garbage collected (see :meth:`_Hooks.abandoned`).
    """
    for provider in PROVIDERS:
        if not provider.matches(client):
            continue
        if provider.is_wrapped(client):
            return client
        try:
            provider.install(client, _record_llm_call, hooks=_HOOKS)
            _coverage.client_wrapped()
        except Exception:
            _LOG.warning(
                "runbound could not wrap %s; it will run unguarded",
                type(client).__name__,
                exc_info=True,
            )
        return client

    raise ValueError(
        f"runbound.wrap: unrecognized client {type(client).__name__!r}; expected an "
        "object with .chat.completions.create or .responses.create (OpenAI-shaped), "
        "or .messages.create (Anthropic-shaped)"
    )
