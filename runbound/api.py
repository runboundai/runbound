"""The public API: ``init``, ``reset``, ``session``, ``tool``, ``tools``,
``coverage``, ``current_session``, ``tool_calls``, ``events``, ``decisions``,
``plane_status`` and ``assert_guarded``.

This module owns runbound's one documented global: the engine and session
created by :func:`init`. Agents are threaded, so every read or mutation of that
global happens under ``_LOCK`` (held only for pointer swaps and registry
bookkeeping — never across policy or user code).

Work done inside a :func:`session` block is accounted to that key's own
:class:`SessionState` instead, tracked in a context variable so threads and
asyncio tasks each see their own without cooperating.

Two rules hold everywhere below:

* **Inert until initialized.** Without :func:`init`, decorated tools run exactly
  as if runbound were not installed.
* **Fail-open.** Anything that goes wrong while observing is logged to the
  ``"runbound"`` logger and swallowed. The deliberate exception is
  :class:`~runbound.exceptions.GuardrailTripped`.
"""

import asyncio
import contextlib
import contextvars
import dataclasses
import functools
import hashlib
import inspect
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from typing import Any
from uuid import uuid4

from . import _coverage, approvals as approvals_module, hooks as hooks_module, local_events, results as results_module, spans
from .config import RunboundConfig
from .engine import ERROR_MAX_CHARS, Engine
from .events import Event
from .exceptions import GuardrailTripped
from .plane_types import PlaneStatus
from .policy import ToolCall, ToolRules, normalize_effects
from .shared import LocalState, build as _build_shared
from .state import SessionState

_LOG = logging.getLogger("runbound")

_SESSION_ID_CHARS = 12
_KEYED_ID_CHARS = 10

_LOCK = threading.Lock()
_ENGINE: Engine | None = None
_SESSION: SessionState | None = None

#: What the plane says. :class:`~runbound.shared.LocalState` until an
#: :func:`init` with a plane replaces it, so every read below is safe before
#: :func:`init`. Swapped under ``_LOCK``; its own methods are called outside it.
_SHARED = LocalState()

#: Keyed sessions, most-recently-used last; capped at ``config.max_sessions``.
_REGISTRY: "OrderedDict[str, SessionState]" = OrderedDict()

#: Keyed :func:`session` blocks entered and not yet exited, process-wide.
_ACTIVE = 0

#: The session of the innermost enclosing :func:`session` block, per thread and
#: per asyncio task. ``None`` means "use the default session".
_CURRENT: contextvars.ContextVar[SessionState | None] = contextvars.ContextVar(
    "runbound_session", default=None
)

#: Mixed into every :func:`_args_hash` digest. Generated once at import and kept
#: across :func:`reset`, so a hash is a stable equality token within one process
#: without being a fingerprint that means anything outside it.
_HASH_SALT: bytes = secrets.token_bytes(16)


# --- lifecycle --------------------------------------------------------------


def init(**kwargs: Any) -> None:
    """Configure runbound and start a session. Call once, at startup.

    Accepts every :class:`~runbound.config.RunboundConfig` field as a keyword
    argument. Configuration errors raise ``ValueError`` (and unknown options
    ``TypeError``) on purpose: a misconfigured SDK must fail loudly here rather
    than quietly fail to guard anything in production.

    ``require_rules=True`` adds one more: every ``@runbound.tool`` already
    imported that states no rule is named in a ``ValueError`` here, and every
    one declared afterwards raises at its own decoration — the CI gate, since in
    a normal module the tools are defined below this call, not above it.

    Calling it again reconfigures the SDK and starts a fresh session and an
    empty keyed-session registry.

    With a ``control_plane_url`` (or a ``token`` once a hosted plane exists)
    this is also where the plane link starts: a client, the telemetry exporter
    (unless ``export_events=False``) and a heartbeat thread. None of that is on
    the call path, none of it can fail an ``init()``, and a second ``init()``
    stops the first one's threads.
    """
    global _ENGINE, _SHARED
    config = RunboundConfig(**kwargs)
    config.validate()
    local_events.configure(config.on_event)
    from . import __version__

    spans.configure(config, __version__)
    _check_required_rules(config)
    if config.plane_mode == "hosted":
        _LOG.info("[runbound] control plane: hosted")
    elif config.plane_mode == "self_hosted":
        _LOG.info("[runbound] control plane: self-hosted at %s", config.control_plane_url)
    shared = _build_shared(config)
    engine = Engine(config, observers=shared.observers(), shared=shared)
    with _LOCK:
        _ENGINE = engine
        previous, _SHARED = _SHARED, shared
        _forget_sessions()
        _start_session(config)
    # Outside the lock on purpose: joining a background thread and saying hello
    # to a control plane are none of them things to do while holding the lock
    # every guarded call needs.
    previous.stop()
    shared.start(engine)


