"""The pre-hook chain: what runs between "a tool was called" and "its body runs".

The fixed order (RS-3, docs/design/hook-chain.md §4), every call::

    1. user rewrite hooks   init(pre_hooks=[...]) then @tool(pre_hooks=[...]); the ONLY
                            place the arguments may change
    2. validate             the final arguments against the signature and type hints
    3. policy               the tool policy's rules, on the final arguments
    4. capability           the class rules of init(capabilities=...) and the action cap
    5. approval             builds the seal from the final arguments
    6. user gate hooks      init(gate_hooks=[...]) then @tool(gate_hooks=[...]); refuse only

then the body. Built-ins run before user gate hooks, so a user hook cannot observe
or soften a refusal; the first refuse wins. The engine runs slots 3 to 5
(:meth:`~runbound.engine.Engine.admit_call`); this module is slots 1, 2 and 6 and the
vocabulary a hook speaks.

**A hook** takes a :class:`~runbound.policy.ToolCall` and returns :func:`allow`
(or ``None`` / ``True``), :func:`refuse` (or ``False``) or, from a pre hook only,
:func:`rewrite`. Rewrites return new arguments and never mutate the caller's.

**A user hook that raises or outlives ``hook_timeout_s`` (5 s)** refuses with
``reason="hook_error"`` when the tool carries a financial, destructive or privileged
class or states no class, and is logged once and skipped for a tool that only reads,
writes or reaches out. A *built-in* that raises (the validator, the policy, the
capability rules) refuses every class. The exception text never reaches the model.
A timed-out hook cannot be killed: its thread (a daemon, run under a copy of the
caller's context) keeps running after the call has been refused or let through, so a
hook that can hang should bound its own work. For a tool that only reads, writes or
reaches out, the failure is logged once and skipped, and the ``tool_result`` event
carries the tag ``hook_error:<name>``.

**Validation** never coerces, and its reasons name the parameter and the types,
never a value (a reason is exported with the refusal). ``bool`` is refused where
``int`` or ``float`` is declared, an unknown ``TypedDict`` key is refused, and a hint
that cannot be checked from JSON (``datetime``, ``UUID``, a TypeVar, ...) is skipped
with one warning per ``(tool, parameter)``; a Python caller passing a real instance
still gets the ``isinstance`` check. A dataclass is validated as a dict, not built.
``Annotated`` metadata is ignored. ``Signature.bind`` runs first, so a missing or
unexpected argument is refused with the signature's own message. ``*args`` and
``**kwargs`` are never checked and defaults are not validated.
"""

from __future__ import annotations

import collections.abc
import contextvars
import dataclasses
import enum
import inspect
import logging
import threading
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import results as results_module
from .policy import ToolCall

_LOG = logging.getLogger("runbound")

#: The classes a tool may carry for a failing user hook to be logged and skipped.
#: Anything else — financial, destructive, privileged, or no class stated — fails closed.
FAIL_OPEN_EFFECTS = frozenset({"read", "write", "external"})


# --- what a hook says -----------------------------------------------------------


@dataclass(frozen=True)
class Refuse:
    """A hook's refusal. ``reason`` is shown to the caller: never put arguments in it."""

    reason: str


@dataclass(frozen=True)
class Rewrite:
    """A hook's replacement arguments; ``None`` keeps that half as it was."""

    args: tuple | None = None
    kwargs: dict | None = None


def allow() -> None:
    """The verdict that changes nothing."""
    return None


def refuse(reason: str = "refused by a pre-hook") -> Refuse:
    """Refuse the call, with a reason that tells the model what to change."""
    return Refuse(str(reason))


def rewrite(args: Sequence | None = None, kwargs: dict | None = None) -> Rewrite:
    """Replace the call's arguments (new containers; the caller's are untouched)."""
    return Rewrite(None if args is None else tuple(args), None if kwargs is None else dict(kwargs))


@dataclass(frozen=True)
class ToolResult:
    """What a post hook is handed: the call, its (redacted, truncated) result, how long it took."""

    name: str
    args: tuple
    kwargs: dict
    result: Any
    session_key: str | None
    tags: dict
    duration_s: float


