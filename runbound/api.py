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
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

from . import _coverage, autowrap, responses
from .config import GuardrailConfig
from .engine import (
    BUDGET_DETECTOR,
    CIRCUIT_DETECTOR,
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
from .events import Anomaly, Event
from .exceptions import CircuitOpen, GuardrailTripped
from .plane_types import ExitDelta, PlaneStatus, key_hash
from .policy import ToolCall
from .pricing import price_call, price_for
from .shared import LocalState, build as _build_shared
from .state import SessionState
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
#: session id: ``(seq, spend_usd, tokens, steps, tool_calls)``. What makes an
#: exit a *delta* — two workers on one key each report their own share and the
#: plane adds them up. Read and written under ``_LOCK``, emptied with the
#: registry, and capped so a process churning through keys cannot grow it
#: without bound.
_EXITS: "OrderedDict[str, tuple]" = OrderedDict()
_EXIT_MEMO_MAX = 4096

#: When the "the fleet is halted" warning was last logged, under
#: ``on_halt="warn"``. One line a minute, however many blocks are refused.
_HALT_WARNED_AT: float | None = None

#: How long between those warnings.
_HALT_WARN_INTERVAL_S = 60.0

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
#: A refused end-user (or a retrying agent) walks into the same wall on every
#: attempt, and the on-call wants to hear about the wall once. Emptied with the
#: registry, under ``_LOCK``.
_FANOUT_ALERTED: set[tuple[str, str]] = set()

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


#: Fields ``init()`` used to accept for outbound delivery, retired in Wave 31
#: now that the SDK does not send alerts at all — the control plane routes
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
    """Raise a kind ``ValueError`` for any Wave-31-retired ``init()`` keyword.

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
    fields Wave 31 retired (``slack_webhook``, ``pagerduty_routing_key``,
    ``webhook_url``, ``webhook_secret``, ``link_template``) raise a
    ``ValueError`` naming where the setting went instead of that bare
    ``TypeError`` — delivery is not a setting on this call any more at all.

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
        _start_session(config)
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
        _start_session(_ENGINE.config)


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
    same code derives the same id for the same end-user: an incident that
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
    _FANOUT_ALERTED.clear()
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
    global _LOOP_IGNORE_TOOLS
    with _LOCK:
        _ENGINE = None
        _SESSION = None
        _LOOP_IGNORE_TOOLS = ()
        previous, _SHARED = _SHARED, LocalState()
        _HALT_WARNED_AT = None
        _forget_sessions()
    previous.stop()
    _CURRENT.set(None)
    _NO_PROVIDER_ANNOUNCED = False
    autowrap.unpatch_all()
    _coverage.reset_for_tests()
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
         "refusals": "default"}                 # highest refusal source in effect

    Counters are process-lifetime: :func:`init` and :func:`reset` do not clear
    them, because "has this process ever seen traffic?" is not a question a new
    session re-asks. Works before :func:`init` — everything reads zero — and
    fails open to zeros rather than raising.

    ``"refusals"`` is the odd one out — not a counter, but the highest of
    :mod:`runbound.responses`'s three sources currently answering a
    :class:`~runbound.exceptions.GuardrailTripped`'s ``.refusal``:
    ``"plane"`` when a control-plane profile is in effect, else ``"local"``
    when ``GuardrailConfig.refusals`` was set, else ``"default"`` (BUILTIN).
    """
    try:
        report = _coverage.snapshot(autowrap.patched(), **_coverage_kwargs())
        report["refusals"] = _refusals_source()
        return report
    except Exception:
        _LOG.warning("runbound could not read its own coverage", exc_info=True)
        zeros = _coverage.zeros()
        zeros["refusals"] = "default"
        return zeros


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
    end-user is ever stopped. At most ``max_sessions`` keys are kept; the
    least recently used is dropped, and re-entering a dropped key simply
    starts it over.

    Blocks nest: the enclosing session is restored on exit. The binding is a
    context variable, so a thread or asyncio task that opens its own block is
    unaffected by any other. Yields the session, or ``None`` before
    :func:`init` — where the block is inert and observes nothing, exactly as
    the rest of the SDK is.

    Under ``on_anomaly="raise"``, entering the block for a key that has already
    tripped raises :class:`~runbound.exceptions.GuardrailTripped` *before the
    body runs* — a blocked end-user costs the business nothing from their next
    message on. :func:`clear` is how that key is let back in — or, with
    ``latch_ttl_seconds`` configured, simply waiting out the window.

    Entering is also where the fan-out limits are enforced. With
    ``max_session_depth``, ``max_child_sessions`` or ``max_active_sessions``
    set, a block that would take the run past one of them raises
    :class:`~runbound.exceptions.GuardrailTripped` before its body runs —
    whatever ``on_anomaly`` says, because those are numbers the customer
    stated, and latching nothing, because what was wrong is the shape of the
    run rather than this end-user.

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
    # what max_session_lifetime_seconds is for (T133).
    state.run_started_at = time.monotonic()
    _sync_entry(key, state)
    _refuse_fanout(state)
    _refuse_if_tripped(state)
    _count_entry()
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)
        _count_exit()
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
            return _registered(key, tags, engine.config)
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
    :class:`~runbound.exceptions.GuardrailTripped` for a halt under
    ``on_halt="raise"``, which must reach the caller before the body runs.
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
            else:
                # A refusal may carry the latch that caused it, and with it
                # real fleet facts (offsets, strikes, generation): adopt them
                # first, so ``_refuse_if_tripped`` refuses the block the way
                # it always has, ``origin="fleet"`` and all. But the refusal
                # stands on its own: a latch that did not take — expired ttl,
                # a shape this worker cannot read — must not turn the plane's
                # "no" into an admission (review finding, Wave 24).
                if getattr(decision, "latch", None):
                    _apply_decision(key, state, decision)
                refused = decision
        halted = (decision is not None and decision.halt) or shared.halted()
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
    if halted:
        _refuse_halted(engine, state)