def reset() -> None:
    """Start a fresh session, keeping the current configuration and engine.

    Use it between agent runs in a long-lived process: counters go back to zero
    and every keyed session is forgotten. A no-op if :func:`init` was never
    called.
    """
    with _LOCK:
        if _ENGINE is None:
            return
        _forget_sessions()
        _start_session(_ENGINE.config)


def current_session() -> SessionState | None:
    """The session work is being accounted to right now.

    Inside a :func:`session` block that is the block's keyed session; otherwise
    the default session created by :func:`init`, or ``None`` before it.
    """
    keyed = _CURRENT.get()
    if keyed is not None:
        return keyed
    with _LOCK:
        return _SESSION


def _new_session(key: str | None = None, tags: dict | None = None) -> SessionState:
    """A session identified by its key.

    A keyed session's id is derived from the key, so every worker running the
    same code derives the same id for the same key: an incident on eight
    replicas dedups into one alert. The default session keeps a random id.
    """
    session_id = _keyed_id(key) if key is not None else uuid4().hex[:_SESSION_ID_CHARS]
    return SessionState(session_id, key=key, tags=tags)


def _keyed_id(key: str) -> str:
    """``<digest of the key>-0``: stable per key and the same on every worker."""
    digest = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()
    return f"{digest[:_KEYED_ID_CHARS]}-0"


def _forget_sessions() -> None:
    """Drop every keyed session. Holds ``_LOCK``."""
    global _ACTIVE
    _REGISTRY.clear()
    _ACTIVE = 0


def _start_session(config: RunboundConfig) -> None:
    """Replace the default session. Caller must hold ``_LOCK``."""
    global _SESSION
    _SESSION = _new_session()


def _teardown_for_tests() -> None:
    """Return the module to its uninitialized state. Test-only.

    Also stops the plane's threads and clears the local telemetry rings: those
    outlive an engine, and a test that left them behind would export — or
    report — the next test's traffic.
    """
    global _ENGINE, _SESSION, _SHARED
    with _LOCK:
        _ENGINE = None
        _SESSION = None
        previous, _SHARED = _SHARED, LocalState()
        _forget_sessions()
    previous.stop()
    _CURRENT.set(None)
    _coverage.reset_for_tests()
    local_events.clear_for_tests()
    hooks_module.reset_for_tests()
    approvals_module.reset_for_tests()
    spans.shutdown()


# --- what runbound can see --------------------------------------------------


def coverage() -> dict:
    """What runbound can actually see right now, as plain numbers.

    ``init()`` computes nothing on its own: the sensors are ``@runbound.tool``
    and :func:`session`. This is how a customer checks that they are wired, in a
    startup log, a health endpoint or a test::

        {"decorated_tools": 3,                 # @runbound.tool decorations
         "decorated_tool_names": ["search", ...],
         "tool_calls_seen": 41,                # decorated tool calls attempted
         "keyed_sessions_seen": 12,            # keys registered
         "tools_by_class": {"read": 2, ...},   # tools per capability class
         "fleet": "local protection active; fleet coordination: not connected",
         "fleet_pending": None}                # events queued to post; None with no plane

    Counters are process-lifetime: :func:`init` and :func:`reset` do not clear
    them. Works before :func:`init` — everything reads zero — and fails open to
    zeros rather than raising.
    """
    try:
        report = _coverage.snapshot()
        report["fleet"] = _fleet_report()
        report["fleet_pending"] = _fleet_pending()
        return report
    except Exception:
        _LOG.warning("runbound could not read its own coverage", exc_info=True)
        zeros = _coverage.zeros()
        zeros["fleet"] = _NO_FLEET
        zeros["fleet_pending"] = None
        return zeros


#: coverage()["fleet"]'s honest answer with no plane connected — never a reason
#: to think any rule is weaker, only that it is not coordinated across a fleet.
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
            return "connected; fleet state unavailable: deciding locally, last halt held"
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
    """This process's own anomalies, refusals and runtime changes, most recent last.

    An in-memory ring, readable with no account, no token and no control plane.
    Nothing here is written to disk, and no record ever carries a call argument.
    Process-lifetime like :func:`coverage`'s counters: :func:`init` and
    :func:`reset` do not clear it. ``[]`` before :func:`init` or when nothing has
    happened yet. See also :func:`decisions` and the ``on_event=`` callback.
    """
    try:
        return local_events.events(n)
    except Exception:
        _LOG.warning("runbound could not read its own local events", exc_info=True)
        return []


def tool_results(n: int = 100) -> list:
    """This process's own ``tool_result`` records — how each tool call ended — most recent last.

    One per call: ``tool``, ``outcome`` (``ok``, ``error``, ``refused``, ``withheld``),
    ``duration_s``, the scan's ``flags``/``flag_score``/``flagged``, ``truncated``, the sizes
    and ``tags``; never the result or an argument. Its own ring, apart from :func:`events`,
    because it grows with every call and would push the refusals out of that one. The
    ``on_event`` callback receives the same records as they happen. ``[]`` before :func:`init`.
    """
    try:
        return local_events.tool_results(n)
    except Exception:
        _LOG.warning("runbound could not read its own tool results", exc_info=True)
        return []