@dataclass(frozen=True)
class Replace:
    """A post hook's replacement for the result."""

    result: Any


@dataclass(frozen=True)
class Withhold:
    """A post hook's decision that the model must not see this result."""

    reason: str = "withheld by a post-hook"


def replace(result: Any) -> Replace:
    """From a post hook: hand the model ``result`` instead."""
    return Replace(result)


def withhold(reason: str = "withheld by a post-hook") -> Withhold:
    """From a post hook: hand the model "Output withheld: <reason>" instead."""
    return Withhold(str(reason))


class StageRefused(Exception):
    """A slot of the chain refused. The engine turns this into the refusal the caller sees.

    ``stage`` is ``"arguments"``, ``"hook"`` or ``"hook_error"`` (the Decision's boundary).
    """

    def __init__(self, stage: str, reason: str, hook: str | None = None, retry_after_s: float | None = None) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.hook = hook
        self.retry_after_s = retry_after_s
        self.http_status: int | None = None  # set by approvals._file when the plane answered with a status that refused the request


# --- warn once ---------------------------------------------------------------------

_WARNED: set = set()
_WARN_LOCK = threading.Lock()


def _warn_once(key: tuple, message: str, *args: Any) -> None:
    with _WARN_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    _LOG.warning(message, *args)


def reset_for_tests() -> None:
    """Forget which warnings were already said. Test-only."""
    with _WARN_LOCK:
        _WARNED.clear()


# --- slot 1: argument validation -------------------------------------------------


class _Unsupported(Exception):
    """This hint cannot be checked from a JSON-shaped value."""


def _ok_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _got(value: Any) -> str:
    return type(value).__name__


def _check(value: Any, hint: Any, path: str) -> str | None:
    """``None`` when ``value`` fits ``hint``, else a reason; raises :class:`_Unsupported`."""
    if hint is Any or hint is inspect.Parameter.empty:
        return None
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _check(value, args[0], path)  # metadata is ignored in v1
    if hint is None or hint is type(None):
        return None if value is None else f"{path}: expected None, got {_got(value)}"
    if hint is bool:
        return None if isinstance(value, bool) else f"{path}: expected bool, got {_got(value)}"
    if hint is int:
        return None if _ok_int(value) else f"{path}: expected int, got {_got(value)}"
    if hint is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return None if ok else f"{path}: expected float, got {_got(value)}"
    if hint is str:
        return None if isinstance(value, str) else f"{path}: expected str, got {_got(value)}"
    if origin is typing.Literal:
        ok = any(value == option and type(value) is type(option) for option in args)
        return None if ok else f"{path}: expected one of {list(args)!r}, got a different {_got(value)}"
    if origin is typing.Union or (hasattr(types, "UnionType") and isinstance(hint, types.UnionType)):
        reasons = []
        for branch in args:
            reason = _check(value, branch, path)
            if reason is None:
                return None
            reasons.append(reason)
        return f"{path}: no branch matched ({'; '.join(reasons)})"
    if origin is list or hint is list:
        if not isinstance(value, list):
            return f"{path}: expected list, got {_got(value)}"
        if args:
            for index, item in enumerate(value):
                reason = _check(item, args[0], f"{path}[{index}]")
                if reason:
                    return reason
        return None
    if origin is dict or hint is dict:
        if not isinstance(value, dict):
            return f"{path}: expected dict, got {_got(value)}"
        if args:
            for key, item in value.items():
                reason = _check(key, args[0], f"{path} key {key!r}") or _check(item, args[1], f"{path}[{key!r}]")
                if reason:
                    return reason
        return None
    if typing.is_typeddict(hint):
        return _check_typeddict(value, hint, path)
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return _check_dataclass(value, hint, path)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        if isinstance(value, hint):
            return None
        values = [member.value for member in hint]
        ok = any(value == option and type(value) is type(option) for option in values)
        return None if ok else f"{path}: expected one of {values!r}, got a different {_got(value)}"
    if isinstance(hint, typing.TypeVar) or hint is typing.Callable or origin is collections.abc.Callable:
        raise _Unsupported(repr(hint))
    if hasattr(hint, "__supertype__"):  # NewType
        return _check(value, hint.__supertype__, path)
    if isinstance(hint, type):  # datetime, UUID, Decimal, user classes
        if isinstance(value, hint):
            return None
        raise _Unsupported(hint.__name__)
    raise _Unsupported(repr(hint))


