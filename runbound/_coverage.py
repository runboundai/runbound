"""What runbound can actually see, counted.

``init()`` configures detectors; it does not observe anything. The sensors are
:func:`runbound.wrap` (and :mod:`runbound.autowrap`), ``@runbound.tool``,
:func:`runbound.session` and :func:`runbound.record_call` — and a process
that installed runbound but wired up none of them is *blind while reporting
green*. This module is the honest answer to "is anything actually being
watched?": a handful of counters, one snapshot, and one warning that fires when
a provider SDK is imported and yet no guarded call has ever been seen.

Private on purpose — the public surface is ``runbound.coverage()``, a
function. A submodule named ``coverage`` would be bound onto the package by any
``import runbound.coverage`` and would silently replace that function.

Everything here is best-effort: a counter that cannot be bumped costs a number
in a report, never a call. Counters are process-lifetime — ``init()`` and
``reset()`` leave them alone, because "has this process ever seen traffic?" is
not a question a new session re-asks.
"""

import logging
import sys
import threading
import time
from typing import Callable, Sequence

_LOG = logging.getLogger("runbound")

#: Modules whose presence in ``sys.modules`` means this process talks to an LLM.
#: The first two runbound can guard; the rest it cannot, which is exactly why
#: they are listed — an imported ``boto3`` is an honest blind spot, not a gap in
#: the report.
PROVIDER_MODULES = (
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "boto3",
    "mistralai",
    "cohere",
)

#: Provider module -> the wrapper shape whose guarded calls cover it. A module
#: absent from this map has no wrapper at all, so importing it is always a
#: blind spot however much other traffic is guarded.
_GUARDABLE = {"openai": "openai", "anthropic": "anthropic"}

#: The name every silence timer runs under, so a leak is countable.
TIMER_NAME = "runbound-coverage"

#: How long the warning quotes when nobody configured a window.
DEFAULT_CHECK_SECONDS = 60.0

#: How many distinct decorated-tool names :func:`snapshot` ever reports. A
#: fleet dashboard wants real names to tell its story, not an unbounded list
#: growing with every dynamically-named tool a long-lived process ever saw.
DECORATED_TOOL_NAMES_MAX = 32

_LOCK = threading.Lock()

_WRAPPED_CLIENTS = 0
_DECORATED_TOOLS = 0
_TOOL_CALLS = 0
_KEYED_SESSIONS = 0
_GUARDED_CALLS = 0
_LAST_GUARDED_AT: float | None = None
_SHAPES_SEEN: set[str] = set()
_DECORATED_TOOL_NAMES: set[str] = set()
_TIMER: threading.Timer | None = None


def client_wrapped() -> None:
    """One more client guarded by :func:`runbound.wrap`."""
    global _WRAPPED_CLIENTS
    try:
        with _LOCK:
            _WRAPPED_CLIENTS += 1
    except Exception:  # pragma: no cover - a counter never costs a call
        pass


def tool_decorated(name: str | None = None) -> None:
    """One more function guarded by ``@runbound.tool``.

    ``name`` is optional and additive: existing callers that pass none still
    bump the counter exactly as before. When given, it is remembered (up to
    :data:`DECORATED_TOOL_NAMES_MAX` distinct names) so a fleet dashboard can
    show which tools a service actually has, not just how many.
    """
    global _DECORATED_TOOLS
    try:
        with _LOCK:
            _DECORATED_TOOLS += 1
            if isinstance(name, str) and name and len(_DECORATED_TOOL_NAMES) < DECORATED_TOOL_NAMES_MAX:
                _DECORATED_TOOL_NAMES.add(name)
    except Exception:  # pragma: no cover
        pass


def tool_called() -> None:
    """One more decorated tool call attempted."""
    global _TOOL_CALLS
    try:
        with _LOCK:
            _TOOL_CALLS += 1
    except Exception:  # pragma: no cover
        pass


def session_started() -> None:
    """One more key registered for the first time.

    A count, not a set: keeping the keys would mean a second, unbounded copy of
    end-user identifiers outliving the registry that evicts them. The trade is
    stated rather than hidden — a key whose session was evicted and then
    re-entered counts again.
    """
    global _KEYED_SESSIONS
    try:
        with _LOCK:
            _KEYED_SESSIONS += 1
    except Exception:  # pragma: no cover
        pass


def guarded_call() -> None:
    """One more model call runbound saw, however it turned out.

    A failed call counts: the question this answers is whether the sensors are
    wired, and a call that reached the provider and came back an error is proof
    that they are.
    """
    global _GUARDED_CALLS, _LAST_GUARDED_AT
    try:
        with _LOCK:
            _GUARDED_CALLS += 1
            _LAST_GUARDED_AT = time.monotonic()
    except Exception:  # pragma: no cover
        pass


def provider_seen(provider: str) -> None:
    """Note the shape of an endpoint a guarded call reached.

    ``provider`` is an endpoint label — ``"openai@localhost:11434"`` — and only
    the shape before the ``@`` says which SDK was covered.
    """
    try:
        shape = str(provider).split("@", 1)[0].strip()
        if shape:
            with _LOCK:
                _SHAPES_SEEN.add(shape)
    except Exception:  # pragma: no cover
        pass


def providers_imported() -> list[str]:
    """Every known provider SDK this process has imported, in listed order."""
    try:
        return [name for name in PROVIDER_MODULES if name in sys.modules]
    except Exception:  # pragma: no cover
        return []