def decisions(n: int = 100) -> list:
    """This process's own admission :class:`~runbound.events.Decision` bodies,
    most recent last — every one stamped onto a refusal, the same numbers
    ``exc.decision`` carries. Same guarantees as :func:`events`."""
    try:
        return local_events.decisions(n)
    except Exception:
        _LOG.warning("runbound could not read its own local decisions", exc_info=True)
        return []


def tools() -> list[dict]:
    """The tool report this worker would send, exactly as the plane sees it.

    Every tool this process knows — one entry per ``@runbound.tool`` and one per
    tool name a framework adapter saw that no decorator declared — sorted by
    name::

        [{"name": "issue_refund",
          "decorated": True,               # False = nothing guards it
          "params": [{"name": "user", "annotation": "str", "required": True}],
          "doc": "Refund a customer.",     # the first line, never more
          "module": "acme.tools"}]

    Built from the code at import time, so it cannot drift from it. Works before
    :func:`init`, costs nothing to call, and fails open to ``[]``. Names,
    annotations rendered as strings and one docstring line are the whole of what
    leaves the process — never an argument, a default or a return value.
    """
    return _coverage.tool_report()


def assert_guarded() -> None:
    """Raise ``RuntimeError`` if no tool is guarded: nothing has been decorated.

    The startup-check form of what :func:`coverage` reports — for a test, a CI
    gate, or the line after the tools are imported in a service that would
    rather not boot blind. Passes as soon as one ``@runbound.tool`` exists.
    Never raises anything else.
    """
    try:
        decorated = _coverage.snapshot()["decorated_tools"]
    except Exception:
        _LOG.warning("runbound could not check its own coverage", exc_info=True)
        return
    if not decorated:
        raise RuntimeError(
            "runbound guards no tool: nothing is decorated with @runbound.tool "
            "(or the module declaring the tools has not been imported yet)"
        )


def plane_status() -> PlaneStatus:
    """Where this worker's link to the control plane stands, right now::

        PlaneStatus(mode="connected", last_contact_age_s=1.2,
                    consecutive_failures=0, notice=None)

    ``mode`` is ``"local"`` when no plane is configured (and before
    :func:`init`), ``"connected"`` while it is answering, and ``"degraded"`` when
    it is not — in which case every answer is being made locally. Safe to call
    from a health endpoint: it reads cached state and never opens a socket.
    ``halt_stale_s`` is seconds since the last contact while a fleet-wide halt
    is being enforced, and ``None`` whenever no halt is. ``plane_degraded`` is
    what the plane last said is wrong with itself while still answering
    (``"ledger_unreachable"``: it cannot reach its own database), ``None`` when it
    says nothing or says it is fine; it is informational (``mode`` is the link's
    own health, so a plane can be ``connected`` and degraded at once), and each
    change of it is one ``plane_degraded`` entry in :func:`events`.
    """
    try:
        with _LOCK:
            shared = _SHARED
        return shared.status()
    except Exception:
        _LOG.warning("runbound could not read the control plane status", exc_info=True)
        return PlaneStatus(mode="degraded")