def _refuse_plane_decision(engine: Engine, state: SessionState, decision) -> None:
    """Refuse at the door when the plane's answer itself is a refusal.

    Two answers arrive this way: the org's daily budget is spent (detector
    ``budget``, ``origin="plane"``, ``rule="org_budget"``, decided by the
    plane's atomic entry script), and — under ``on_plane_loss="refuse"`` —
    the plane could not be asked at all (detector ``plane``). Until Wave 24
    the api read only the fleet facts on
    a decision (offsets, strikes, latch) and never ``allow``, so a plane that
    said "no" was silently overruled by the worker; found by T61's review.

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
        anomaly = Anomaly(
            detector=str(refusal.get("detector") or "plane"),
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


def _apply_remote_latch(state: SessionState, latch) -> None:
    """Stop this session on a trip another worker made, if it is still running.

    The remote anomaly is latched exactly as a local one would be, with what
    is left of its ttl as this session's own expiry — so ``_refuse_if_tripped``
    refuses the block, ``is_tripped()`` reports the real reason, and the
    end-user is let back in when the fleet's cooldown runs out rather than when
    this worker happened to hear about it. A latch with nothing left on it is
    not applied at all.
    """
    anomaly = _remote_anomaly(latch)
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


def _remote_anomaly(latch) -> Anomaly | None:
    """The anomaly a remote latch describes, or ``None`` if it describes none.

    Tolerant by design: the plane is a different program on a different release
    cycle, and a field it renames must cost this worker a default, not a
    refused session it cannot explain.
    """
    if not isinstance(latch, dict):
        return None
    details = latch.get("details")
    details = dict(details) if isinstance(details, dict) else {}
    details.setdefault("origin", "fleet")
    return Anomaly(
        detector=str(latch.get("detector") or "fleet"),
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
    """Describe the halt this worker is enforcing. Carries no raw key."""
    config = engine.config
    key = getattr(state, "key", None)
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

    ``steps_delta`` carries model turns (T134: an agent step is a turn, not
    every recorded event) — ``state.turns`` here, not ``state.event_count``.
    """
    with state.lock:
        spend = float(state.total_cost_usd)
        tokens = int(state.total_tokens)
        steps = int(state.turns)
        tools = dict(state.tool_calls)
    session_id = getattr(state, "session_id", "")
    with _LOCK:
        seq, last_spend, last_tokens, last_steps, last_tools = _EXITS.get(
            session_id, (0, 0.0, 0, 0, {})
        )
        seq += 1
        _EXITS[session_id] = (seq, spend, tokens, steps, tools)
        _EXITS.move_to_end(session_id)
        while len(_EXITS) > _EXIT_MEMO_MAX:
            _EXITS.popitem(last=False)
    return ExitDelta(
        key_hash=key_hash(key),
        seq=seq,
        spend_delta_usd=max(0.0, spend - last_spend),
        tokens_delta=max(0, tokens - last_tokens),
        steps_delta=max(0, steps - last_steps),
        tool_calls=_tool_delta(tools, last_tools),
    )


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
    worker knows about an end-user, this one what the whole fleet does. Reads
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
    """One refused block, in the terms an on-call human asks in."""
    return Anomaly(
        detector=FANOUT_DETECTOR,
        severity="critical",
        message=message,
        details={
            "session_id": getattr(state, "session_id", ""),
            "key": getattr(state, "key", None),
            "tags": dict(getattr(state, "tags", None) or {}),
            "rule": rule,
            "count": count,
            "limit": limit,
        },
    )