def _check_typeddict(value: Any, hint: Any, path: str) -> str | None:
    if not isinstance(value, dict):
        return f"{path}: expected object for {hint.__name__}, got {_got(value)}"
    fields = typing.get_type_hints(hint, include_extras=True)
    required = getattr(hint, "__required_keys__", frozenset(fields))
    missing = sorted(key for key in required if key not in value)
    if missing:
        return f"{path}: missing required key(s) {missing}"
    unknown = sorted(str(key) for key in value if key not in fields)
    if unknown:
        return f"{path}: unknown key(s) {unknown}"
    for key, field_hint in fields.items():
        if key in value:
            reason = _check(value[key], field_hint, f"{path}.{key}")
            if reason:
                return reason
    return None


def _check_dataclass(value: Any, hint: type, path: str) -> str | None:
    if isinstance(value, hint):
        return None
    if not isinstance(value, dict):
        return f"{path}: expected object for {hint.__name__}, got {_got(value)}"
    hints = typing.get_type_hints(hint, include_extras=True)
    for field in dataclasses.fields(hint):
        needed = field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
        if field.name not in value:
            if needed:
                return f"{path}: missing required field {field.name!r}"
            continue
        reason = _check(value[field.name], hints.get(field.name, Any), f"{path}.{field.name}")
        if reason:
            return reason
    unknown = sorted(str(key) for key in value if key not in hints)
    return f"{path}: unknown field(s) {unknown}" if unknown else None


class Validator:
    """Slot 1 for one tool: checks a call's arguments against ``func``'s signature and hints.

    The signature is read at construction; the hints are resolved on the first
    call (a forward reference may not be importable until the module has
    finished loading) and kept. A hint that cannot be resolved skips every hint
    for the tool, with one warning.
    """

    def __init__(self, func: Callable, tool_name: str | None = None) -> None:
        self.name = tool_name or getattr(func, "__name__", "<tool>")
        self._func = func
        try:
            self._signature: inspect.Signature | None = inspect.signature(func)
        except (TypeError, ValueError):
            self._signature = None
        self._hints: dict | None = None
        self._lock = threading.Lock()

    def _resolved_hints(self) -> dict:
        hints = self._hints
        if hints is None:
            try:
                hints = typing.get_type_hints(inspect.unwrap(self._func), include_extras=True)
            except Exception as exc:  # NameError for a name that is not importable at runtime, etc.
                _warn_once(
                    ("hints", self.name),
                    "runbound: type hints for tool %r could not be resolved (%s: %s); "
                    "its arguments are not type-checked",
                    self.name, type(exc).__name__, exc,
                )
                hints = {}
            with self._lock:
                self._hints = hints
        return hints

    def check(self, args: tuple, kwargs: dict) -> str | None:
        """A reason this call's arguments are refused, or ``None``."""
        signature = self._signature
        if signature is None:
            return None
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError as exc:
            return f"signature: {exc}"
        hints = self._resolved_hints()
        for name, value in bound.arguments.items():
            parameter = signature.parameters[name]
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            try:
                reason = _check(value, hints.get(name, inspect.Parameter.empty), name)
            except _Unsupported as unsupported:
                _warn_once(
                    ("unsupported", self.name, name),
                    "runbound: parameter %r of tool %r has a type hint (%s) that cannot be "
                    "checked from JSON; it is not validated",
                    name, self.name, unsupported,
                )
                continue
            if reason:
                return reason
        return None


# --- slot 2: user hooks ----------------------------------------------------------------