def tool_calls(key: str | None = None) -> dict[str, int]:
    """How many times each tool has been *attempted*, per tool name.

    What ``tool_policy``'s ``max_calls`` is measured against, so a dashboard —
    or a test — can see the tally without provoking a violation::

        {"send_email": 2, "issue_refund": 1}

    Attempts, not successes: a refused call is counted, because it was made.
    With a ``key``, reports that keyed session; without one, the session work is
    being accounted to right now. A copy; never creates a session: ``{}`` before
    :func:`init`, for an unknown or evicted key, and for a session that has
    called nothing.
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


# --- keyed sessions ---------------------------------------------------------


@contextlib.contextmanager
def session(key: str, tags: dict | None = None, requested_for: str | None = None) -> Iterator[SessionState | None]:
    """Account everything inside this block to ``key``'s own session.

    ::

        with runbound.session(user_id, tags={"plan": "free"}):
            run_agent(message)

    One key means one :class:`SessionState`, reused across blocks, so a tool's
    ``max_calls`` is counted per key and a latched key is stopped on its own.
    At most ``max_sessions`` keys are kept; the least recently used is dropped,
    and re-entering a dropped key simply starts it over.

    ``requested_for`` (an email or user id, at most 128 characters) names the person the work is done for. It rides the
    approval request of any tool that asks a person inside the block, and the plane refuses (409 ``self_approval``) that same
    person approving it. Nothing else reads it.

    Blocks nest: the enclosing session is restored on exit. The binding is a
    context variable, so a thread or asyncio task that opens its own block is
    unaffected by any other. Yields the session, or ``None`` before :func:`init`
    — where the block is inert and observes nothing.
    """
    state = _enter_session(key, tags)
    if state is None:
        yield None
        return
    _count_active(1)
    token = _CURRENT.set(state)
    who_token = approvals_module._REQUESTED_FOR.set(approvals_module.clean_requested_for(requested_for))
    try:
        yield state
    finally:
        approvals_module._REQUESTED_FOR.reset(who_token)
        _CURRENT.reset(token)
        _count_active(-1)
        _flush_refusal_summaries(state)


def _enter_session(key: str, tags: dict | None) -> SessionState | None:
    """The state for ``key``, created or promoted in the registry.

    ``None`` before :func:`init`, and also if the lookup itself fails: a broken
    registry costs the caller its per-key accounting, never its block.
    """
    try:
        with _LOCK:
            engine = _ENGINE
            if engine is None:
                return None
            return _registered(key, tags, engine.config)
    except Exception:
        _LOG.warning("runbound could not open session %r; continuing unkeyed", key, exc_info=True)
        return None


def _registered(key: str, tags: dict | None, config: RunboundConfig) -> SessionState:
    """Get-or-create ``key``'s state as most-recently-used. Holds ``_LOCK``.

    Tags given on a later entry are merged in.
    """
    state = _REGISTRY.get(key)
    if state is not None:
        _REGISTRY.move_to_end(key)
        if tags:
            state.tags.update(tags)
        return state
    state = _new_session(key=key, tags=tags)
    _coverage.session_started()
    _REGISTRY[key] = state
    while len(_REGISTRY) > config.max_sessions:
        _REGISTRY.popitem(last=False)
    return state


def _count_active(delta: int) -> None:
    global _ACTIVE
    with _LOCK:
        _ACTIVE = max(0, _ACTIVE + delta)


def _row_may_wait(tool_name: str) -> bool:
    """Whether the tool's dashboard row can make the pre-chain wait on the plane (an approval, a quota take).

    Read at call time, like the engine's own lookup; a row that arrives in between costs one blocking call.
    """
    engine, _ = _active()
    row = None if engine is None else getattr(engine.shared, "row_for", lambda _tool: None)(tool_name)
    return row is not None and bool(row.approval_required or row.quota)


def _active_count() -> int:
    """Keyed :func:`session` blocks entered and not yet exited (the heartbeat's ``active``)."""
    with _LOCK:
        return _ACTIVE


def _flush_refusal_summaries(state: SessionState) -> None:
    """Report what this session's refusals suppressed, as its block exits."""
    try:
        with _LOCK:
            engine = _ENGINE
        if engine is not None:
            engine.flush_refusal_summaries(state)
    except Exception:
        _LOG.warning("runbound could not flush refusal summaries", exc_info=True)


# --- observing --------------------------------------------------------------


def _active() -> "tuple[Engine | None, SessionState | None]":
    """The engine and the session this call is accounted to, as one pair.

    Read together under ``_LOCK`` so that observing an event and enforcing a
    policy on the same call cannot land on two different sessions if
    :func:`init` runs in between. ``(None, None)`` before :func:`init`, which is
    what makes everything downstream inert.
    """
    state = _CURRENT.get()
    with _LOCK:
        engine = _ENGINE
        if state is None:
            state = _SESSION
    return (None, None) if engine is None or state is None else (engine, state)


def _observe(_latch: bool = True, _local: bool = False, **fields: Any) -> None:
    """Build an event from ``fields`` and run it through the engine.

    Does nothing before :func:`init`. Accounts the event to the enclosing
    :func:`session` block's state, or to the default session outside one, and
    assigns the event's monotonic timestamp and its 1-based step number.
    Swallows every failure except :class:`GuardrailTripped`, which must reach
    the host.
    """
    try:
        engine, state = _active()
        if engine is None or state is None:
            return
        step = state.next_step() if fields.get("kind") != "tool_result" else max(state.current_step, 1)
        event = Event(ts=time.monotonic(), step=step, **fields)
        engine.process(state, event, latch=_latch)
        if _local:
            local_events.record_result(state.session_id, state.key, event)
    except GuardrailTripped:
        raise
    except Exception:
        _LOG.warning("runbound failed to record an event; continuing", exc_info=True)


def _args_hash(tool_name: str, args: tuple, kwargs: dict) -> str | None:
    """sha256 of a salted canonical repr of ``(tool_name, args, sorted kwargs)``.

    Keyword order never changes the digest. Only the digest is ever stored — raw
    arguments never leave the caller's process — and it is salted per process,
    so it is an equality token for this process and not a fingerprint of the
    arguments. ``None`` when the arguments cannot be repr'd.
    """
    try:
        canonical = repr((tool_name, args, tuple(sorted(kwargs.items()))))
        payload = _HASH_SALT + canonical.encode("utf-8", "replace")
        return hashlib.sha256(payload).hexdigest()
    except Exception:
        _LOG.warning("runbound could not hash arguments for tool %r", tool_name, exc_info=True)
        return None


def _record_tool_error(tool_name: str, exc: BaseException, *, retryable: bool = False) -> None:
    """Emit a ``tool_error`` event for a tool that raised.

    A trip discovered while recording the failure is logged and dropped: the
    tool's own exception is the one the caller needs. ``retryable`` is
    ``@runbound.tool(retryable=True)``'s own declaration, carried onto the event.
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


# --- the tool decorator -----------------------------------------------------


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
    pre_hooks: Sequence[Callable] = (),
    gate_hooks: Sequence[Callable] = (),
    post_hooks: Sequence[Callable] = (),
    max_result_chars: int | None = None,
    approval: str | None = None,
    mode: str | None = None,
    timeout_s: float | None = None,
) -> Callable:
    """Record every call to a tool, and state the rule it must obey.

    ::

        @runbound.tool
        def search(query): ...

        @runbound.tool(name="web_search")
        def search(query): ...

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
    ``max_calls=n`` allows ``n`` admitted calls per session: a call the chain
    refuses does not use one up. ``constraint`` and ``require_approval`` each take a
    ``predicate(call) -> bool`` receiving a :class:`~runbound.policy.ToolCall`;
    returning ``False`` — or raising, which is fail-**closed** — refuses the
    call. Each tool's ``require_approval`` is its own.

    ``effects`` is the **set of capability classes** this tool carries, from
    :data:`runbound.policy.CAPABILITIES`; left unset it is unclassified. It is
    not a rule and restricts nothing on its own. What reads it is the class rules
    of ``init(capabilities=...)``: a tool is refused before its body runs, with
    :class:`~runbound.exceptions.CapabilityRefused`, when a class rule (or a
    Narrow halt from the plane) denies any one of its classes. Under
    ``require_rules=True`` a tool carrying a consequential class needs a rule
    that can refuse it; ``reviewed=True`` alone does not pass that gate::

        @runbound.tool(effects={"read"}, reviewed=True)
        def lookup_order(order_id): ...

        @runbound.tool(effects={"financial"}, max_calls=1)
        def issue_refund(user, amount): ...

    ``reviewed=True`` states that this tool was looked at and is deliberately
    unrestricted. It is **not** ``ToolPolicy.allow``; it restricts nothing and
    exists so a tool that needs no rule can still satisfy ``require_rules``.

    Rules fold into the policy the engine enforces the moment they are declared,
    so a tool decorated after :func:`init` — the usual ordering — is enforced
    from its first call. Where a tool's rule is stated both here and in
    ``init(tool_policy=...)``, this one wins and a warning says so.

    With ``init(require_rules=True)`` a tool that states no rule at all raises
    ``ValueError`` here, at decoration time: the CI gate. An unenforceable
    keyword (``max_calls=0``, a ``constraint`` that is not callable) raises here
    too, loudly — misconfiguration is the one thing in runbound that is never
    swallowed.

    The ``tool_call`` event is emitted *before* the function runs, so a refused
    call's body never executes. If the tool raises, a ``tool_error`` event is
    emitted and the tool's own exception is re-raised unchanged.

    ``pre_hooks`` and ``gate_hooks`` are this tool's own hooks
    (:mod:`runbound.hooks`), run after the ones in ``init(...)``: pre hooks first
    (they may rewrite the arguments), gate hooks last (refuse only). In between the
    final arguments are validated against the signature and type hints: a call that
    does not fit is refused before the body runs (``reason="invalid_arguments"``),
    for sync and async tools alike, and nothing is coerced.

    ``approval="required"`` puts a person in the loop (:mod:`runbound.approvals`), after the
    arguments are final and every check has passed. ``mode="sync"`` (the default) waits for the
    answer for up to ``timeout_s`` (600); ``mode="deferred"`` returns
    ``{"status": "pending_approval", "request_id": ...}`` at once and a worker of the service runs
    the tool once a person approves (``init(approval_secret=...)`` seals the arguments so any
    replica may). A denial or timeout refuses with ``reason="approval_denied"`` /
    ``"approval_timeout"``. A deferred tool's arguments must be JSON-friendly or this raises
    ``ValueError`` here.

    What the tool returns passes the post chain before the caller sees it:
    redaction of secret-named keys, truncation to a size (``max_result_chars``
    overrides ``init(result_limits=...)`` for this tool), a prompt-injection scan
    that only tags, then ``post_hooks`` (:func:`runbound.hooks.replace` /
    :func:`runbound.hooks.withhold`). A ``tool_result`` event records the duration,
    the outcome and what the chain did, never the result.

    ``polling=True``, ``idempotent=True`` and ``retryable=True`` state facts
    about the tool's own semantics — supposed to repeat, safe to repeat, expected
    to be retried by the caller — reported in the tool report (:func:`tools`) for
    a human or the plane to read. They are not rules.

    An ``async def`` tool gets an ``async def`` wrapper, so the decorated
    function is still a coroutine function: frameworks that dispatch on
    ``inspect.iscoroutinefunction`` (FastAPI, LangChain) keep routing it the way
    they did. Cancellation is not a tool failure and is re-raised untouched.
    """

    rules = _tool_rules(blocked, max_calls, constraint, require_approval, reviewed, effects)
    own_hooks = _tool_hooks(pre_hooks, "pre_hooks")
    own_gates = _tool_hooks(gate_hooks, "gate_hooks")
    own_post = _tool_hooks(post_hooks, "post_hooks")
    if max_result_chars is not None and (
        isinstance(max_result_chars, bool) or not isinstance(max_result_chars, int) or max_result_chars < 1
    ):
        raise ValueError(f"@runbound.tool(max_result_chars=...) must be an int >= 1, got {max_result_chars!r}")

    def decorate(func: Callable) -> Callable:
        tool_name = name or getattr(func, "__name__", "<tool>")
        _require_rule(tool_name, rules)
        validator = hooks_module.Validator(func, tool_name)
        spec = approvals_module.check_registration(func, tool_name, approval, mode, timeout_s)
        _coverage.tool_decorated(tool_name)
        reported = dataclasses.replace(rules, approval="required", approval_mode=spec.mode) if spec else rules
        _coverage.tool_declared(
            tool_name, func, reported, polling=polling, idempotent=idempotent, retryable=retryable
        )

        def begin(args: tuple, kwargs: dict) -> tuple:
            return _begin(tool_name, args, kwargs, rules.effects, validator, own_hooks, own_gates, spec)

        def end(result: Any, args: tuple, kwargs: dict, started: float, tags: list) -> Any:
            return _finish(tool_name, result, args, kwargs, started, tags, rules.effects, own_post, max_result_chars)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    if spec is not None or _row_may_wait(tool_name):  # a sync approval or a quota take blocks: keep it off the event loop
                        args, kwargs, tags, started = await asyncio.to_thread(begin, args, kwargs)
                    else:
                        args, kwargs, tags, started = begin(args, kwargs)
                except approvals_module.Deferred as deferred:
                    return deferred.result
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _record_tool_error(tool_name, exc, retryable=retryable)
                    _emit_result(tool_name, "error", started, tags, exc=exc)
                    raise
                return end(result, args, kwargs, started, tags)

            _register_approval(tool_name, spec, async_wrapper, func)
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                args, kwargs, tags, started = begin(args, kwargs)
            except approvals_module.Deferred as deferred:
                return deferred.result
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                _record_tool_error(tool_name, exc, retryable=retryable)
                _emit_result(tool_name, "error", started, tags, exc=exc)
                raise
            return end(result, args, kwargs, started, tags)

        _register_approval(tool_name, spec, wrapper, func)
        return wrapper

    return decorate if fn is None else decorate(fn)


def _register_approval(tool_name: str, spec: Any, wrapper: Callable, func: Callable) -> None:
    """Remember how a deferred tool is executed once approved: through its own full chain."""
    approvals_module.register_tool(tool_name, func, approvals_module.run_executor(wrapper))
    if spec is None:
        return
    with _LOCK:
        engine = _ENGINE
    secret = getattr(getattr(engine, "config", None), "approval_secret", None) or os.environ.get("RUNBOUND_APPROVAL_SECRET")
    approvals_module.register(tool_name, spec, approvals_module.run_executor(wrapper), secret)


def _tool_hooks(pre_hooks: Sequence[Callable], label: str) -> tuple:
    """``@tool(pre_hooks=...)`` or ``gate_hooks=`` checked: a sequence of callables."""
    if callable(pre_hooks):
        pre_hooks = (pre_hooks,)
    hooks = tuple(pre_hooks or ())
    for hook in hooks:
        if not callable(hook):
            raise ValueError(f"@runbound.tool({label}=...) must be callables taking a ToolCall, got {hook!r}")
    return hooks


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
    wrote it: ``max_calls=0`` can never be satisfied and ``constraint="under_500"``
    can never be asked, and both should fail the import that declares them, not
    the tool call that trips over them months later.
    """
    for label, value in (("blocked", blocked), ("reviewed", reviewed)):
        if not isinstance(value, bool):
            raise ValueError(f"@runbound.tool({label}=...) must be True or False, got {value!r}")
    if blocked and reviewed:
        raise ValueError(
            "@runbound.tool(blocked=True, reviewed=True) contradicts itself: "
            "blocked refuses the tool, reviewed says it is deliberately unrestricted"
        )
    if max_calls is not None and (
        isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1
    ):
        raise ValueError(f"@runbound.tool(max_calls=...) must be an int >= 1, got {max_calls!r}")
    for label, value in (("constraint", constraint), ("require_approval", require_approval)):
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
    module is all of them.
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


def _check_required_rules(config: RunboundConfig) -> None:
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
    effects: frozenset = frozenset(),
    decorated: bool = False,
    validator: "hooks_module.Validator | None" = None,
    hooks: Sequence[Callable] = (),
    gate_hooks: Sequence[Callable] = (),
    allow_rewrite: bool = True,
    tags: "list | None" = None,
    approval: Any = None,
    info: "dict | None" = None,
) -> "tuple[tuple, dict]":
    """Record and vet a call that is about to be made; returns the ``(args, kwargs)`` to run.

    Raises :class:`~runbound.exceptions.GuardrailTripped` (or a subclass) when the
    call is refused — before the tool body runs, which is the whole point of doing
    this here rather than after. The event goes first: the attempt counts towards
    the step count and this tool's own tally whether or not the chain then refuses
    it, and ``max_calls`` is measured against a tally that includes this attempt.
    The arguments returned differ from the ones given only when a user pre-hook
    rewrote them.
    """
    _coverage.tool_called()
    _observe(kind="tool_call", tool_name=tool_name, args_hash=_args_hash(tool_name, args, kwargs))
    return _admit_action(
        tool_name, args, kwargs, effects, decorated, validator, hooks, gate_hooks, allow_rewrite, tags, approval, info
    )