def _alert_fanout(engine: Engine, anomaly: Anomaly, state: SessionState) -> None:
    """Page once per (session, rule), then stay quiet however often it retries.

    An agent that walks into a fan-out limit walks into it on every attempt,
    and the on-call learns nothing from the second one. The engine dedups per
    (session, detector, severity) on top of this, so a session that later
    breaks a *second* fan-out rule is refused as firmly as ever but does not
    page again. Fail-open, like every other notification path: a refusal is
    never lost to a broken alerter.
    """
    try:
        memo = (getattr(state, "session_id", ""), anomaly.details["rule"])
        with _LOCK:
            if memo in _FANOUT_ALERTED:
                return
            _FANOUT_ALERTED.add(memo)
        engine.notify_door(state, anomaly)
    except Exception:
        _LOG.warning("runbound could not alert on a fan-out refusal", exc_info=True)


# --- the abuse ladder's rollover --------------------------------------------


def _rolled_over(key: str, state: SessionState) -> SessionState:
    """``key``'s session, replaced first if the ladder closed the old one.

    The ladder's last rung latches a session with ``action="rollover"``: the
    detector has decided this end-user's session is over, and retiring it is
    the api's half of the deal — a new generation of the key, one more strike,
    half the allowance, and a cooldown to serve before the fresh session runs.
    The strike outlives the session it was earned on, so a repeat offender
    cannot be forgiven by an eviction.

    Only entry rolls a key over. An event arriving on a closed session simply
    re-applies that session's latch, as any latched session does, so the swap
    always happens between requests rather than in the middle of one. And only
    ``on_spike="limit"`` has a ladder at all: every other configuration walks
    straight past this, into the entry check it has always had.

    Fail-open: anything that goes wrong is logged and the old state is entered
    exactly as it would have been without the ladder.
    """
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is None or engine.config.on_spike != "limit":
            return state
        latched = _latched(state, engine.config)
        if latched is None:
            _cooldown_served(state)
            return state
        strikes = _rollover_strikes(state, latched)
        if strikes is None:
            return state
        return _roll_over(key, state, latched, strikes, engine.config)
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
    all through the cooldown costs the end-user nothing extra.
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


def _cooldown_served(state: SessionState) -> None:
    """Retire a cooldown this session has now outlived.

    ``latch_ttl_override`` is one cooldown's expiry, not a policy the session
    keeps: once the session is running again its next latch is an ordinary
    one, permanent unless ``latch_ttl_seconds`` says otherwise.
    """
    with state.lock:
        if state.tripped_by is None:
            state.latch_ttl_override = None