def _hook_name(hook: Callable) -> str:
    return getattr(hook, "__name__", None) or type(hook).__name__


def fails_closed(effects: frozenset | None) -> bool:
    """Does a user hook's failure refuse this tool? Yes for a consequential class
    (financial, destructive, privileged) and for a tool that states no class."""
    classes = frozenset(effects or ())
    return not classes or not classes <= FAIL_OPEN_EFFECTS


def fails_closed_unavailable(effects: frozenset | None) -> bool:
    """Does fail mode refuse this tool when the policy is unavailable (TPD §5.5, register K8)?

    The classes ``fails_closed`` names, and also a bare ``write`` (``write`` alone or with ``read``): a write tool whose declaration
    carries no further effect tag counts as unclassified for fail mode. ``{write, external}`` has a tag and follows it (open).
    """
    classes = frozenset(effects or ())
    return fails_closed(classes) or ("write" in classes and not classes - {"read", "write"})


def _call_hook(hook: Callable, call: ToolCall, timeout: float | None) -> Any:
    """``hook(call)``, bounded: a hook still running after ``timeout`` seconds counts as a raise.

    The hook runs in a daemon thread under a copy of the caller's context, so it
    sees the same session; a hung one is abandoned, never joined.
    """
    if timeout is None:
        return hook(call)
    box: list = []
    context = contextvars.copy_context()

    def run() -> None:
        try:
            box.append((True, context.run(hook, call)))
        except BaseException as exc:  # carried back to the caller as the hook's own failure
            box.append((False, exc))

    thread = threading.Thread(target=run, name="runbound-hook", daemon=True)
    thread.start()
    thread.join(timeout)
    if not box:
        raise TimeoutError(f"hook still running after {timeout}s")
    ok, value = box[0]
    if not ok:
        raise value
    return value


def run_hooks(
    hooks: Sequence[Callable],
    call: ToolCall,
    effects: frozenset | None,
    *,
    kind: str = "pre",
    allow_rewrite: bool = True,
    timeout: float | None = None,
    tags: list | None = None,
) -> ToolCall:
    """Run user hooks in order; returns the call the rest of the chain judges.

    ``kind="pre"`` (slot 1) hooks may refuse or rewrite; ``kind="gate"`` (slot 6)
    hooks may only refuse, so a rewrite from one is a hook error — so is a rewrite
    where ``allow_rewrite`` is false (a framework adapter that cannot apply new
    arguments). A hook that raises, returns nonsense or outlives ``timeout`` is a
    hook error: :class:`StageRefused` for a tool that fails closed, one warning
    and carry on for a tool that only reads, writes or reaches out; the skipped hook's
    ``hook_error:<name>`` is appended to ``tags`` for the ``tool_result`` event.
    """
    rewrites_ok = allow_rewrite and kind == "pre"
    for hook in hooks:
        name = _hook_name(hook)
        try:
            verdict = _call_hook(hook, call, timeout)
            if verdict is False:
                verdict = Refuse(f"refused by {kind}-hook {name}")
            if isinstance(verdict, Rewrite) and not rewrites_ok and (
                (call.args if verdict.args is None else verdict.args) != call.args
                or (call.kwargs if verdict.kwargs is None else verdict.kwargs) != call.kwargs
            ):  # a rewrite that changes nothing is an allow; one that changes something cannot be applied
                raise TypeError("a rewrite cannot be applied here")
            if not (verdict is None or verdict is True or isinstance(verdict, (Refuse, Rewrite))):
                raise TypeError(f"a hook returns allow, refuse or rewrite, not {type(verdict).__name__}")
        except Exception as exc:
            if fails_closed(effects):
                _LOG.warning("runbound: %s-hook %r failed (%s); refusing %r", kind, name, type(exc).__name__, call.name)
                raise StageRefused("hook_error", f"{kind}-hook {name} failed", name) from None
            if tags is not None:
                tags.append(f"hook_error:{name}")
            _warn_once(
                ("hook-error", call.name, name),
                "runbound: %s-hook %r failed on tool %r (%s); continuing, the tool only reads, writes or reaches out",
                kind, name, call.name, type(exc).__name__,
            )
            continue
        if isinstance(verdict, Refuse):
            raise StageRefused("hook", verdict.reason, name)
        if isinstance(verdict, Rewrite) and rewrites_ok:
            call = dataclasses.replace(
                call,
                args=call.args if verdict.args is None else verdict.args,
                kwargs=dict(call.kwargs) if verdict.kwargs is None else verdict.kwargs,
            )
    return call