def _admit_action(
    tool_name: str,
    args: tuple,
    kwargs: dict,
    effects: frozenset,
    decorated: bool,
    validator: "hooks_module.Validator | None" = None,
    hooks: Sequence[Callable] = (),
    gate_hooks: Sequence[Callable] = (),
    allow_rewrite: bool = True,
    tags: "list | None" = None,
    approval: Any = None,
    info: "dict | None" = None,
) -> "tuple[tuple, dict]":
    """Run the pre-hook chain (:meth:`~runbound.engine.Engine.admit_call`) on one attempted call.

    Looking the session up and building the :class:`~runbound.policy.ToolCall` is
    fail-open (a bug logs and lets the call through); the refusals are raised on
    purpose from inside the engine. A refusal counts the attempt as refused, never
    executed; clearing the chain counts it as admitted and executed, right before
    the tool's own body runs.
    """
    try:
        engine, state = _active()
        if engine is None or state is None:
            return args, kwargs
        call = ToolCall(tool_name, args, kwargs, state.key, dict(state.tags or {}))
        chain = tuple(engine.config.pre_hooks) + tuple(hooks)
        gates = tuple(engine.config.gate_hooks) + tuple(gate_hooks)
    except Exception:
        _LOG.warning(
            "runbound could not apply the tool policy to %r; the call was allowed",
            tool_name,
            exc_info=True,
        )
        return args, kwargs
    try:
        final = engine.admit_call(
            state, call, validator=validator, hooks=chain, gate_hooks=gates, effects=effects,
            decorated=decorated,
            allow_rewrite=allow_rewrite and approvals_module.current_request() is None,  # no rewrite after the seal
            tags=tags, approval=approval, info=info,
        )
    except GuardrailTripped:
        state.mark_action_refused()
        raise
    state.mark_action_admitted()
    return final.args, dict(final.kwargs)


