"""What runbound can actually see, counted, and the tool report.

``init()`` configures rules; it does not observe anything. The sensors are
``@runbound.tool`` and :func:`runbound.session`. This module keeps a handful of
counters, one snapshot, and the **tool report** — which tools this process has,
taken from the ``@runbound.tool`` decorators at import time and from the names a
framework adapter sees, sent on the heartbeat and shown by :func:`runbound.tools`.

Private on purpose — the public surface is ``runbound.coverage()``, a
function. A submodule named ``coverage`` would be bound onto the package by any
``import runbound.coverage`` and would silently replace that function.

Everything here is best-effort: a counter that cannot be bumped costs a number
in a report, never a call. Counters are process-lifetime — ``init()`` and
``reset()`` leave them alone.
"""

import hashlib
import inspect
import types
import json
import logging
import threading

from .policy import CAPABILITIES
from typing import Any, Callable

_LOG = logging.getLogger("runbound")

#: How many distinct decorated-tool names :func:`snapshot` ever reports. A
#: fleet dashboard wants real names to tell its story, not an unbounded list
#: growing with every dynamically-named tool a long-lived process ever saw.
DECORATED_TOOL_NAMES_MAX = 32

#: How many tools a report ever carries. Beyond this the report is truncated
#: (sorted by name) and a debug line is logged once.
TOOL_REPORT_MAX = 500

#: The buckets ``tools_by_class`` always reports, zero or not: every capability
#: class, plus the tools that declared none.
CLASS_BUCKETS = CAPABILITIES + ("unclassified",)

_LOCK = threading.Lock()

_DECORATED_TOOLS = 0
_TOOL_CALLS = 0
_KEYED_SESSIONS = 0
_DECORATED_TOOL_NAMES: set[str] = set()

#: name -> report entry. A second, richer store beside ``_DECORATED_TOOL_NAMES``
#: — that one answers "how many, and which names" for the coverage snapshot and
#: keeps its own small cap; this one is what the console draws a tool inventory
#: from, so it carries the signature too.
_TOOLS: dict[str, dict] = {}
_TRUNCATION_LOGGED = False

#: name -> the :class:`runbound.policy.ToolRules` its ``@runbound.tool`` stated,
#: for every decorated tool including the ones that state nothing. The engine
#: folds this into the policy it enforces (``Engine._local_policy``), so unlike
#: everything else in this module it holds the customer's own callables — and
#: for that reason it is *not* the store the report is built from: the report
#: reads ``_TOOLS``, where the same rules live rendered as strings.
#:
#: Same lifetime as :data:`_DECORATED_TOOL_NAMES`: written at import, read for
#: the life of the process, emptied only by :func:`reset_for_tests`. Decorators
#: run long after ``init()`` in a normal app, so the engine reads it live and
#: caches on :data:`_TOOL_RULES_VERSION`, which every write below bumps.
_TOOL_RULES: dict = {}
_TOOL_RULES_VERSION = 0

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
    caller identifiers outliving the registry that evicts them. The trade is
    stated rather than hidden — a key whose session was evicted and then
    re-entered counts again.
    """
    global _KEYED_SESSIONS
    try:
        with _LOCK:
            _KEYED_SESSIONS += 1
    except Exception:  # pragma: no cover
        pass


# --- the tool report --------------------------------------------------------
#
# What tools this process has, built from the code that declares them rather
# than from a file anyone writes, so it cannot drift from the code. Names,
# parameter names, annotations rendered as strings and one line of docstring
# leave the process; an argument value, a default value or a return value never
# does.


def tool_declared(
    name: str,
    func: Callable | None = None,
    rules=None,
    *,
    polling: bool = False,
    idempotent: bool = False,
    retryable: bool = False,
    deferred: bool = False,
) -> None:
    """Remember a tool the code declares, with its signature. Never raises.

    ``rules`` is the :class:`runbound.policy.ToolRules` the decorator stated.
    It is kept twice on purpose: rendered as strings in the report entry, which
    goes over the wire, and as itself in :data:`_TOOL_RULES`, which never does
    and is what the engine folds into the policy it enforces.

    ``polling``, ``idempotent`` and ``retryable`` are the decorator's
    own metadata, not rules — they carry no ``ToolRules`` and are reported
    here purely for a human or the plane to read (see :meth:`_tool_entry`).

    ``deferred`` is the mode the decorator registered (``mode="deferred"``), never inferred from a row.
    """
    global _TOOL_RULES_VERSION
    try:
        if not isinstance(name, str) or not name:
            return
        entry = _tool_entry(
            name, func, rules, polling=polling, idempotent=idempotent, retryable=retryable,
            deferred=deferred,
        )
        with _LOCK:
            _TOOLS[name] = entry
            if rules is not None:
                _TOOL_RULES[name] = rules
                _TOOL_RULES_VERSION += 1
    except Exception:  # pragma: no cover - a report never costs a call
        _LOG.debug("runbound: could not record the tool %r", name, exc_info=True)


def tool_rules_version() -> int:
    """How many times the rule registry has changed, ever.

    A plain int read, no lock: the engine checks it before every tool call to
    decide whether the policy it cached is still the policy the decorators
    describe, and taking a lock for that would put one on the call path.
    """
    return _TOOL_RULES_VERSION


def tool_rules() -> dict:
    """Every decorated tool's stated rules, name -> ``ToolRules``. A fresh copy."""
    with _LOCK:
        return dict(_TOOL_RULES)