def providers_unguarded(imported: Sequence[str] | None = None) -> list[str]:
    """Imported provider SDKs that no guarded call has ever covered.

    A provider runbound has no wrapper for — Gemini, Bedrock, Mistral, Cohere
    — is listed the moment it is imported and never leaves the list, because
    importing it is the blind spot. ``openai`` and ``anthropic`` leave it as
    soon as one guarded call reaches an endpoint of that shape.
    """
    names = providers_imported() if imported is None else list(imported)
    with _LOCK:
        seen = set(_SHAPES_SEEN)
    return [name for name in names if _GUARDABLE.get(name) not in seen]


def snapshot(
    auto_wrapped: Sequence[str] = (),
    *,
    auto_wrap: bool = True,
    seconds: float = DEFAULT_CHECK_SECONDS,
) -> dict:
    """Everything the counters know, in one dict — see ``runbound.coverage``."""
    imported = providers_imported()
    with _LOCK:
        last = _LAST_GUARDED_AT
        report = {
            "auto_wrapped": list(auto_wrapped),
            "wrapped_clients": _WRAPPED_CLIENTS,
            "decorated_tools": _DECORATED_TOOLS,
            "decorated_tool_names": sorted(_DECORATED_TOOL_NAMES),
            "guarded_calls": _GUARDED_CALLS,
            "tool_calls_seen": _TOOL_CALLS,
            "keyed_sessions_seen": _KEYED_SESSIONS,
            "providers_imported": imported,
        }
    report["providers_unguarded"] = providers_unguarded(imported)
    report["last_guarded_call_age_s"] = (
        None if last is None else max(time.monotonic() - last, 0.0)
    )
    warning = silence_warning(auto_wrapped, auto_wrap=auto_wrap, seconds=seconds)
    report["warnings"] = [] if warning is None else [warning]
    return report


def zeros() -> dict:
    """The shape of a snapshot with nothing in it. What a failure reports."""
    return {
        "auto_wrapped": [],
        "wrapped_clients": 0,
        "decorated_tools": 0,
        "decorated_tool_names": [],
        "guarded_calls": 0,
        "tool_calls_seen": 0,
        "keyed_sessions_seen": 0,
        "providers_imported": [],
        "providers_unguarded": [],
        "last_guarded_call_age_s": None,
        "warnings": [],
    }


def silence_warning(
    auto_wrapped: Sequence[str] = (),
    *,
    auto_wrap: bool = True,
    seconds: float = DEFAULT_CHECK_SECONDS,
) -> str | None:
    """The "nothing is guarded" text, or ``None`` when something is.

    One string with three consumers — the timer logs it at WARNING,
    :func:`runbound.assert_guarded` raises it, and
    :func:`runbound.coverage` reports it — so a customer who meets this
    problem in a log, in a test and in a dashboard meets one sentence.

    ``None`` when a guarded call has been seen (the sensors work) or when no
    provider SDK is imported at all (there is no traffic to miss).
    """
    try:
        with _LOCK:
            calls = _GUARDED_CALLS
        if calls:
            return None
        imported = providers_imported()
        if not imported:
            return None
        names = ", ".join(repr(name) for name in imported)
        verb = "is" if len(imported) == 1 else "are"
        return (
            f"runbound sees no LLM traffic after {seconds:.0f}s although {names} "
            f"{verb} imported — nothing is guarded. Did you call "
            f"runbound.wrap(client)? ({_auto_wrap_state(auto_wrap, auto_wrapped)})"
        )
    except Exception:  # pragma: no cover - a report never breaks a host
        _LOG.debug("runbound: could not work out the coverage warning", exc_info=True)
        return None


def _auto_wrap_state(auto_wrap: bool, auto_wrapped: Sequence[str]) -> str:
    """Why auto-instrumentation did not save them, in three honest words."""
    if not auto_wrap:
        return "auto_wrap: off"
    labels = list(auto_wrapped)
    if not labels:
        return "auto_wrap: on, but no provider SDK was patched"
    return "auto_wrap: on, patched " + ", ".join(labels)


def start_silence_timer(seconds: float, warn: Callable[[], None]) -> None:
    """Arm the one-shot silent-zero check, replacing any timer already armed.

    A daemon thread: a check that has not fired yet must never be the reason a
    process will not exit.
    """
    global _TIMER
    cancel_silence_timer()
    try:
        timer = threading.Timer(seconds, warn)
        timer.name = TIMER_NAME
        timer.daemon = True
        with _LOCK:
            _TIMER = timer
        timer.start()
    except Exception:
        _LOG.warning("runbound could not start the coverage check", exc_info=True)


def cancel_silence_timer() -> None:
    """Stop the armed silent-zero check, if there is one. Never raises."""
    global _TIMER
    try:
        with _LOCK:
            timer, _TIMER = _TIMER, None
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1.0)
    except Exception:  # pragma: no cover
        _LOG.debug("runbound: could not cancel the coverage check", exc_info=True)


def reset_for_tests() -> None:
    """Put every counter back to zero and disarm the timer. Test-only."""
    global _WRAPPED_CLIENTS, _DECORATED_TOOLS, _TOOL_CALLS, _KEYED_SESSIONS
    global _GUARDED_CALLS, _LAST_GUARDED_AT
    cancel_silence_timer()
    with _LOCK:
        _WRAPPED_CLIENTS = 0
        _DECORATED_TOOLS = 0
        _TOOL_CALLS = 0
        _KEYED_SESSIONS = 0
        _GUARDED_CALLS = 0
        _LAST_GUARDED_AT = None
        _SHAPES_SEEN.clear()
        _DECORATED_TOOL_NAMES.clear()