# --- the call's two ends: before the body, and what comes back ------------------


class _Run(float):
    """The monotonic start of a call, carrying its span (a float, so durations are ``now - run``)."""

    span: "spans.CallSpan | None" = None
    effects: frozenset = frozenset()
    request_id: "str | None" = None  # the approval request this call filed, if any
    report_id: "str | None" = None  # the plane's id of a sync approval, to report the outcome on


def _start_run(tool_name: str, effects: frozenset = frozenset(), call_id: str | None = None) -> _Run:
    """Start timing a call, and its span when something would receive one."""
    run = _Run(time.monotonic())
    run.effects = effects
    if spans.active():
        try:
            from . import __version__

            _engine, state = _active()
            run.span = spans.begin(tool_name, getattr(state, "session_id", None), __version__, call_id)
        except Exception:
            _LOG.debug("runbound could not start a span", exc_info=True)
    return run


def _begin(
    tool_name: str, args: tuple, kwargs: dict, effects: frozenset,
    validator: "hooks_module.Validator", hooks: tuple, gate_hooks: tuple, approval: Any = None,
) -> "tuple[tuple, dict, list, _Run]":
    """Run the pre-hook chain; returns ``(args, kwargs, tags, run)`` for the body.

    A refusal ends the call here: its ``tool_result`` (outcome ``"refused"``) is
    recorded and the refusal re-raised.
    """
    tags: list = []
    run = _start_run(tool_name, effects)
    if run.span is not None:
        run.span.args_digest = _args_hash(tool_name, args, kwargs)
    info: dict = {}
    try:
        args, kwargs = _announce_call(
            tool_name, args, kwargs, effects=effects, decorated=True,
            validator=validator, hooks=hooks, gate_hooks=gate_hooks, tags=tags, approval=approval, info=info,
        )
    except GuardrailTripped as refusal:
        run.request_id = info.get("request_id")
        _emit_result(tool_name, "refused", run, tags, exc=refusal)
        raise
    except approvals_module.Deferred:
        run.request_id = info.get("request_id")
        _emit_result(tool_name, "pending_approval", run, tags)
        raise
    run.request_id = info.get("request_id")
    run.report_id = info.get("approval_id") if info.get("sync") else None
    return args, kwargs, tags, run