def unruled_tools() -> list[str]:
    """Decorated tools that state no rule at all, sorted — what ``require_rules`` names."""
    for_check = tool_rules()
    return sorted(name for name, rule in for_check.items() if not rule.stated())


def unenforced_consequential_tools() -> list:
    """Tools carrying a consequential class with no rule that can refuse them.

    What ``require_rules`` also fails on: ``reviewed=True`` alone does
    not satisfy the gate for an action that cannot be undone or unpaid. Each
    entry is ``(name, classes)`` so the message can name what made it strict.
    """
    for_check = tool_rules()
    return sorted(
        (name, rule.consequential())
        for name, rule in for_check.items()
        if rule.stated() and getattr(rule, "needs_rule", lambda: False)()
    )


def tools_by_class() -> dict:
    """How many decorated tools carry each capability class.

    A tool counts under *every* class it declares, so the columns do not sum to
    the number of tools; one that declared none counts as unclassified.
    """
    counts = {bucket: 0 for bucket in CLASS_BUCKETS}
    for rule in tool_rules().values():
        classes = [name for name in getattr(rule, "effects", ()) or () if name in counts]
        if not classes:
            counts["unclassified"] += 1
        for name in classes:
            counts[name] += 1
    return counts


def tool_requested(name: str) -> None:
    """Remember a tool name the *model* asked for. Never raises.

    A name that no ``@runbound.tool`` declared stays ``decorated: False`` --
    the model can ask for it and nothing guards it, which the console shows
    in red. A name already declared is left exactly as it is.

    Bounded by :data:`TOOL_REPORT_MAX`: these names come from a model, not from
    the code, so a long-lived process asked for endlessly invented tools must
    not grow a dict forever. Past the cap a new undecorated name is dropped,
    which is what truncating the report would have done to it anyway.
    """
    try:
        if not isinstance(name, str) or not name:
            return
        with _LOCK:
            if name in _TOOLS or len(_TOOLS) >= TOOL_REPORT_MAX:
                return
            _TOOLS[name] = {
                "name": name,
                "decorated": False,
                "params": [],
                "doc": None,
                "module": None,
                "rules": {},
                "effects": [],
                "polling": False,
                "idempotent": False,
                "retryable": False,
                "deferred": False,
            }
    except Exception:  # pragma: no cover
        _LOG.debug("runbound: could not record the request for %r", name, exc_info=True)


