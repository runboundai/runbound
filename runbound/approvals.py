"""Approvals: put a person in the loop of a tool call, sync or deferred, with a seal.

``@runbound.tool(approval="required", mode="sync" | "deferred", timeout_s=600)`` makes slot 5 of
the pre-hook chain (:mod:`runbound.hooks`) ask the control plane's approval routes (A2: plain
HTTP with the service's API key) about the **final** arguments, after every rewrite hook, the
validator, the policy and the capability check have passed.

**Sync.** ``POST /v1/approvals``, then long-poll ``GET ?wait_s=25`` until a person decides or a
monotonic deadline of ``timeout_s`` passes. The plane is the only clock: the SDK sends
``timeout_s``, never an absolute time. Transport errors are retried until the deadline and are
never a denial. Denied -> ``ExecutionRefused(reason="approval_denied")``; expired or out of time
-> ``approval_timeout``; the refusal is what the agent gets back as the tool's result. A sync
tool that then ran is reported (``executed`` / ``failed``) so the plane can close the row.

**Deferred.** The call returns ``{"status": "pending_approval", "request_id": ...}`` at once and
the body does not run. Once a person approves, the plane offers the request to a worker in
``hello.approved[]`` (one per reply); that worker verifies the seal **before any hook runs**,
executes the tool with the approved arguments (``runbound.current_request()`` names the request
while it does), reports ``executed`` / ``failed`` and calls ``init(on_deferred_result=...)``.

**The seal** makes the plane a courier for deferred arguments, never their author::

    seal = {"v": 1, "not_after": "<ISO 8601 UTC, ends in Z>",
            "mac": hex(HMAC-SHA256(secret, canonical_json({v, service, tool, request_id, args, not_after})))}
    canonical_json = json.dumps(obj, sort_keys=True, separators=(",", ":"))

The secret is per service (``RUNBOUND_APPROVAL_SECRET`` or ``init(approval_secret=)``), shared by
its replicas, and never sent to the plane. On a mismatch, or past ``not_after`` (+300 s of clock
tolerance; ``not_after`` is only an anti-replay guard, the plane's ``expires_at`` is the expiry),
the worker runs nothing, reports ``tampered`` and the call ends in ``approval_tampered``. With no
secret the SDK sends no seal and executes only requests it parked itself, with the arguments it
parked (in memory; S7 persists them): it says so once, at registration. Anything a rewrite hook
would change after the seal exists is a hook error, not a rewrite.

Accepted limit: a compromised plane can still *approve* a real pending request.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextvars
import dataclasses
import enum
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
import pathlib
import sys
import threading
import time
import typing
import uuid
from collections import OrderedDict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from . import delivery
from . import ids as ids_module
from .hooks import StageRefused, _warn_once
from .plane_types import key_hash

_LOG = logging.getLogger("runbound")

SEAL_VERSION = 1
#: How far PAST ``not_after`` a worker's clock may be before the seal is refused: 300 s. One-sided by design: a clock ahead by
#: more than this fails the seal (``clock_skew``), a clock behind it by any amount still verifies. ``not_after`` is only an
#: anti-replay guard; the plane's claim TTL and the request's ``expires_at`` bound replay, so no lower bound is checked.
NOT_AFTER_TOLERANCE_S = 300
#: How much longer than ``timeout_s`` a seal stays valid (a claim and a retry need room).
NOT_AFTER_SLACK_S = 600
LONG_POLL_S = 25
#: Parked deferred requests kept in memory (S7 persists them).
PENDING_MAX = 10_000

PENDING_RESULT_STATUS = "pending_approval"

#: ``ExecutionRefused.reason`` for each way a request can end badly.
DENIED, TIMEOUT, TAMPERED, UNAVAILABLE = "approval_denied", "approval_timeout", "approval_tampered", "approval_unavailable"
QUOTA_EXCEEDED = "quota_exceeded"
PLANE_TOO_OLD = "plane_too_old"
#: A bind that reports the request was already bound (``first: false``) or refuses it: another attempt, or this one earlier, may have started the body, so it is NOT dispatched again.
PRIOR_OUTCOME_UNKNOWN = "prior_outcome_unknown"
BIND_TIMEOUT_S = 5.0

#: ``verify_seal``'s answer when the MAC holds but ``not_after`` has passed by more than the tolerance: the arguments are
#: what was sealed, so this is a clock problem, not tampering, and is reported as outcome ``clock_skew``.
CLOCK_SKEW_REASON = "the seal is past its not_after"

#: (service, tool, key hash) -> monotonic instant before which the plane said its quota is full.
_QUOTA_BLOCKED: dict = {}


@dataclasses.dataclass(frozen=True)
class ApprovalSpec:
    """What ``@tool(approval="required", ...)`` stated."""

    mode: str = "sync"
    timeout_s: float = 600.0
    names: tuple = ()  # the tool's parameter names, for normalising args
    requirement: "str | None" = None  # which requirement this request is for (a console line id, a default or the code), so a grant satisfies only that one


class Deferred(Exception):
    """Raised out of slot 5 for a deferred request: the call ends here with ``result``."""

    def __init__(self, result: dict, approval_id: str | None) -> None:
        super().__init__(PENDING_RESULT_STATUS)
        self.result = result
        self.approval_id = approval_id
        self.request_id = result.get("request_id")


class Suspended(Exception):
    """Raised out of slot 5 for a SUSPENDED request (AAI-04-4b, the LangChain resume path only): the plane holds a bind-required request and the caller interrupts its graph.

    Raised BEFORE any body runs and before any wait. The call is pending approval: not refused, not executed, no ``max_calls`` or repetition unit kept (``resume_admit`` reserves them
    when the approval is used); the plane's quota unit stays as the create took it."""

    def __init__(self, request_id: str, approval_id: "str | None", args_digest: "str | None", attempt_id: "str | None") -> None:
        super().__init__(PENDING_RESULT_STATUS)
        self.request_id, self.approval_id, self.args_digest, self.attempt_id = request_id, approval_id, args_digest, attempt_id


@dataclasses.dataclass
class Pending:
    tool: str
    request_id: str
    args: dict
    approval_id: str | None
    run_id: str | None = None  # the run that filed it: the resumed execution reports under the same run


_PENDING: "OrderedDict[str, Pending]" = OrderedDict()
_EXECUTORS: dict = {}
_INFLIGHT: set = set()
_LOST: set = set()  # claims the plane took back while their body was still running
#: Approval ids whose tool body is running right now: each hello says so, which renews the claim (the
#: plane's claim ttl is short; a long execution must not be re-offered to another replica).
_EXECUTING: set = set()
_LOCK = threading.Lock()
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("runbound_approval_request", default=None)
#: What the plane offered with the approved request being executed here: ``bind_required``, its ``claim_token`` (the lease it must bind under) and whether a quota unit is held. A mutable dict
#: so the bind can say the claim was lost.
_CLAIM: contextvars.ContextVar = contextvars.ContextVar("runbound_approval_claim", default=None)
_APPROVAL: contextvars.ContextVar = contextvars.ContextVar("runbound_approval_id", default=None)  # the plane's id of the request being executed, for the re-check's report
#: Who a call is made for (``runbound.session(requested_for=...)``): sent on the approval create so the plane can refuse the requester
#: approving their own request (409 ``self_approval``). A context variable, so a thread or task that opens its own block is unaffected.
_REQUESTED_FOR: contextvars.ContextVar = contextvars.ContextVar("runbound_requested_for", default=None)
REQUESTED_FOR_MAX = 128


def clean_requested_for(value: Any) -> str | None:
    """An email or user id of at most 128 characters, else ``None`` (logged once): a bad value never costs the caller its block."""
    if value is None:
        return None
    if isinstance(value, str) and 0 < len(value.strip()) <= REQUESTED_FOR_MAX:
        return value.strip()
    _warn_once(("requested-for",), f"runbound: session(requested_for=) must be an email or user id of 1 to {REQUESTED_FOR_MAX} characters; ignored")
    return None