def _end_span(tool_name: str, outcome: str, run: _Run, exc: "BaseException | None", done: Any, tags: Any = ()) -> None:
    """Finish the call's span (if it has one) with what is known now. Never raises."""
    span = run.span
    run.span = None
    if span is None:
        return
    try:
        engine, state = _active()
        processed = done.processed if done is not None else None
        refusal = exc if outcome == "refused" else None
        span.end(
            spans.facts(
                tool_name, outcome, config=getattr(engine, "config", None),
                run_id=getattr(state, "session_id", None), key=getattr(state, "key", None), effects=run.effects,
                call_id=span.call_id, args_digest=span.args_digest, approval_request_id=run.request_id, exc=exc if outcome == "error" else None,
                decision=getattr(refusal, "decision", None),
                policy_version=getattr(getattr(engine, "shared", None), "policy_version", None),
                flags=processed.flags if processed else (), flag_score=processed.flag_score if processed else 0.0,
                result_chars=processed.chars_in if processed else 0,
                would_refuse=next((str(t).split(":", 1)[1] for t in tags if str(t).startswith("would_refuse:")), None),
            ),
            exc if outcome == "error" else None,
        )
    except Exception:
        _LOG.debug("runbound could not end a span", exc_info=True)


def _emit_result(
    tool_name: str, outcome: str, started: float, tags: list, done: "hooks_module.PostResult | None" = None,
    exc: "BaseException | None" = None,
) -> None:
    """Record the ``tool_result`` event (how long, how it ended, what the post chain did) and end the span."""
    fields: dict = {}
    if done is not None and done.processed is not None:
        processed = done.processed
        fields.update(
            flags=processed.flags, flag_score=processed.flag_score, flagged=processed.flagged,
            truncated=processed.truncated, result_chars=processed.chars_in, result_chars_out=processed.chars_out,
        )
    all_tags = tuple(tags) + (tuple(done.tags) if done is not None else ())
    if isinstance(started, _Run):
        _end_span(tool_name, outcome, started, exc, done, all_tags)
        if started.report_id and outcome in ("ok", "withheld", "error"):
            started_report, started.report_id = started.report_id, None
            approvals_module.report_async(_ENGINE, started_report, "failed" if outcome == "error" else "executed")
    _observe(
        False, kind="tool_result", tool_name=tool_name, outcome=outcome,
        duration_s=max(time.monotonic() - started, 0.0), tags=all_tags, **fields,
        _local=True,
    )