def validate(validator: Validator | None, call: ToolCall) -> None:
    """Slot 2: refuse arguments that do not fit the tool. A built-in that raises refuses every class."""
    if validator is None:
        return
    try:
        reason = validator.check(call.args, call.kwargs)
    except Exception as exc:  # a bug in the validator, not in the caller's arguments
        _LOG.warning("runbound: argument validation failed for %r (%s)", call.name, type(exc).__name__, exc_info=True)
        raise StageRefused("hook_error", "argument validation failed", "validate") from None
    if reason:
        raise StageRefused("arguments", reason, "validate")


# --- the post chain: redact, truncate, scan, user post hooks (S6) -------------------------


@dataclass
class PostResult:
    """What the post chain made of one result."""

    result: Any
    outcome: str = "ok"  # "ok" | "withheld"
    tags: list = field(default_factory=list)
    processed: "results_module.Processed | None" = None


def _withheld(name: str) -> str:
    return f"Output withheld: post-hook {name} failed"


def run_post(
    result: Any,
    call: ToolCall,
    duration_s: float,
    effects: frozenset | None,
    *,
    limits: "results_module.Limits",
    keys: frozenset,
    hooks: Sequence[Callable] = (),
    timeout: float | None = None,
) -> PostResult:
    """Slots after the body: redact, truncate, scan, then the user post hooks.

    A redaction that raises **withholds** the output, for every class. A failed
    truncation falls back to a plain size cap and a failed scan leaves the result
    untagged (``truncate_failed`` / ``scan_failed`` tags). A user post hook that raises
    or outlives ``timeout`` withholds the output for a tool that fails closed
    (see :func:`fails_closed`) and is tagged ``hook_error:<name>`` and skipped
    otherwise. The exception text is never part of what comes back.
    """
    try:
        done = results_module.process(result, limits, keys)
    except Exception:  # a bug in the post chain itself: withhold, never pass an unprocessed result
        _LOG.warning("runbound: the post chain failed for %r", call.name, exc_info=True)
        return PostResult(_withheld("redact"), "withheld", ["hook_error:redact"])
    out = PostResult(done.result, "ok", [], done)
    if "redact" in done.failed:
        _LOG.warning("runbound: redaction failed for %r; the output is withheld", call.name)
        out.result, out.outcome = _withheld("redact"), "withheld"
        out.tags.append("hook_error:redact")
        return out
    out.tags.extend(f"{step}_failed" for step in done.failed)
    for hook in hooks:
        name = _hook_name(hook)
        context = ToolResult(call.name, call.args, call.kwargs, out.result, call.session_key, call.tags, duration_s)
        try:
            verdict = _call_hook(hook, context, timeout)
            if not (verdict is None or verdict is True or isinstance(verdict, (Replace, Withhold))):
                raise TypeError("a post hook returns allow, replace or withhold")
        except Exception as exc:
            out.tags.append(f"hook_error:{name}")
            if fails_closed(effects):
                _LOG.warning("runbound: post-hook %r failed (%s); withholding %r's output", name, type(exc).__name__, call.name)
                out.result, out.outcome = _withheld(name), "withheld"
                return out
            _warn_once(
                ("post-hook-error", call.name, name),
                "runbound: post-hook %r failed on tool %r (%s); the result is passed on as it was",
                name, call.name, type(exc).__name__,
            )
            continue
        if isinstance(verdict, Withhold):
            out.result, out.outcome = f"Output withheld: {verdict.reason}", "withheld"
            return out
        if isinstance(verdict, Replace):
            out.result = verdict.result
    return out