def claims_lost(ids: list) -> None:
    """The plane says these claims are no longer ours: stop renewing them and start nothing new for them.

    A body already running cannot be stopped; when it ends it reports as usual (``failed`` if it failed;
    a report on a request that is already closed is a harmless repeat)."""
    with _LOCK:
        for approval_id in ids:
            if approval_id in _EXECUTING:
                _EXECUTING.discard(approval_id)
                _LOST.add(approval_id)
                _LOG.warning("runbound: the claim on approval %s is no longer this worker's", approval_id)


def executing_ids() -> list:
    """Approval ids this worker is executing now (the heartbeat renews their claims)."""
    with _LOCK:
        return sorted(_EXECUTING)


def current_request() -> str | None:
    """The request id of the deferred approval being executed right now, else ``None``.

    Use it as the tool's **idempotency key**: a deferred request can be offered twice (a claim that lapsed
    on a paused worker, a replica that took over), so a side effect keyed by ``request_id`` happens once."""
    return _CURRENT.get()


def reset_for_tests() -> None:
    with _LOCK:
        _PENDING.clear()
        _EXECUTORS.clear()
        _META.clear()
        _SIGS.clear()
        _SHAPES.clear()
        _INFLIGHT.clear()
        _EXECUTING.clear()
        _LOST.clear()
        _QUOTA_BLOCKED.clear()
        _WITHDRAWN.clear()
        WITHDRAWALS.clear()


# --- the seal ----------------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _mac(secret: str, service: str, tool: str, request_id: str, args: dict, not_after: str) -> str:
    body = canonical_json({"v": SEAL_VERSION, "service": service, "tool": tool, "request_id": request_id,
                           "args": args, "not_after": not_after})
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_seal(secret: str, service: str, tool: str, request_id: str, args: dict, timeout_s: float,
              now: datetime | None = None) -> dict:
    """The seal for one deferred request (see the module docstring)."""
    moment = now or datetime.now(timezone.utc)
    not_after = _iso(moment + timedelta(seconds=timeout_s + NOT_AFTER_SLACK_S))
    return {"v": SEAL_VERSION, "not_after": not_after,
            "mac": _mac(secret, service, tool, request_id, args, not_after)}