def _roll_over(
    key: str,
    old: SessionState,
    anomaly: Anomaly,
    strikes: int,
    config: GuardrailConfig,
) -> SessionState:
    """Retire ``old`` and put the key's next session in its place, latched.

    Either the swap happens whole — new generation, strike recorded, fresh
    session registered and latched — or the key is left exactly as it was
    found and the failure is re-raised for the caller to fail open on.
    """
    strikes = min(strikes, config.spike_max_strikes)
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
    _latch_rollover(fresh, old, key, anomaly, strikes, config)
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
) -> None:
    """Start the fresh session stopped: a cooldown, or the final block.

    Below ``spike_max_strikes`` the session is latched on the rollover anomaly
    itself for ``spike_cooldown_seconds`` — the end-user is refused for that
    long and then served again, on tighter terms. At the last strike there is
    no expiry: the key stays blocked until the business clears it.

    ``old``'s ladder facts — its history, heal count, and the timestamps of
    its last limit and close — move to ``fresh`` here, with one more entry
    appended recording the rollover (or block) itself: the key's "why was I
    limited" story is continuous across a strike, even though the session
    object underneath it was just replaced. Only the baseline and the current
    allowance start fresh, because a new session judges its own calls from
    scratch.
    """
    blocked = strikes >= config.spike_max_strikes
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
        fresh.ladder_history = history
        fresh.healed_times = healed_times
        fresh.spike_closed_at = closed_at
        fresh.spike_trigger = trigger
        fresh.spike_limited_at = limited_at
        fresh.record_ladder_transition(old_level, 0, "blocked" if blocked else "rollover")
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
        tripped = _latched(state, engine.config)
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
    work for an end-user, or to render "you have hit your limit" without
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
        return _latched(state, engine.config)
    except Exception:
        _LOG.warning("runbound could not read the latch for %r", key, exc_info=True)
        return None


def session_status(key: str) -> dict | None:
    """Where a keyed session stands right now, or ``None`` if there is none.

    Everything the abuse ladder knows about one end-user, in the terms a
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
        tripped = _latched(state, engine.config)
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
    return {
        "trigger": None
        if not trigger
        else {
            "metric": trigger["metric"],
            "value": trigger["value"],
            "median": trigger["median"],
            "factor": trigger["factor"],
            "at_s_ago": _s_ago(now, trigger["at"]),
        },
        "limited_at_s_ago": _s_ago(now, state.spike_limited_at),
        "allowance_start": state.spike_allowance_start,
        "healed_times": state.healed_times,
        "closed_at_s_ago": _s_ago(now, state.spike_closed_at),
        "baseline": None
        if baseline is None
        else {"duration_s": baseline[0], "output_tokens": baseline[1]},
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
    """Forget ``key``'s session entirely: explicit forgiveness for one user.

    The business decides when a blocked end-user is let back in — after a
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
            _STRIKES.pop(key, None)
            _DOOR_REFUSALS.pop(key, None)
            _EXITS.pop(_keyed_id(key), None)
            _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        if state is not None:
            with state.lock:
                state.record_ladder_transition(state.spike_level, 0, "cleared")
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


def _record_tool_error(tool_name: str, exc: BaseException) -> None:
    """Emit a ``tool_error`` event for a tool that raised.

    A trip discovered while recording the failure is logged and dropped: the
    tool's own exception is the one the caller needs, and swapping it for
    :class:`GuardrailTripped` would hide the real failure.
    """
    try:
        detail = str(exc)[:ERROR_MAX_CHARS]
    except Exception:
        detail = type(exc).__name__
    try:
        _observe(kind="tool_error", tool_name=tool_name, error=detail)
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
    *,
    partial: bool = False,
    tokens_estimated: bool = False,
) -> None:
    """Price a model call and emit its ``llm_call`` event.

    ``duration_s`` is how long the call took and ``tokens_reasoning`` the
    thinking tokens it burned on top of ``tokens_out``; both default to the
    "not measured" zero, so a caller that only knows the token counts still
    reports a complete event.

    Priced by :func:`~runbound.pricing.price_call` under
    ``config.on_unpriced_model``: an unpriced model is $0.00 (warned once per
    model, by default) under "zero", the ``unpriced_price_per_1m_usd`` fallback
    under "estimate", and — since the door (:meth:`_Hooks.before`) is what
    actually refuses under "refuse" — whichever of those two a call that
    reaches here anyway falls back to. The event's ``priced`` field is
    ``"estimated"`` exactly when the fallback pair was used.

    ``partial`` and ``tokens_estimated`` are for a call that never actually
    finished — an abandoned stream reported by :meth:`_Hooks.abandoned` — and
    are carried straight onto the event; every other caller leaves them at
    their default of ``False``.
    """
    # Counted before the engine check: the coverage report asks whether a
    # sensor fired, which is true whether or not anything was configured to
    # listen.
    _coverage.guarded_call()
    with _LOCK:
        engine = _ENGINE
    if engine is None:
        return
    cost, estimated = price_call(
        model,
        tokens_in,
        tokens_out,
        engine.config.custom_prices,
        on_unpriced_model=engine.config.on_unpriced_model,
        unpriced_price_per_1m_usd=engine.config.unpriced_price_per_1m_usd,
    )
    _observe(
        kind="llm_call",
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        duration_s=duration_s,
        tokens_reasoning=tokens_reasoning,
        priced=("estimated" if estimated else None),
        partial=partial,
        tokens_estimated=tokens_estimated,
    )