def tool_report() -> list[dict]:
    """Every tool this process knows, sorted by name, capped at TOOL_REPORT_MAX.

    A fresh copy each call, so a caller that edits what it got back — a test, a
    REPL, a payload builder — cannot edit what the next heartbeat sends. Fails
    open to ``[]``: a report that cannot be built is worth a debug line, never
    an exception in a customer's process.
    """
    try:
        with _LOCK:
            entries = sorted(_TOOLS.values(), key=lambda entry: entry["name"])
        if len(entries) > TOOL_REPORT_MAX:
            _log_truncation(len(entries))
            entries = entries[:TOOL_REPORT_MAX]
        return [_copy_entry(entry) for entry in entries]
    except Exception:  # pragma: no cover
        _LOG.debug("runbound: could not build the tool report", exc_info=True)
        return []


def tool_report_hash(report: list[dict] | None = None) -> str:
    """A stable short digest of a report: sha256 of the canonical JSON, 16 hex chars.

    Stable across processes and across runs — it is what tells the plane
    "nothing about my tools changed", so it must not depend on dict ordering or
    on anything with an address in it. Raises only for a report that is not
    JSON-serialisable, which is nothing :func:`tool_report` ever returns.
    """
    entries = tool_report() if report is None else report
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def _copy_entry(entry: dict) -> dict:
    """One entry, deep enough that nothing shared stays shared."""
    return {
        **entry,
        "params": [dict(param) for param in entry["params"]],
        "rules": dict(entry.get("rules") or {}),
    }


def _log_truncation(total: int) -> None:
    """Say once that a report is being cut short. Repeating it every 5s helps nobody."""
    global _TRUNCATION_LOGGED
    with _LOCK:
        if _TRUNCATION_LOGGED:
            return
        _TRUNCATION_LOGGED = True
    _LOG.debug(
        "runbound knows %d tools and reports the first %d by name",
        total,
        TOOL_REPORT_MAX,
    )


def _tool_entry(
    name: str,
    func: Callable | None,
    rules=None,
    *,
    polling: bool = False,
    idempotent: bool = False,
    retryable: bool = False,
    deferred: bool = False,
) -> dict:
    """The eleven keys the plane reads, in the order it reads them.

    ``rules`` is what the decorator stated, rendered by
    :meth:`runbound.policy.ToolRules.as_report` — a predicate as its
    ``"module:qualname"``, never the predicate. A tool with no rule reports an
    empty dict, so the console can tell "no rule" from "not reported".

    ``polling``, ``idempotent`` and ``retryable`` are the last three:
    the decorator's own declared metadata, not a rule — enforced nowhere in
    this module, just carried for a human or the plane to read.
    """
    return {
        "name": name,
        "decorated": True,
        "params": _params(func),
        "doc": _first_doc_line(func),
        "module": _module(func),
        "rules": _rules_report(rules),
        "effects": sorted(getattr(rules, "effects", ()) or ()),
        "polling": bool(polling),
        "idempotent": bool(idempotent),
        "retryable": bool(retryable),
        "deferred": bool(deferred),
    }


def _rules_report(rules) -> dict:
    """``rules.as_report()``, or ``{}`` for anything that will not render itself."""
    if rules is None:
        return {}
    try:
        return dict(rules.as_report())
    except Exception:  # pragma: no cover - a report never costs a call
        _LOG.debug("runbound: could not render a tool's rules", exc_info=True)
        return {}


def _params(func: Callable | None) -> list[dict]:
    """A tool's parameters, or ``[]`` when the callable does not describe itself.

    Builtins, C functions and exotic wrappers raise from
    :func:`inspect.signature`; they are still tools this process has, so they
    are reported with no parameters rather than not reported at all.
    """
    if func is None:
        return []
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except Exception:
        _LOG.debug("runbound: %r does not describe its signature", func, exc_info=True)
        return []
    if parameters and parameters[0].name in ("self", "cls"):
        parameters = parameters[1:]
    return [_param_entry(parameter) for parameter in parameters]