def _finish(
    tool_name: str, result: Any, args: tuple, kwargs: dict, started: float, tags: list,
    effects: frozenset, post_hooks: tuple, max_result_chars: int | None,
) -> Any:
    """The post chain on what the body returned: redact, truncate, scan, user post hooks.

    Returns what the caller gets (the result, cut and cleaned, or a withheld notice) and
    records the ``tool_result`` event. Inert before :func:`init`. If the chain itself
    cannot run, the output is withheld: an unprocessed result is never passed on.
    """
    try:
        engine, state = _active()
    except Exception:
        _LOG.warning("runbound could not look up the session for %r's result", tool_name, exc_info=True)
        return result
    if engine is None or state is None:
        return result
    duration = time.monotonic() - started
    try:
        config = engine.config
        limits = results_module.Limits(**{**(config.result_limits or {}), **(
            {} if max_result_chars is None else {"max_chars": max_result_chars})})
        keys = results_module.DEFAULT_REDACT_KEYS | {key.lower() for key in config.redact_keys}
        call = ToolCall(tool_name, args, kwargs, state.key, dict(state.tags or {}))
        done = hooks_module.run_post(
            result, call, duration, effects, limits=limits, keys=keys,
            hooks=tuple(config.post_hooks) + tuple(post_hooks), timeout=config.hook_timeout_s,
        )
    except Exception:
        _LOG.warning("runbound could not run the post chain for %r; the output is withheld", tool_name, exc_info=True)
        _emit_result(tool_name, "withheld", started, tags + ["hook_error:post-chain"])
        return "Output withheld: post-hook post-chain failed"
    _emit_result(tool_name, done.outcome, started, tags, done)
    return done.result