# --- the wrappers' hooks ----------------------------------------------------


class _Hooks:
    """What a client wrapper calls around a provider request.

    The wrappers know how to read someone else's SDK; everything about what a
    failure *means* lives here and in the engine. The moments::

        before(provider, model=None)         # may raise CircuitOpen/GuardrailTripped
        success(provider)                    # the call worked
        error(model, exc, duration_s, provider)  # the call failed
        release(provider)                    # the call is over, however it ended
        tool_request(name, args_hash)        # the model asked for a tool
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
    :class:`~runbound.exceptions.CircuitOpen`, the in-flight and
    unpriced-model refusals from :meth:`before`, and a
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

    def before(self, provider: str, model: str | None = None) -> None:
        """Vet a call before it goes out: the circuit, the price, then the cap.

        The circuit is the retry-storm fix: failing fast costs a microsecond,
        while the call it replaces costs a timeout and, often, a retry loop
        around it. Only ``on_provider_failure="open"`` refuses anything; under
        the default the circuit is counted and reported and this is a no-op.

        runbound does not fall back to another provider — routing is the
        application's decision — so the app catches
        :class:`~runbound.exceptions.CircuitOpen` (a
        :class:`~runbound.exceptions.GuardrailTripped`) and does what it
        likes. No alert is sent here: the on-call was paged when the circuit
        opened, not once per refused call.

        Then, when ``model`` is given, ``on_unpriced_model="refuse"``: a model
        with no static or custom price is refused here, before the request
        goes out, whatever ``on_anomaly`` says — the customer chose this mode
        precisely to stop such calls rather than merely count them. Latches
        nothing (nothing ran) and is alerted once per model per Engine (the
        alert dedup set is rebuilt by :func:`init`, so this is not truly
        process-wide), not per session, because an unpriced model is a fact
        about the model. With
        no ``model`` (an older wrapper call site, or a provider that only
        reveals it in the response) this step is skipped; pricing still
        catches the call after the fact when it is recorded.

        Then ``max_inflight_calls``: on a self-hosted endpoint the scarce
        resource is GPU concurrency, not dollars, and a call that would take
        this label past the cap is refused here — before the request goes out
        and whatever ``on_anomaly`` says, because the number is one the
        customer stated. A refused call takes no slot; an allowed one takes
        exactly one, given back by :meth:`release`.
        """
        try:
            with _LOCK:
                engine = _ENGINE
            if engine is None:
                return
            allowed = engine.circuit_allows(provider)
            anomaly = None if allowed else _circuit_open_anomaly(engine, provider)
        except Exception:
            _LOG.warning(
                "runbound could not check the circuit for %r; the call proceeds",
                provider,
                exc_info=True,
            )
            return
        if anomaly is not None:
            raise CircuitOpen(anomaly, provider)
        _check_unpriced_refusal(engine, model)
        _reserve_inflight(engine, provider)

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
            )
        except GuardrailTripped:
            _LOG.debug(
                "runbound: a trip fired while recording an abandoned stream "
                "call; swallowed (called from a finalizer, nothing to catch it)",
                exc_info=True,
            )
        except Exception:
            _LOG.debug("runbound: could not record an abandoned stream call", exc_info=True)

    def success(self, provider: str) -> None:
        """Report a call that worked: an open circuit closes. Never raises."""
        _coverage.provider_seen(provider)
        try:
            with _LOCK:
                engine = _ENGINE
            if engine is None:
                return
            engine.record_llm_success(provider)
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
        try:
            engine, state = _active()
            if engine is None or state is None:
                return
            engine.record_llm_error(state, model, exc, duration_s, provider)
        except GuardrailTripped:
            raise
        except Exception:
            _LOG.warning("runbound failed to record a failed model call", exc_info=True)

    def tool_request(self, name: str, args_hash: str | None) -> None:
        """Record a tool call the model asked for, before anyone dispatches it.

        The hash is the wrapper's, already in the ``"req:"`` namespace, so a
        model asking for the same tool with the same arguments over and over is
        a loop even when the developer dispatches those calls by hand and
        runbound never sees a ``@tool``. ``loop_exempt`` is set when ``name``
        is listed in ``loop_ignore_tools`` — the ``@runbound.tool`` decorator
        applies the same config by name, but a model *request* has no
        decorator to carry ``repeatable=True`` on, so config is the only way
        to mark one exempt here.
        """
        _observe(
            kind="tool_request",
            tool_name=name,
            args_hash=args_hash,
            loop_exempt=_is_loop_ignored(name),
        )


#: The one hooks object; wrappers hold it, tests may call it directly.
_HOOKS = _Hooks()


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


def _check_unpriced_refusal(engine: Engine, model: str | None) -> None:
    """Refuse before the request goes out when the model has no price.

    A no-op unless ``on_unpriced_model="refuse"`` and ``model`` is known.
    Mirrors :func:`_reserve_inflight`: ignores ``on_anomaly`` (the customer
    stated this rule), latches nothing (nothing ran yet), and pages once per
    model per Engine (the alert is deduped on a set :func:`init` rebuilds, so
    this is not truly process-wide) rather than per session or per call.
    """
    try:
        if engine.config.on_unpriced_model != "refuse" or not model:
            return
        if price_for(model, engine.config.custom_prices) is not None:
            return
        anomaly = _unpriced_model_anomaly(model)
    except Exception:
        _LOG.warning(
            "runbound could not check pricing for model %r; the call proceeds",
            model,
            exc_info=True,
        )
        return
    _alert_unpriced_model(engine, anomaly)
    raise GuardrailTripped(anomaly)


def _unpriced_model_anomaly(model: str) -> Anomaly:
    """Describe the refusal :func:`_check_unpriced_refusal` is about to raise."""
    return Anomaly(
        detector=BUDGET_DETECTOR,
        severity="critical",
        message=(
            f"Model {model!r} has no known price and on_unpriced_model=\"refuse\"; "
            "refusing before the request goes out"
        ),
        details={"reason": "unpriced_model", "model": model},
    )


def _alert_unpriced_model(engine: Engine, anomaly: Anomaly) -> None:
    """Page once per model, then stay quiet however often it is refused.

    Fail-open, like :func:`_alert_inflight`: a refusal is never lost to a
    broken alerter.
    """
    try:
        with _LOCK:
            state = _SESSION
        state = _CURRENT.get() or state
        if state is not None:
            engine.notify_door(state, anomaly)
        _LOG.warning("[runbound] %s", anomaly.message)
    except Exception:
        _LOG.warning(
            "runbound could not alert on an unpriced-model refusal", exc_info=True
        )


def _circuit_open_anomaly(engine: Engine, provider: str) -> Anomaly:
    """Describe the refusal a wrapper is about to raise."""
    state = engine.circuit.state(provider)
    cooldown = engine.config.circuit_cooldown_seconds
    return Anomaly(
        detector=CIRCUIT_DETECTOR,
        severity="critical",
        message=(
            f"Provider {provider!r} circuit is open; failing fast "
            f"(cooldown {cooldown:.0f}s)"
        ),
        details={
            "provider": provider,
            "host": provider_host(provider),
            "state": state,
            "cooldown_seconds": cooldown,
        },
    )


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

    Raises :class:`~runbound.exceptions.GuardrailTripped` when this call is
    the one that trips the session — a budget spent, a storm confirmed — and
    is otherwise fail-open and inert before :func:`init`.
    """
    if error is not None:
        _HOOKS.error(model, error, duration_s, provider)
        return
    _record_llm_call(model, tokens_in, tokens_out, duration_s)
    _HOOKS.success(provider)


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
    (``model`` is known up front here, so ``"refuse"`` can act on it) and the
    in-flight cap *before* the body runs, and records what came out through
    the same path :func:`wrap` uses: on success ``tokens(result)`` — a step
    with no tokens when no callback is given, or when the callback fails —
    and on an exception an ``llm_error`` plus a mark against ``provider``'s
    circuit, before the original exception is re-raised untouched.

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
                _HOOKS.before(provider, model)
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
                    _release_inflight(provider)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _HOOKS.before(provider, model)
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
    fn: Callable | None = None, *, name: str | None = None, repeatable: bool = False
) -> Callable:
    """Record every call to a tool. Usable bare or with ``(name=..., ...)``.

    ::

        @runbound.tool
        def search(query): ...

        @runbound.tool(name="web_search")
        def search(query): ...

        @runbound.tool(repeatable=True)
        def poll_job_status(job_id): ...

    The ``tool_call`` event is emitted *before* the function runs, so a loop is
    broken on the repeat that would have made it — with ``on_anomaly="raise"``
    the tool body never executes on the tripping call. If the tool raises, a
    ``tool_error`` event is emitted and the tool's own exception is re-raised
    unchanged.

    ``repeatable=True`` (equivalently, listing this tool's name in
    ``loop_ignore_tools``) marks a tool that is *supposed* to run with the
    same arguments over and over — polling a job's status, say — so its calls
    never feed the loop window and can never trip the loop detector, however
    many times they repeat. They still count towards ``tool_calls()`` and any
    action policy's ``max_calls``: this marks polling, it does not raise a
    limit, so it is not the setting to reach for to dodge a real loop.

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

    def decorate(func: Callable) -> Callable:
        tool_name = name or getattr(func, "__name__", "<tool>")
        _coverage.tool_decorated(tool_name)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _announce_call(tool_name, args, kwargs, repeatable=repeatable)
                delay = _HOOKS.take_pending_delay()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    _record_tool_error(tool_name, exc)
                    raise

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _announce_call(tool_name, args, kwargs, repeatable=repeatable)
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
                _record_tool_error(tool_name, exc)
                raise

        return wrapper

    return decorate if fn is None else decorate(fn)


def _announce_call(
    tool_name: str, args: tuple, kwargs: dict, *, repeatable: bool = False
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
    set when this call came from ``@runbound.tool(repeatable=True)`` or when
    ``tool_name`` is listed in ``loop_ignore_tools`` — either way the attempt
    still counts everywhere except the loop window.
    """
    _coverage.tool_called()
    _observe(
        kind="tool_call",
        tool_name=tool_name,
        args_hash=_args_hash(tool_name, args, kwargs),
        loop_exempt=repeatable or _is_loop_ignored(tool_name),
    )
    _enforce_policy(tool_name, args, kwargs)


def _enforce_policy(tool_name: str, args: tuple, kwargs: dict) -> None:
    """Apply the configured action policy to one attempted tool call.

    Inert before :func:`init` — no policy is configured, so no
    :class:`~runbound.policy.ToolCall` is even built. Working out *which*
    session to judge the call against is fail-open like everything else here;
    the judgment itself is not wrapped, because the engine already logs and
    swallows anything that goes wrong evaluating a policy and catching here
    would swallow the refusal it raises on purpose.

    The arguments are handed straight to the customer's own predicates and go
    no further: the tags are copied so a predicate cannot rewrite the session's
    identity, and nothing about the call is stored, hashed or logged here.
    """
    try:
        engine, state = _active()
        if engine is None or state is None:
            return
        call = ToolCall(tool_name, args, kwargs, state.key, dict(state.tags or {}))
    except Exception:
        _LOG.warning(
            "runbound could not apply the tool policy to %r; the call was allowed",
            tool_name,
            exc_info=True,
        )
        return
    engine.enforce_policy(state, call)


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