def _param_entry(parameter: inspect.Parameter) -> dict:
    """One parameter: its name as written, its annotation, whether it must be given.

    ``required`` is "has no default" — never the default *value*, which is the
    customer's data and stays in the customer's process.
    """
    if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
        name, required = "*" + parameter.name, False
    elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
        name, required = "**" + parameter.name, False
    else:
        name = parameter.name
        required = parameter.default is inspect.Parameter.empty
    return {
        "name": name,
        "annotation": _annotation(parameter.annotation),
        "required": required,
    }


def _annotation(annotation: Any) -> str | None:
    """An annotation as the source that wrote it, or ``None`` when there is none.

    A class renders as its bare name (``str``, ``float``), everything else as
    its own text (``list[int]``, ``str | None``) — the same rendering
    :class:`inspect.Signature` uses when it prints itself. Anything whose text
    carries a memory address is dropped instead: an entry whose hash changed on
    every restart would resend the whole report on every deploy of every
    worker, and tell the console nothing it could use.
    """
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        text = annotation
    elif isinstance(annotation, type) and not isinstance(annotation, types.GenericAlias):
        # On Python 3.10 ``isinstance(list[int], type)`` is True (3.11+ says
        # False), and taking ``__qualname__`` there prints ``list`` for
        # ``list[int]`` -- found by the public repo's 3.10 CI leg, 2026-09-15.
        text = annotation.__qualname__
    else:
        text = str(annotation)
    return None if not text or " at 0x" in text else text


def _first_doc_line(func: Callable | None) -> str | None:
    """The docstring's first non-empty line, stripped, or ``None``.

    One line and never more, whatever the docstring holds — a privacy rule, not
    a formatting preference: what follows the summary is where people put
    hostnames, credentials and customer examples.
    """
    try:
        doc = getattr(func, "__doc__", None)
    except Exception:
        return None
    if not isinstance(doc, str):
        return None
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _module(func: Callable | None) -> str | None:
    """Where the tool is defined, or ``None`` when the callable will not say."""
    module = getattr(func, "__module__", None)
    return module if isinstance(module, str) and module else None


def snapshot() -> dict:
    """Everything the counters know, in one dict — see ``runbound.coverage``."""
    with _LOCK:
        report = {
            "decorated_tools": _DECORATED_TOOLS,
            "decorated_tool_names": sorted(_DECORATED_TOOL_NAMES),
            "tool_calls_seen": _TOOL_CALLS,
            "keyed_sessions_seen": _KEYED_SESSIONS,
        }
    report["tools_by_class"] = tools_by_class()
    return report


def zeros() -> dict:
    """The shape of a snapshot with nothing in it. What a failure reports."""
    return {
        "decorated_tools": 0,
        "decorated_tool_names": [],
        "tool_calls_seen": 0,
        "keyed_sessions_seen": 0,
        "tools_by_class": {bucket: 0 for bucket in CLASS_BUCKETS},
    }


def reset_for_tests() -> None:
    """Put every counter back to zero. Test-only."""
    global _DECORATED_TOOLS, _TOOL_CALLS, _KEYED_SESSIONS
    global _TRUNCATION_LOGGED, _TOOL_RULES_VERSION
    with _LOCK:
        _DECORATED_TOOLS = 0
        _TOOL_CALLS = 0
        _KEYED_SESSIONS = 0
        _DECORATED_TOOL_NAMES.clear()
        _TOOLS.clear()
        _TOOL_RULES.clear()
        # Bumped, never zeroed: an engine that outlives this call must not
        # find its cache key matching a registry that has just been emptied.
        _TOOL_RULES_VERSION += 1
        _TRUNCATION_LOGGED = False