def verify_seal(secret: str | None, seal: Any, service: str, tool: str, request_id: str, args: Any,
                now: datetime | None = None) -> str | None:
    """Why ``seal`` does not hold for what was received, or ``None`` when it does.

    The ``not_after`` check is ONE-SIDED: ``now`` more than :data:`NOT_AFTER_TOLERANCE_S` past ``not_after`` is refused
    (``CLOCK_SKEW_REASON``, a clock problem, not tampering), while a ``now`` any distance BEFORE it still verifies: this
    worker's clock being an hour behind the sealer's changes nothing. Replay is bounded by the plane (its claim TTL and the
    request's ``expires_at``), not by a lower bound here.
    """
    if not secret:
        return "no approval secret is configured on this worker"
    if not isinstance(seal, dict) or seal.get("v") != SEAL_VERSION or not isinstance(args, dict):
        return "the seal is missing or malformed"
    not_after, mac = seal.get("not_after"), seal.get("mac")
    if not isinstance(not_after, str) or not isinstance(mac, str):
        return "the seal is missing or malformed"
    try:
        expected = _mac(secret, service, tool, request_id, args, not_after)
        if not hmac.compare_digest(expected, mac):
            return "the seal does not match the arguments received"
        limit = datetime.strptime(not_after, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return "the seal is missing or malformed"
    if (now or datetime.now(timezone.utc)) > limit + timedelta(seconds=NOT_AFTER_TOLERANCE_S):
        return CLOCK_SKEW_REASON
    return None


# --- registration -------------------------------------------------------------------------------


class _InjectedMarker:
    """``Annotated[Session, runbound.Injected]``: the app supplies this parameter, the model does not (DEF-1)."""

    def __repr__(self) -> str:
        return "runbound.Injected"


#: Marks a parameter the APP supplies (a DB session, an HTTP client, the current tenant). It is never stored, sealed, digested, shown to a person or read by a rule;
#: a deferred execution gets it again from ``init(provide=...)``, else from the parameter's default.
Injected = _InjectedMarker()

_NONE_TYPE = type(None)


def _marks_injected(meta: Any) -> bool:
    """Is ``meta`` (one ``Annotated`` metadata item) ``runbound.Injected``, or LangChain's ``InjectedToolArg`` (and so ``InjectedState``, ``InjectedToolCallId``)?

    LangChain is only looked at when it is already imported: a hint can only name it if it is, so nothing is ever imported on its account.
    """
    if meta is Injected:
        return True
    for module in ("langchain_core.tools", "langchain_core.tools.base"):
        base = getattr(sys.modules.get(module), "InjectedToolArg", None)
        if isinstance(base, type) and (meta is base or isinstance(meta, base) or (isinstance(meta, type) and issubclass(meta, base))):
            return True
    return False


def is_injected(hint: Any) -> bool:
    """Does this (resolved, ``include_extras``) hint mark its parameter as supplied by the app?"""
    if typing.get_origin(hint) is typing.Annotated:
        return any(_marks_injected(meta) for meta in hint.__metadata__) or is_injected(typing.get_args(hint)[0])
    if typing.get_origin(hint) is typing.Union or type(hint).__name__ == "UnionType":
        return any(is_injected(arm) for arm in typing.get_args(hint))
    return False


def _inner(hint: Any) -> Any:
    """The class behind ``Annotated[...]`` / ``Optional[...]``, to look an injected parameter's provider up by type."""
    while True:
        origin, args = typing.get_origin(hint), typing.get_args(hint)
        if origin is typing.Annotated:
            hint = args[0]
        elif origin is typing.Union or type(hint).__name__ == "UnionType":
            arms = [a for a in args if a is not _NONE_TYPE]
            if len(arms) != 1:
                return hint
            hint = arms[0]
        else:
            return hint


def _codec_kind(hint: Any) -> "str | None":
    """Which codec a class hint needs to cross JSON: ``model`` (pydantic v2, found by duck typing, never imported), ``datetime``, ``date``, ``time``, ``decimal``, ``uuid``, ``path``."""
    if not isinstance(hint, type):
        return None
    if hasattr(hint, "model_validate") and hasattr(hint, "model_dump"):
        return "model"
    for kind, base in (("datetime", datetime), ("date", date), ("time", dt_time), ("decimal", Decimal), ("uuid", uuid.UUID), ("path", pathlib.PurePath)):
        if issubclass(hint, base):
            return kind
    return None


def _json_hint(hint: Any, codecs: bool = True) -> bool:
    """Can a value of this type hint survive a JSON round trip (``codecs``: through a codec, see :func:`_encode`)?"""
    if hint is Any or hint is inspect.Parameter.empty or hint is None or hint is type(None):
        return True
    if hint in (str, int, float, bool, list, dict):
        return True
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _json_hint(args[0], codecs)
    if origin is typing.Literal:
        return all(isinstance(a, (str, int, float, bool)) or a is None for a in args)
    if origin is dict:
        return all(_json_hint(a, codecs and i == 1) for i, a in enumerate(args))  # a key is a JSON object key: never a codec type
    if origin is list or origin is typing.Union or type(hint).__name__ == "UnionType":
        return all(_json_hint(a, codecs) for a in args)
    if typing.is_typeddict(hint) or (dataclasses.is_dataclass(hint) and isinstance(hint, type)):
        return all(_json_hint(h) for h in typing.get_type_hints(hint).values())
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return all(isinstance(m.value, (str, int, float, bool)) for m in hint)
    if hasattr(hint, "__supertype__"):
        return _json_hint(hint.__supertype__, codecs)
    return codecs and _codec_kind(hint) is not None


def _needs_codec(hint: Any) -> bool:
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _needs_codec(args[0])
    if origin in (list, dict, typing.Union) or type(hint).__name__ == "UnionType":
        return any(_needs_codec(a) for a in args)
    return _codec_kind(hint) is not None


def _fits(value: Any, hint: Any) -> bool:
    """Is ``value`` an instance of what ``hint`` names (loosely: enough to pick a union arm)?"""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _fits(value, args[0])
    if hint is Any:
        return True
    try:
        return isinstance(value, origin or hint)
    except TypeError:
        return False


def _encode(value: Any, hint: Any) -> Any:
    """``value`` as JSON, for a parameter hinted ``hint``: a model as its JSON-mode dict, a datetime/date/time as ISO 8601, a Decimal/UUID/Path as a string. Anything else as it is."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _encode(value, args[0])
    if value is None:
        return None
    if origin is typing.Union or type(hint).__name__ == "UnionType":
        for arm in args:
            if arm is not _NONE_TYPE and _fits(value, arm):
                return _encode(value, arm)
        return value
    if origin is list and isinstance(value, (list, tuple)):
        return [_encode(v, args[0] if args else Any) for v in value]
    if origin is dict and isinstance(value, dict):
        return {k: _encode(v, args[1] if len(args) == 2 else Any) for k, v in value.items()}
    kind = _codec_kind(hint)
    if kind is None or not isinstance(value, hint):
        return value
    if kind == "model":
        return value.model_dump(mode="json")
    if kind in ("datetime", "date", "time"):
        return value.isoformat()
    return str(value)


def _decode(value: Any, hint: Any) -> Any:
    """The inverse of :func:`_encode`: what the tool's body is called with. Raises when a value is not the encoded form of its type."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _decode(value, args[0])
    if value is None:
        return None
    if origin is typing.Union or type(hint).__name__ == "UnionType":
        arms = [a for a in args if a is not _NONE_TYPE]
        for arm in sorted(arms, key=lambda a: not _needs_codec(a)):  # the arms that need a codec first: "2026-01-01" is a datetime where one is named
            try:
                if _needs_codec(arm):
                    return _decode(value, arm)
                if _fits(value, arm):
                    return value
            except Exception:
                continue
        return value
    if origin is list and isinstance(value, list):
        return [_decode(v, args[0] if args else Any) for v in value]
    if origin is dict and isinstance(value, dict):
        return {k: _decode(v, args[1] if len(args) == 2 else Any) for k, v in value.items()}
    kind = _codec_kind(hint)
    if kind is None:
        return value
    if kind == "model":
        return hint.model_validate(value)
    if kind == "datetime":
        return hint.fromisoformat(value)
    if kind == "date":
        return hint.fromisoformat(value)
    if kind == "time":
        return hint.fromisoformat(value)
    if kind == "decimal":
        return hint(value)
    return hint(value)  # uuid.UUID, a Path class


def _why(hint: Any) -> str:
    """What is wrong with a hint that has no JSON form, in a few words."""
    cls = typing.get_origin(hint) or hint
    if cls is collections.abc.Callable or (callable(hint) and not isinstance(hint, type)):
        return "a callable"
    if isinstance(cls, type):
        if issubclass(cls, (io.IOBase, typing.IO)):
            return "a file object"
        return f"{cls.__name__}, which has no JSON form"
    return repr(hint)


class UnresolvedHint(Exception):
    """A parameter's type hint cannot be resolved and nothing marks the parameter as supplied by the app."""

    def __init__(self, parameter: str, text: str) -> None:
        super().__init__(f"the type hint of {parameter!r} cannot be resolved")
        self.parameter, self.text = parameter, text


class _Placeholder:
    """Stands in for a name a hint uses but the module cannot resolve at run time (a class imported only under ``TYPE_CHECKING``). Never a type: only a parameter explicitly marked injected may carry one."""


class _Lenient(dict):
    """Evaluation names for one annotation string: the function's own globals and builtins first, an inert placeholder for anything else (recorded in ``used``)."""

    def __init__(self, namespace: dict) -> None:
        super().__init__()
        self.namespace, self.used = namespace, []

    def __missing__(self, key: str) -> Any:
        import builtins

        if key in self.namespace:
            return self.namespace[key]
        if hasattr(builtins, key):
            return getattr(builtins, key)
        self.used.append(key)
        return _Placeholder


def resolve_hints(func: Callable) -> dict:
    """The resolved hints of ``func`` (``include_extras``).

    When the strict resolution fails (a class imported only under ``TYPE_CHECKING``, a forward reference), each parameter is read on its own: a hint that does not resolve
    is accepted ONLY on a parameter explicitly marked injected (``Annotated[..., runbound.Injected]``), where the unknown name becomes an inert placeholder (the app supplies
    the value, so its type is never needed). Any other unresolvable parameter raises :class:`UnresolvedHint`.
    """
    target = inspect.unwrap(func)
    try:
        return typing.get_type_hints(target, include_extras=True)
    except Exception:
        pass
    namespace = getattr(target, "__globals__", {})
    hints = {}
    for name, annotation in getattr(target, "__annotations__", {}).items():
        if name == "return":
            continue
        if isinstance(annotation, str):
            names = _Lenient(namespace)
            try:
                hint = eval(annotation, namespace, names)  # noqa: S307 - an annotation string, as typing.get_type_hints evaluates it
            except Exception:
                raise UnresolvedHint(name, annotation) from None
            unresolved = bool(names.used)
        else:
            hint, unresolved = annotation, "ForwardRef(" in repr(annotation)
        if unresolved and not is_injected(hint):
            raise UnresolvedHint(name, annotation if isinstance(annotation, str) else repr(annotation))
        hints[name] = hint
    return hints


def unfit_parameters(func: Callable) -> list:
    """``[(parameter, why), ...]``: the parameters of ``func`` a deferred approval could not store as JSON. An injected parameter is never one of them.

    Raises when the type hints cannot be resolved.
    """
    signature = inspect.signature(func)
    hints = resolve_hints(func)
    bad = []
    for p in signature.parameters.values():
        hint = hints.get(p.name, inspect.Parameter.empty)
        if p.kind is p.VAR_POSITIONAL:
            bad.append((f"*{p.name}", "variable positional arguments cannot be stored"))
        elif not is_injected(hint) and not _json_hint(hint):
            bad.append((p.name, _why(hint)))
    return bad


def check_registration(func: Callable, name: str, approval: Any, mode: Any, timeout_s: Any) -> ApprovalSpec | None:
    """Validate ``@tool(approval=...)`` keywords; raises ``ValueError`` on nonsense. ``None`` when off."""
    if approval is None:
        if mode is not None or timeout_s is not None:
            raise ValueError("@runbound.tool(mode=..., timeout_s=...) need approval=\"required\"")
        return None
    if approval != "required":
        raise ValueError(f'@runbound.tool(approval=...) must be "required" or None, got {approval!r}')
    mode = mode or "sync"
    if mode not in ("sync", "deferred"):
        raise ValueError(f'@runbound.tool(mode=...) must be "sync" or "deferred", got {mode!r}')
    timeout_s = 600.0 if timeout_s is None else timeout_s
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 604_800:
        raise ValueError(f"@runbound.tool(timeout_s=...) must be in (0, 604800], got {timeout_s!r}")
    signature = inspect.signature(func)
    if mode == "deferred":
        try:
            bad = unfit_parameters(func)
        except Exception as exc:
            param = getattr(exc, "parameter", None)
            text = str(getattr(exc, "text", "") or "").strip("'\"")
            raise ValueError(
                f"tool {name!r} cannot be mode=\"deferred\": its type hints cannot be resolved"
                + (f" (parameter {param!r}: {text!r})" if param else "")
                + ", so its arguments cannot be shown to be JSON (import what they name, or use mode=\"sync\"); "
                + (f"if the app supplies {param!r}, annotate it Annotated[{text}, runbound.Injected]" if param else "if the app supplies a parameter, annotate it Annotated[<type>, runbound.Injected]")
            ) from None
        if bad:
            raise ValueError(
                f"tool {name!r} cannot be mode=\"deferred\": its arguments are stored and replayed as JSON, and "
                + "; ".join(f"parameter {param!r} ({why}) cannot be" if not param.startswith("*") else f"{param!r} ({why})" for param, why in bad)
                + ". Mark a parameter runbound.Injected if the app supplies it (it is then never stored), give it a JSON-friendly type, or use mode=\"sync\""
            )
    return ApprovalSpec(mode, float(timeout_s), tuple(signature.parameters))


_META: dict = {}  # tool -> (parameter names, are its arguments JSON-friendly), for approvals a row asks for
_SIGS: dict = {}  # tool -> its inspect.Signature, to bind a call's arguments by name for a rule's conditions


def json_friendly(func: Callable) -> bool:
    """Can this tool's arguments be stored and replayed as JSON (so a row may make it ``deferred``)? Injected parameters are not stored, so they do not count."""
    try:
        return not unfit_parameters(func)
    except Exception:
        return False


@dataclasses.dataclass(frozen=True)
class Shape:
    """What a tool's parameters need to cross a deferred approval: which the app supplies (``injected``, in signature order) and which need a codec (``codecs``: name -> hint)."""

    injected: tuple = ()
    codecs: dict = dataclasses.field(default_factory=dict)
    hints: dict = dataclasses.field(default_factory=dict)


_SHAPES: dict = {}  # tool -> Shape, only for a tool that has an injected or a codec parameter


def _shape_of(func: Callable) -> "Shape | None":
    try:
        signature = inspect.signature(func)
        hints = resolve_hints(func)
    except Exception:
        return None
    injected = tuple(p.name for p in signature.parameters.values() if is_injected(hints.get(p.name, inspect.Parameter.empty)))
    codecs = {p.name: hints[p.name] for p in signature.parameters.values()
              if p.name in hints and p.name not in injected and _needs_codec(hints[p.name])}
    return Shape(injected, codecs, hints) if injected or codecs else None


def register_tool(name: str, func: Callable, executor: Callable) -> None:
    """Every decorated tool: how to run it once approved, and what a row needs to know to ask about it."""
    try:
        names = tuple(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        names = ()
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        signature = None
    with _LOCK:
        _EXECUTORS.setdefault(name, executor)
        _META[name] = (names, json_friendly(func))
        _SIGS[name] = signature
        shape = _shape_of(func)
        if shape is None:
            _SHAPES.pop(name, None)
        else:
            _SHAPES[name] = shape


def bound_arguments(name: str, args: tuple, kwargs: dict) -> "dict | None":
    """The call's arguments by parameter name, defaults applied, as a console rule's conditions read them; ``None`` when they cannot be bound."""
    signature = _SIGS.get(name)
    if signature is None:
        return None
    try:
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
    except TypeError:
        return None
    shape = _SHAPES.get(name)
    values = {key: value for key, value in bound.arguments.items()
              if signature.parameters[key].kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
              and (shape is None or key not in shape.injected)}  # an injected parameter is never read by a rule
    if shape is not None:
        for key, hint in shape.codecs.items():
            if key in values:
                values[key] = _encode(values[key], hint)  # a rule reads the encoded form, the one a person is shown
    return values


def public_call(name: str, args: tuple, kwargs: dict) -> tuple:
    """``(args, kwargs)`` of a call without its injected parameters, for the span's argument digest; the call unchanged when the tool has none."""
    shape = _SHAPES.get(name)
    signature = _SIGS.get(name)
    if shape is None or not shape.injected or signature is None:
        return args, kwargs
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:
        return args, kwargs
    return (), {key: value for key, value in bound.arguments.items() if key not in shape.injected}


def tool_meta(name: str) -> tuple:
    return _META.get(name, ((), False))


def register(name: str, spec: ApprovalSpec, executor: Callable, secret: str | None) -> None:
    """Remember how to execute a deferred tool, and say once if it cannot fail over."""
    if spec.mode != "deferred":
        return
    with _LOCK:
        _EXECUTORS[name] = executor
    if not secret:
        _warn_once(
            ("approval-no-secret",),
            "runbound: deferred approvals are registered but no approval secret is set "
            "(RUNBOUND_APPROVAL_SECRET or init(approval_secret=)): requests are sent unsealed, so "
            "only this worker can execute them, from its own memory",
        )


# --- slot 5: ask ------------------------------------------------------------------------------------


def _normalise(spec: ApprovalSpec, call: Any) -> dict:
    """The call's arguments as one JSON object (name -> value), the shape stored and replayed."""
    values = dict(call.kwargs)
    positional = [n for n in spec.names if n not in values]
    for name, value in zip(positional, call.args):
        values[name] = value
    shape = _SHAPES.get(call.name)
    if shape is not None:
        for name in shape.injected:
            values.pop(name, None)  # the app supplies it again at execution: it is never stored, sealed, digested or shown
        for name, hint in shape.codecs.items():
            if name in values:
                original = values[name]
                values[name] = _encode(original, hint)
                if spec.mode == "deferred":
                    _must_round_trip(name, original, values[name], hint)
    return values


def _must_round_trip(name: str, original: Any, encoded: Any, hint: Any) -> None:
    """A deferred request replays the DECODED JSON, so it must decode to what the caller passed. A model's JSON form is not always its value (a secret dumps masked, an
    excluded field is dropped, an alias is not read back, a custom serializer reshapes it): running the approved tool with that would be silent corruption, so the call is
    refused here, before any approval exists, and the tool never runs."""
    try:
        back = _decode(encoded, hint)
        same = bool(back == original)
        why = "the value read back from its JSON form is not equal to the value passed (a secret, an excluded field, an alias or a custom serializer changes it)"
    except Exception as exc:  # noqa: BLE001 - whatever the codec raised is the reason
        same, why = False, f"{type(exc).__name__}: {str(exc)[:160]}"
    if not same:
        raise StageRefused("arguments", f"parameter {name!r} cannot round-trip through JSON: {why}; use mode=\"sync\" or a JSON-friendly type")


def _sleep_until(deadline: float, seconds: float) -> None:
    time.sleep(max(0.0, min(seconds, deadline - time.monotonic())))


def admit(engine: Any, session: Any, call: Any, spec: ApprovalSpec, info: dict) -> None:
    """Slot 5 for a tool that needs a person. Returns when approved; otherwise raises.

    ``StageRefused`` (denied, timed out, unavailable) or :class:`Deferred` (a deferred request was
    filed). A call that *is* the execution of an approved deferred request skips the question.
    """
    resumed = _CURRENT.get()
    if resumed is not None:
        info["request_id"] = resumed  # the span of the resumed execution names the request a person approved
        approval_id = _APPROVAL.get()
        if approval_id:
            info["approval_id"] = approval_id  # so the execution-entry re-check can tell the plane this approved request did not run (it holds a quota unit)
        claim = _CLAIM.get() or {}
        if claim.get("bind_required"):  # a bind-required request: this run must bind under its claim before the body (RFC 0a.2)
            info.update(bind_required=True, claim_token=claim.get("claim_token"), claim=claim)
            if claim.get("quota_held") is False:
                info["quota_held"] = False
            try:
                info["args_digest"] = args_digest(call.name, _normalise(spec, call))
            except Exception:
                info["args_digest"] = None
        return
    suspend = info.get("suspend")
    if suspend is not None and not getattr(engine.config, "approval_bind", False):  # a suspended request is bind-required for life: without the capability there is nothing safe to file
        raise StageRefused("approval", "suspended approvals need approval_bind=True (the plane must advertise bind_v1); nothing was asked", PLANE_TOO_OLD)
    if info.get("fresh_only") and suspend is None:  # a boundary that supports no approval wait or resume (the early LangChain path): refuse BEFORE anything is asked or filed, never run the call
        raise StageRefused("approval", "this call needs a person's approval, which this boundary does not support (unsupported); nothing was asked and the tool did not run", UNAVAILABLE)
    config, shared = engine.config, engine.shared
    unavailable = getattr(shared, "approval_refusal", lambda: None)()
    if unavailable:  # a new request needs a connected link; a long-poll already in flight rides out an outage
        raise StageRefused("approval", unavailable, UNAVAILABLE)
    if not getattr(shared, "fleet", False):  # nobody to ask: fail closed, at once
        raise StageRefused("approval", "no control plane is connected to ask for approval", UNAVAILABLE)
    args = _normalise(spec, call)
    try:
        json.dumps(args)
    except (TypeError, ValueError):
        raise StageRefused("approval", "the arguments cannot be sent as JSON", UNAVAILABLE) from None
    key_digest = key_hash(session.key) if getattr(session, "key", None) else None
    blocked = _QUOTA_BLOCKED.get((config.service, call.name, key_digest))
    if blocked is not None:  # the plane said this quota is full for the rest of its window: do not ask again
        if time.monotonic() < blocked:
            raise StageRefused("approval", "the tool's quota is exhausted for this window", QUOTA_EXCEEDED,
                               max(blocked - time.monotonic(), 0.0))
        _QUOTA_BLOCKED.pop((config.service, call.name, key_digest), None)
    request_id = (suspend or {}).get("request_id") or uuid.uuid4().hex  # a replay-safe attempt names its own request, so a replayed node reaches the same approval
    body: dict = {
        "tool": call.name, "request_id": request_id, "args": args, "mode": "suspended" if suspend is not None else spec.mode, "timeout_s": spec.timeout_s,
        "service": config.service, "worker_id": config.resolved_worker_id(),
        "run_id": getattr(session, "session_id", None),
    }
    if key_digest:
        body["key"] = key_digest  # per-key quotas
    requested_for = _REQUESTED_FOR.get()
    if requested_for:
        body["requested_for"] = requested_for
    if getattr(config, "approval_bind", False) or suspend is not None:
        body["capabilities"] = ["bind_v1"]  # ask for a request the plane will not let two attempts execute (legacy planes ignore the key and file a legacy request)
    body["args_digest"] = args_digest(call.name, args)  # the plane recomputes it: a different canonical form fails loudly (400), never silently asks again
    if spec.requirement:
        body["requirement"] = spec.requirement
    for field in ("action_id", "attempt_id", "evaluation_id"):
        value = (info.get("ids") or {}).get(field)
        if value:
            body[field] = value  # the identities of the call that asked, so the request joins its span and its refusal
    record = info.get("record") or {}
    for field in ("reason_code", "matched_rule_ids", "policy_version", "service_default_revision", "policy_state", "contributors", "sdk_version",
                  "rule_revision", "cfg_hash", "boot_id"):
        if record.get(field) is not None:
            body[field] = record[field]  # the decision record rides on the request, so "why is this waiting" reads by approval id
    secret = config.approval_secret
    if spec.mode == "deferred" and secret:
        body["seal"] = make_seal(secret, config.service, call.name, request_id, args, spec.timeout_s)
    deadline = time.monotonic() + spec.timeout_s
    try:
        approval = _file(shared, body, deadline, (config.service, call.name, key_digest))
    except StageRefused as refused:
        if suspend is not None and refused.http_status == 400:  # a plane that does not know the mode refuses it loudly: never fall back to sync or deferred
            raise StageRefused("approval", "the control plane does not support suspended approvals; nothing was asked", PLANE_TOO_OLD) from None
        raise
    if suspend is not None and approval.get("granted") is not True and approval.get("bind_required") is not True:
        if approval.get("id"):  # filed as something other than a bind-required request (a plane with the bind off): take it back, never wait on it
            try:
                withdraw(shared, request_id, "plane_too_old", approval.get("id"))
            except Exception:
                pass
        raise StageRefused("approval", "the control plane did not file a bind-required request; suspended approval is unavailable", PLANE_TOO_OLD)
    if approval.get("granted") is True:  # an active session/minutes grant admits this call: nobody is asked, nothing waits, the plane took no quota unit
        grant_id = str(approval["grant_id"])
        if isinstance(record.get("contributors"), list):
            record["contributors"] = [*record["contributors"], f"grant:{grant_id}"]
        info.update(request_id=request_id, granted=grant_id, quota=None)
        return
    # ``approval["quota"]`` non-null means the plane already took this call's quota unit: never take another.
    info.update(request_id=request_id, approval_id=approval.get("id"), quota=approval.get("quota"))
    if approval.get("bind_required") is True:
        info.update(bind_required=True, args_digest=body["args_digest"])  # the executing worker binds under the claim the plane gives it
    if suspend is not None:
        info.update(bind_required=True, args_digest=body["args_digest"])
        raise Suspended(request_id, approval.get("id"), body["args_digest"], (info.get("ids") or {}).get("attempt_id"))
    if spec.mode == "deferred":
        with _LOCK:
            _PENDING[request_id] = Pending(call.name, request_id, args, approval.get("id"), body["run_id"])
            while len(_PENDING) > PENDING_MAX:
                _PENDING.popitem(last=False)
        raise Deferred({"status": PENDING_RESULT_STATUS, "request_id": request_id}, approval.get("id"))
    info["sync"] = True
    stop = info.get("stop")
    if stop is not None and stop():  # abandoned before the request was filed (the cancel could not name it yet): take it back here, and never wait
        withdrew = withdraw(shared, request_id, "cancelled_by_caller", approval.get("id"))
        if withdrew.status == "not_withdrawable" and approval.get("id"):
            report_async(engine, str(approval["id"]), "superseded", reason="cancelled_by_caller")
        raise StageRefused("approval", "the invocation was abandoned before the request was decided; nothing will be dispatched", UNAVAILABLE)
    decided = _wait(shared, approval, deadline, stop, request_id)
    if isinstance(decided, dict):
        approval = decided
    if approval.get("bind_required") is True:  # the plane filed a bind-required request: bind before the body
        info.update(bind_required=True, args_digest=body["args_digest"])
        if approval.get("quota_held") is False:
            info["quota_held"] = False


def args_digest(tool: str, args: dict) -> str:
    """The digest the plane recomputes for a request: sha256 of canonical JSON ``{"tool", "args"}`` (the arguments as the plane will parse them)."""
    sent = json.loads(json.dumps(args))
    text = json.dumps({"tool": tool, "args": sent}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file(shared: Any, body: dict, deadline: float, quota_key: tuple = ()) -> dict:
    """``POST /v1/approvals`` until the plane takes it (idempotent on ``request_id``) or time runs out."""
    delay = 0.25
    while True:
        status, data = shared.approvals("POST", "/v1/approvals", body, timeout=5.0)
        if status in (200, 201) and isinstance(data, dict) and data.get("id"):
            return data
        if status == 200 and isinstance(data, dict) and data.get("granted") is True and isinstance(data.get("grant_id"), str) and data["grant_id"]:
            return data
        if status == 429:  # an enforced quota is full: no approval was created, and none may be retried now
            wait = data.get("retry_after_s") if isinstance(data, dict) else None
            wait = float(wait) if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait >= 0 else 60.0
            _QUOTA_BLOCKED[quota_key] = time.monotonic() + wait
            raise StageRefused("approval", "the tool's quota is exhausted for this window", QUOTA_EXCEEDED, wait)
        if status == 409:
            raise StageRefused("approval", "the approval request conflicts with an earlier one", UNAVAILABLE)
        if status in (400, 401, 403, 404, 422):
            refused = StageRefused("approval", f"the control plane refused the approval request (HTTP {status})", UNAVAILABLE)
            refused.http_status = status
            raise refused
        if time.monotonic() >= deadline:  # status None (transport) or a 5xx that never cleared
            raise StageRefused("approval", "the control plane could not be reached to ask for approval", TIMEOUT)
        _sleep_until(deadline, delay)
        delay = min(delay * 2, 5.0)


def _wait(shared: Any, approval: dict, deadline: float, stop: Any = None, request_id: "str | None" = None) -> "dict | None":
    """Long-poll until the request is decided. Never treats a transport error as a denial.

    ``stop`` (AAI-04-4a, the LangChain local fence) is polled between polls: an abandoned invocation, or a request this process withdrew, stops waiting at once, whatever the plane
    says, so the thread can never go on to dispatch a call nobody is waiting for. The request itself is withdrawn by whoever abandoned it."""
    approval_id = approval["id"]
    delay = 0.25
    while True:
        if (stop is not None and stop()) or (request_id is not None and request_id in _WITHDRAWN):
            raise StageRefused("approval", "the invocation was abandoned before the request was decided; nothing will be dispatched", UNAVAILABLE)
        status = approval.get("status")
        if status == "approved":
            return approval
        if status == "denied":
            reason = approval.get("decision_reason")
            raise StageRefused("approval", "a person denied the request" + (f": {reason}" if reason else ""), DENIED)
        if status in ("expired", "approved_unconsumed", "failed", "executed", "claimed"):
            raise StageRefused("approval", "the approval request expired", TIMEOUT)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StageRefused("approval", "no one decided the request in time", TIMEOUT)
        wait = int(max(1, min(LONG_POLL_S, remaining)))
        code, data = shared.approvals("GET", f"/v1/approvals/{approval_id}?wait_s={wait}", None, timeout=wait + 5.0)
        if code == 200 and isinstance(data, dict):
            approval, delay = data, 0.25
            continue
        if code in (401, 403, 404):
            raise StageRefused("approval", f"the control plane stopped answering the request (HTTP {code})", UNAVAILABLE)
        _sleep_until(deadline, delay)  # transport error or 5xx: retry until the deadline
        delay = min(delay * 2, 5.0)


#: The report outcomes every plane that has the report route accepts. A newer SDK that reports another (a future ``cancelled`` or ``withdrawn``) to an older plane gets a 400:
#: that is a plane too old for this SDK, counted as such, never requeued forever.
OUTCOMES_OLD_PLANES_KNOW = frozenset({"executed", "failed", "tampered", "clock_skew", "superseded"})

#: HTTP statuses after which a report is settled: delivered, or refused for good (retrying cannot help).
_SETTLED = frozenset({200, 201, 400, 401, 403, 404, 409})


class ReportQueue:
    """Reports the plane could not take: an append-only ``reports.jsonl`` next to the policy cache.

    An executed-but-unreported deferred action is an audit gap, and the plane would expire the row and
    release the quota unit for something that actually ran. So when ``/report`` cannot be delivered the
    outcome is appended here (``0600``, fsynced; an approval id, an outcome, a digest and a worker id,
    **never** an argument or a result) and drained on the next connected hello. Idempotent on
    ``(approval id, outcome)``: the plane's report is too (``first`` / ``repeat``). Memory only when
    there is no cache directory.
    """

    def __init__(self, path: Any) -> None:
        self._path = path
        self._memory: list = []
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            if self._path is None:
                self._memory.append(record)
                return
            try:
                fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                self._memory.append(record)

    def _read(self) -> list:
        seen, out = set(), []
        lines = []
        if self._path is not None:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        records = [r for r in (self._parse(line) for line in lines) if r] + self._memory
        for record in records:
            key = (record.get("approval_id"), record.get("outcome"))
            if key not in seen:
                seen.add(key)
                out.append(record)
        return out

    @staticmethod
    def _parse(line: str) -> dict | None:
        try:
            record = json.loads(line)
        except ValueError:
            return None  # a torn last line from a crash: skip it
        return record if isinstance(record, dict) and record.get("approval_id") and record.get("outcome") else None

    def pending(self) -> int:
        with self._lock:
            return len(self._read())

    def drain(self, send: Callable[[dict], bool], limit: int = 50) -> int:
        """Send what is queued with ``send`` (True = settled); keep the rest. Returns how many settled."""
        with self._lock:
            records = self._read()
            if not records:
                return 0
            keep, settled = [], 0
            for index, record in enumerate(records):
                if index >= limit or not send(record):
                    keep.append(record)
                else:
                    settled += 1
            self._memory = []
            if self._path is None:
                self._memory = keep
                return settled
            try:
                if not keep:
                    self._path.unlink(missing_ok=True)
                else:
                    temp = self._path.with_suffix(".tmp")
                    temp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in keep), encoding="utf-8")
                    os.chmod(temp, 0o600)
                    os.replace(temp, self._path)
            except OSError:
                self._memory = keep
            return settled


def _post_report(shared: Any, record: dict, attempts: int = 1) -> bool:
    """One report to the plane; True when it is settled (delivered, or refused for good)."""
    body = {"outcome": record["outcome"], "worker_id": record.get("worker_id")}
    if record.get("result_digest"):
        body["result_digest"] = record["result_digest"]
    if record.get("reason"):
        body["reason"] = record["reason"]
    if isinstance(record.get("record"), dict) and record["record"]:
        body["record"] = record["record"]  # a superseded call's decision record, both phases (WIRE-VPC 12e)
    for attempt in range(attempts):
        status, _ = shared.approvals("POST", f"/v1/approvals/{record['approval_id']}/report", body, timeout=5.0)
        if status in _SETTLED:
            if status not in (200, 201):  # refused for good: counted, by why (an outcome this plane predates is not "the plane is broken")
                too_old = status in (400, 422) and record["outcome"] not in OUTCOMES_OLD_PLANES_KNOW
                delivery.dropped("approval_reports", "plane_too_old" if too_old else f"http_{status}")
            return True
        if attempt + 1 < attempts:
            time.sleep(0.5 * 2 ** attempt)
    return False


def drain_reports(shared: Any) -> None:
    """On a connected hello: send every report that could not go earlier. Never raises."""
    queue = getattr(shared, "reports", None)
    if queue is None:
        return
    try:
        queue.drain(lambda record: _post_report(shared, record))
    except Exception:
        _LOG.debug("runbound: could not drain the report queue", exc_info=True)


_REPORT_RECORD_KEYS = ("reason_code", "matched_rule_ids", "policy_version", "service_default_revision", "policy_state", "contributors", "sdk_version", "rule_revision",
                       "cfg_hash", "boot_id", "recheck_rule_revision", "recheck_cfg_hash", "recheck_policy_version", "recheck_service_default_revision",
                       "recheck_contributor", "recheck_reason")


def _report_record(record: dict) -> dict:
    """The decision-record keys a ``superseded`` report carries (both phases); only what is known."""
    return {key: record[key] for key in _REPORT_RECORD_KEYS if record.get(key) is not None}


#: The report outcome for a call cancelled after its body began. The plane has no ``cancelled`` status yet (the new status is held for D2/D3), so the closest existing one is used: the
#: call did not finish, and the quota unit it held is kept because the body may have run. Changing it to the new status when Codex rules is this one line.
CANCELLED_REPORT_OUTCOME = "failed"


def report_async(engine: Any, approval_id: str, outcome: str, result: Any = None, reason: "str | None" = None, decision_record: "dict | None" = None) -> None:
    """Tell the plane how an approved call went (``executed`` | ``failed`` | ``tampered`` | ``clock_skew`` | ``superseded``).

    ``reason`` names why a ``superseded`` call did not run (the refusal code); the plane returns the quota unit it held.
    ``decision_record`` is a ``superseded`` call's decision record, both phases (what it was approved under, what the execution-entry re-check read; WIRE-VPC 12e), so the
    request keeps it and an explain by approval id shows it.

    Tried a few times; if the plane cannot take it, queued for the next connected hello. Never raises."""

    def run() -> None:
        try:
            record = {"approval_id": approval_id, "outcome": outcome, "worker_id": engine.config.resolved_worker_id()}
            if reason:
                record["reason"] = str(reason)[:64]
            kept = _report_record(decision_record) if outcome == "superseded" and isinstance(decision_record, dict) else {}
            if kept:
                record["record"] = kept
            if result is not None:
                record["result_digest"] = hashlib.sha256(
                    json.dumps(result, sort_keys=True, default=str).encode("utf-8", "replace")).hexdigest()
            if not _post_report(engine.shared, record, attempts=4):
                queue = getattr(engine.shared, "reports", None)
                if queue is not None:
                    queue.append({**record, "queued_at": time.time()})
                    _LOG.warning("runbound: could not report approval %s (%s); queued for the next connected hello",
                                 approval_id, outcome)
        except Exception:
            _LOG.debug("runbound: could not report approval %s", approval_id, exc_info=True)

    threading.Thread(target=run, name="runbound-approval-report", daemon=True).start()


# --- the heartbeat hands us approved deferred requests ----------------------------------------------


def handle_approved(items: list) -> None:
    """``hello.approved[]``: execute what this worker may, each on its own thread. Never raises."""
    for item in items:
        try:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            with _LOCK:
                if item["id"] in _INFLIGHT:
                    continue
                _INFLIGHT.add(item["id"])
            threading.Thread(target=_execute, args=(item,), name="runbound-approved", daemon=True).start()
        except Exception:
            _LOG.warning("runbound: could not start an approved request", exc_info=True)


def _engine() -> Any:
    from . import api

    return api._ENGINE


def _run_as(run_id: Any) -> Any:
    """The context a resumed execution runs in: bound to the run that filed it (see :func:`runbound.api._run_as`)."""
    from . import api

    if run_id is not None and not (isinstance(run_id, str) and 0 < len(run_id) <= api._RUN_ID_MAX):
        _warn_once(("claim-run-id",), "runbound: an approved request carried a run_id that is not a string of 1 to %d "
                   "characters; ignored, the call runs under the default run", api._RUN_ID_MAX)
        run_id = None  # a bad label never fails a claim
    return api._run_as(run_id)


def _call_back(info: dict) -> None:
    engine = _engine()
    callback = getattr(getattr(engine, "config", None), "on_deferred_result", None)
    if callback is None:
        return
    try:
        callback(info)
    except Exception:
        _LOG.warning("runbound: on_deferred_result raised; ignored", exc_info=True)


def _execute(item: dict) -> None:
    """Verify, run and report one approved deferred request."""
    approval_id, tool, request_id = item["id"], item.get("tool"), item.get("request_id")
    try:
        engine = _engine()
        executor = _EXECUTORS.get(tool)
        if engine is None or executor is None:
            return  # not a tool this worker has; the plane offers it again
        config = engine.config
        service = item.get("service") or config.service
        own = _PENDING.get(request_id)  # parked on this worker, if it filed the request
        if item.get("seal") is not None:
            problem = verify_seal(config.approval_secret, item["seal"], service, tool, request_id, item.get("args"))
            args = item.get("args")
        else:
            if own is None:
                return  # unsealed and not parked here: only the requesting worker may run it
            problem = None if own.args == item.get("args") else "the arguments differ from the ones parked"
            args = own.args
        if problem:
            _LOG.warning("runbound: approved request for %r refused: %s", tool, problem)
            skew = problem == CLOCK_SKEW_REASON  # a valid MAC with a stale not_after: this worker's clock, not an attacker
            outcome = "clock_skew" if skew else "tampered"
            report_async(engine, approval_id, outcome)
            _call_back({"request_id": request_id, "tool": tool, "outcome": outcome, "error": "clock_skew" if skew else TAMPERED})
            return
        claim = {"bind_required": item.get("bind_required") is True, "claim_token": item.get("claim_token"), "quota_held": item.get("quota_held"), "lost": False}
        if request_id in _WITHDRAWN:  # the caller gave up on it: never dispatched here
            _LOG.warning("runbound: approved request %s was withdrawn; not run", approval_id)
            return
        token, approval_token, claim_token = _CURRENT.set(request_id), _APPROVAL.set(approval_id), _CLAIM.set(claim)
        with _LOCK:
            _EXECUTING.add(approval_id)
        try:
            with _run_as((own.run_id if own is not None else None) or item.get("run_id")):
                result = executor(**_rebuild(engine, tool, args))
            outcome = "executed"
        except BaseException as exc:  # the tool's own failure, a parameter that could not be supplied, or a refusal by the chain
            if claim["lost"]:  # the plane gave this request to another worker: that one owns it now. Not run, not reported, not called back
                result, outcome = None, "dropped"
                _LOG.warning("runbound: the claim on approved request %s was lost before it could be bound; dropped, the new owner decides", approval_id)
            elif getattr(exc, "reason", None) == PRIOR_OUTCOME_UNKNOWN:  # bound elsewhere or already: the other attempt owns the outcome, so this one reports nothing
                result, outcome = None, "unknown"
                _call_back({"request_id": request_id, "tool": tool, "outcome": "failed", "error": PRIOR_OUTCOME_UNKNOWN})
            elif getattr(exc, "reason", None) == "approval_superseded":  # the re-check refused it and has already told the plane (``superseded``, with the record): not a failure
                result, outcome = None, "superseded"
                _call_back({"request_id": request_id, "tool": tool, "outcome": "superseded", "error": "approval_superseded"})
            else:
                result, outcome = None, "failed"
                _call_back({"request_id": request_id, "tool": tool, "outcome": "failed", "error": exc.code if isinstance(exc, Unrunnable) else type(exc).__name__})
        finally:
            _CURRENT.reset(token)
            _APPROVAL.reset(approval_token)
            _CLAIM.reset(claim_token)
            with _LOCK:
                _EXECUTING.discard(approval_id)
        if outcome == "executed":
            report_async(engine, approval_id, "executed", result)
            _call_back({"request_id": request_id, "tool": tool, "outcome": "executed", "result": result})
        elif outcome == "failed":
            report_async(engine, approval_id, "failed")
        with _LOCK:
            _PENDING.pop(request_id, None)
    except Exception:
        _LOG.warning("runbound: could not execute approved request %s", approval_id, exc_info=True)
    finally:
        with _LOCK:
            _INFLIGHT.discard(approval_id)


class Unrunnable(Exception):
    """An approved request that cannot be run because a parameter could not be supplied: the request ends ``failed`` with ``code`` as its error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _provider(provide: dict, name: str, hint: Any) -> Any:
    """The zero-argument provider for an injected parameter: by parameter name first, else by type (the hint's class, then its bases), else ``None``."""
    if name in provide:
        return provide[name]
    cls = _inner(hint)
    if cls is _Placeholder:
        return None  # the class was not importable here: only the parameter's name (or its default) can supply it
    for base in getattr(cls, "__mro__", (cls,)):
        try:
            if base in provide:
                return provide[base]
        except TypeError:
            continue
    return None


def _rebuild(engine: Any, tool: str, args: dict) -> dict:
    """The keyword arguments an approved request runs the tool with: the stored JSON decoded back to the types the tool declares, and every injected parameter supplied afresh.

    An injected parameter comes from ``init(provide=...)`` (by name, then by type), else from its default (a callable default is called, a plain one used as is);
    with neither the request is not run (:class:`Unrunnable`, ``injected_unavailable:<param>``). A provider is called on every execution, so nothing is held across the wait.
    """
    shape = _SHAPES.get(tool)
    if shape is None:
        return args
    values = {key: value for key, value in args.items() if key not in shape.injected}  # an injected value never comes from the wire
    for name, hint in shape.codecs.items():
        if name in values:
            values[name] = _decode(values[name], hint)
    signature = _SIGS.get(tool)
    provide = getattr(engine.config, "provide", None) or {}
    for name in shape.injected:
        provider = _provider(provide, name, shape.hints.get(name))
        if provider is not None:
            values[name] = provider()
            continue
        default = signature.parameters[name].default if signature is not None else inspect.Parameter.empty
        if default is inspect.Parameter.empty:
            _warn_once(("injected-unavailable", tool, name),
                       "runbound: tool %r is approved but its injected parameter %r has no provider (init(provide={...})) and no default; the request is not run", tool, name)
            raise Unrunnable(f"injected_unavailable:{name}")
        values[name] = default() if callable(default) else default
    return values


def run_executor(func: Callable) -> Callable:
    """A callable that runs ``func(**args)``; an async tool is awaited on its own loop."""
    if inspect.iscoroutinefunction(func):
        return lambda **kw: asyncio.run(func(**kw))
    return lambda **kw: func(**kw)


# --- the execution bind (RFC 0a.2, D2) ------------------------------------------------------------


def bind(engine: Any, call: Any, info: dict) -> None:
    """Bind an approved, bind-required request to THIS attempt before its body runs. A no-op for a legacy request.

    ``POST /v1/approvals/{id}/bind {attempt_id, action_id, args_digest, claim_token?}``. ``first: true`` lets the call go on. A bind that cannot be made (transport, a 5xx, 401/403/404,
    ``503 downgrade_in_progress``) fails CLOSED whatever the tool's fail mode: ``approval_unavailable``. ``first: false`` and every 409 (``bound_to_other_attempt``, ``claim_lost``,
    ``not_approved``, ``approval_expired``, ``approval_binding_mismatch``, a terminal status) mean the request may already have run: ``prior_outcome_unknown``, never dispatched. A lost
    claim on a deferred execution also marks the job dropped (``_execute`` neither reports nor calls back).
    """
    approval_id = info.get("approval_id")
    if not info.get("bind_required") or not approval_id:
        return
    ids = info.get("ids") or {}
    body: dict = {"attempt_id": ids.get("attempt_id") or ids_module.mint(), "action_id": ids.get("action_id") or ids_module.mint(), "args_digest": info.get("args_digest")}
    if info.get("claim_token"):
        body["claim_token"] = info["claim_token"]
    status, data = engine.shared.approvals("POST", f"/v1/approvals/{approval_id}/bind", body, timeout=BIND_TIMEOUT_S)
    if status == 200 and isinstance(data, dict):
        if data.get("first") is True:
            info["bound"] = True
            return
        raise StageRefused("approval", "this request was already bound to an attempt; it is not run again (its outcome is unknown)", PRIOR_OUTCOME_UNKNOWN)
    if status == 409:
        reason = data.get("error") if isinstance(data, dict) else None
        claim = info.get("claim")
        if reason == "claim_lost" and isinstance(claim, dict):
            claim["lost"] = True
        raise StageRefused("approval", f"the control plane refused to bind this request ({reason or 'conflict'}); it is not run (its outcome is unknown)", PRIOR_OUTCOME_UNKNOWN)
    raise StageRefused("approval", "the control plane could not bind this approved request, so it is not run", UNAVAILABLE)


# --- replay resolution (RFC 0a.2 (1)) -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Replay:
    """What the plane's record of a ``request_id`` means for a call that wants to run again.

    ``action``: ``first_run`` (admit normally), ``pending`` (raise the interrupt again), ``resume`` (``resume_admit`` -> bind -> body), ``no_dispatch`` (the recorded outcome
    stands: ``reason`` is ``prior_outcome_unknown`` or the terminal status), ``refuse`` (``reason`` is the status or ``replay_unavailable``; ``detail`` names ``key_mismatch`` and the like).
    Nothing but ``first_run`` and ``resume`` may run the body.
    """

    action: str
    reason: "str | None" = None
    detail: "str | None" = None
    approval: "dict | None" = None


REPLAY_LOOKUP_TIMEOUT_S = 1.0
_REFUSED_STATUSES = frozenset({"denied", "expired", "withdrawn", "approved_unconsumed"})
_TERMINAL_STATUSES = frozenset({"executed", "failed", "superseded", "cancelled"})


def resolve_replay(shared: Any, request_id: str, resume_context: Any = None, *, now: "float | None" = None) -> Replay:
    """The lookup table of RFC 0a.2 (1): ONE authenticated ``GET /v1/approvals/by-request/{request_id}`` decides, whatever the call's current verdict would be.

    Only a ``200 {"exists": false}`` (the plane's single absence answer) with no approval in ``resume_context`` is a ``first_run``. Everything else maps to the table; an auth failure,
    a generic 404 (an old plane, a proxy, a missing route), a 5xx, a timeout, a row that is not bind-required, or any shape this does not know is ``refuse``/``replay_unavailable``:
    none of them is absence, so none may enter first admission whatever the tool's fail mode. ``resume_context`` is whatever the runtime handed back (a dict with an
    ``approval_id`` / ``request_id``, or a non-empty value): if it says an approval already existed, ``exists: false`` is a contradiction and refuses.
    """
    unavailable = Replay("refuse", "replay_unavailable")
    try:
        status, data = shared.approvals("GET", f"/v1/approvals/by-request/{request_id}", None, timeout=REPLAY_LOOKUP_TIMEOUT_S)
    except Exception:
        return unavailable
    if status == 403 and isinstance(data, dict) and data.get("error") == "not_requester":
        return Replay("refuse", "replay_unavailable", "key_mismatch")
    if status != 200 or not isinstance(data, dict):
        return unavailable
    exists = data.get("exists")
    if exists is False:
        said_existed = bool(resume_context.get("approval_id") or resume_context.get("request_id")) if isinstance(resume_context, dict) else bool(resume_context)
        if said_existed:
            return Replay("refuse", "replay_unavailable", "absent_but_expected")
        return Replay("first_run")
    approval = data.get("approval")
    if exists is not True or not isinstance(approval, dict) or not isinstance(approval.get("status"), str):
        return unavailable
    state = approval["status"]
    if approval.get("bind_required") is not True:
        return Replay("refuse", "replay_unavailable", "not_bind_required", approval)  # a request filed without the binding cannot be resumed safely
    if state == "pending":
        return Replay("pending", approval=approval)
    if state == "executing" or approval.get("bound_attempt_id"):
        return Replay("no_dispatch", PRIOR_OUTCOME_UNKNOWN, state, approval)
    if state in _TERMINAL_STATUSES:
        return Replay("no_dispatch", state, state, approval)
    if state in _REFUSED_STATUSES:
        return Replay("refuse", state, state, approval)
    if state == "approved":
        resume_by = _parse_time(approval.get("resume_by"))
        clock = time.time() if now is None else now
        if resume_by is not None and clock >= resume_by:  # strict: the window ends AT resume_by
            return Replay("refuse", "approved_unconsumed", "resume_by_passed", approval)
        return Replay("resume", approval=approval)
    return unavailable


def _parse_time(value: Any) -> "float | None":
    """An ISO 8601 UTC instant (as the plane serves it) as epoch seconds, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --- withdrawal (a pending request only) ------------------------------------------------------------------

_WITHDRAWN: set = set()  # request ids withdrawn here: a call holding one never dispatches, whatever the plane said
#: The ACTUAL answer of each withdrawal, newest last: ``(request_id, status, plane_status)``. ``withdrawn`` is only what the plane confirmed; ``not_withdrawable`` carries the true status
#: (an approval that won the race is NOT withdrawn); ``local`` means the plane could not be asked and this process alone will not dispatch it.
WITHDRAWALS: "collections.deque" = collections.deque(maxlen=256)


@dataclasses.dataclass(frozen=True)
class Withdrawal:
    """``status``: ``withdrawn`` (the plane did it, or had), ``not_withdrawable`` (``plane_status`` is the true status: it was decided, or already ran) or ``local`` (the plane
    could not be reached: the call is treated as withdrawn HERE and is never dispatched by this process)."""

    status: str
    plane_status: "str | None" = None


def is_withdrawn(request_id: Any) -> bool:
    return request_id in _WITHDRAWN


def withdraw(shared: Any, request_id: str, reason: "str | None" = None, approval_id: "str | None" = None) -> Withdrawal:
    """:func:`_withdraw` and the record of what the plane actually answered (:data:`WITHDRAWALS`)."""
    got = _withdraw(shared, request_id, reason, approval_id)
    WITHDRAWALS.append((request_id, got.status, got.plane_status))
    return got


def _withdraw(shared: Any, request_id: str, reason: "str | None" = None, approval_id: "str | None" = None) -> Withdrawal:
    """Withdraw a PENDING approval request the caller filed (``POST /v1/approvals/{id}/withdraw``): for the MCP boundary and any HTTP caller whose agent gave up waiting.

    The approval id is the one given, the one a parked deferred request holds, or the one the by-request lookup names. 200 -> ``withdrawn`` (idempotent for the same key); 409
    ``not_withdrawable`` -> that, with the true status. A transport failure, or a request whose id cannot be found, leaves the call withdrawn LOCALLY: it is recorded, a parked
    deferred request is forgotten, and this process never dispatches it. Never raises.
    """
    with _LOCK:
        parked = _PENDING.get(request_id)
        approval_id = approval_id or (parked.approval_id if parked is not None else None)
    try:
        if approval_id is None:
            status, data = shared.approvals("GET", f"/v1/approvals/by-request/{request_id}", None, timeout=REPLAY_LOOKUP_TIMEOUT_S)
            row = data.get("approval") if status == 200 and isinstance(data, dict) else None
            approval_id = row.get("id") if isinstance(row, dict) else None
        if approval_id is None:
            return _withdrawn_here(request_id)
        status, data = shared.approvals("POST", f"/v1/approvals/{approval_id}/withdraw", {"reason": reason} if reason else {}, timeout=BIND_TIMEOUT_S)
    except Exception:
        return _withdrawn_here(request_id)
    if status == 200:
        _withdrawn_here(request_id)
        return Withdrawal("withdrawn")
    if status == 409:
        true_status = data.get("status") if isinstance(data, dict) else None
        if true_status == "withdrawn":
            _withdrawn_here(request_id)
            return Withdrawal("withdrawn")
        return Withdrawal("not_withdrawable", true_status if isinstance(true_status, str) else None)
    return _withdrawn_here(request_id)


def _withdrawn_here(request_id: str) -> Withdrawal:
    with _LOCK:
        _WITHDRAWN.add(request_id)
        _PENDING.pop(request_id, None)
    return Withdrawal("local")
