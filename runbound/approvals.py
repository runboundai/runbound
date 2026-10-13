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
import contextvars
import dataclasses
import enum
import hashlib
import hmac
import inspect
import json
import logging
import os
import threading
import time
import typing
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

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
        _INFLIGHT.clear()
        _EXECUTING.clear()
        _LOST.clear()
        _QUOTA_BLOCKED.clear()


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


def _json_hint(hint: Any) -> bool:
    """Can a value of this type hint survive a JSON round trip?"""
    if hint is Any or hint is inspect.Parameter.empty or hint is None or hint is type(None):
        return True
    if hint in (str, int, float, bool, list, dict):
        return True
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _json_hint(args[0])
    if origin is typing.Literal:
        return all(isinstance(a, (str, int, float, bool)) or a is None for a in args)
    if origin in (list, dict) or origin is typing.Union or type(hint).__name__ == "UnionType":
        return all(_json_hint(a) for a in args)
    if typing.is_typeddict(hint) or (dataclasses.is_dataclass(hint) and isinstance(hint, type)):
        return all(_json_hint(h) for h in typing.get_type_hints(hint).values())
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return all(isinstance(m.value, (str, int, float, bool)) for m in hint)
    if hasattr(hint, "__supertype__"):
        return _json_hint(hint.__supertype__)
    return False


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
            hints = typing.get_type_hints(inspect.unwrap(func), include_extras=True)
        except Exception:
            raise ValueError(
                f"tool {name!r} cannot be mode=\"deferred\": its type hints cannot be resolved, so its "
                "arguments cannot be shown to be JSON (import what they name, or use mode=\"sync\")"
            ) from None
        bad = [
            p.name for p in signature.parameters.values()
            if p.kind is p.VAR_POSITIONAL or not _json_hint(hints.get(p.name, inspect.Parameter.empty))
        ]
        if bad:
            raise ValueError(
                f"tool {name!r} cannot be mode=\"deferred\": its arguments are stored and replayed as JSON, "
                f"and {', '.join(bad)} cannot be (use mode=\"sync\", or JSON-friendly types)"
            )
    return ApprovalSpec(mode, float(timeout_s), tuple(signature.parameters))


_META: dict = {}  # tool -> (parameter names, are its arguments JSON-friendly), for approvals a row asks for
_SIGS: dict = {}  # tool -> its inspect.Signature, to bind a call's arguments by name for a rule's conditions


def json_friendly(func: Callable) -> bool:
    """Can this tool's arguments be stored and replayed as JSON (so a row may make it ``deferred``)?"""
    try:
        signature = inspect.signature(func)
        hints = typing.get_type_hints(inspect.unwrap(func), include_extras=True)
        return not any(
            p.kind is p.VAR_POSITIONAL or not _json_hint(hints.get(p.name, inspect.Parameter.empty))
            for p in signature.parameters.values()
        )
    except Exception:
        return False


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
    return {key: value for key, value in bound.arguments.items()
            if signature.parameters[key].kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)}


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
    return values


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
        return
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
    request_id = uuid.uuid4().hex
    body: dict = {
        "tool": call.name, "request_id": request_id, "args": args, "mode": spec.mode, "timeout_s": spec.timeout_s,
        "service": config.service, "worker_id": config.resolved_worker_id(),
        "run_id": getattr(session, "session_id", None),
    }
    if key_digest:
        body["key"] = key_digest  # per-key quotas
    requested_for = _REQUESTED_FOR.get()
    if requested_for:
        body["requested_for"] = requested_for
    body["args_digest"] = args_digest(call.name, args)  # the plane recomputes it: a different canonical form fails loudly (400), never silently asks again
    if spec.requirement:
        body["requirement"] = spec.requirement
    record = info.get("record") or {}
    for field in ("reason_code", "matched_rule_ids", "policy_version", "service_default_revision", "policy_state", "contributors", "sdk_version"):
        if record.get(field) is not None:
            body[field] = record[field]  # the decision record rides on the request, so "why is this waiting" reads by approval id
    secret = config.approval_secret
    if spec.mode == "deferred" and secret:
        body["seal"] = make_seal(secret, config.service, call.name, request_id, args, spec.timeout_s)
    deadline = time.monotonic() + spec.timeout_s
    approval = _file(shared, body, deadline, (config.service, call.name, key_digest))
    if approval.get("granted") is True:  # an active session/minutes grant admits this call: nobody is asked, nothing waits, the plane took no quota unit
        grant_id = str(approval["grant_id"])
        if isinstance(record.get("contributors"), list):
            record["contributors"] = [*record["contributors"], f"grant:{grant_id}"]
        info.update(request_id=request_id, granted=grant_id, quota=None)
        return
    # ``approval["quota"]`` non-null means the plane already took this call's quota unit: never take another.
    info.update(request_id=request_id, approval_id=approval.get("id"), quota=approval.get("quota"))
    if spec.mode == "deferred":
        with _LOCK:
            _PENDING[request_id] = Pending(call.name, request_id, args, approval.get("id"), body["run_id"])
            while len(_PENDING) > PENDING_MAX:
                _PENDING.popitem(last=False)
        raise Deferred({"status": PENDING_RESULT_STATUS, "request_id": request_id}, approval.get("id"))
    info["sync"] = True
    _wait(shared, approval, deadline)


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
            raise StageRefused("approval", f"the control plane refused the approval request (HTTP {status})", UNAVAILABLE)
        if time.monotonic() >= deadline:  # status None (transport) or a 5xx that never cleared
            raise StageRefused("approval", "the control plane could not be reached to ask for approval", TIMEOUT)
        _sleep_until(deadline, delay)
        delay = min(delay * 2, 5.0)


def _wait(shared: Any, approval: dict, deadline: float) -> None:
    """Long-poll until the request is decided. Never treats a transport error as a denial."""
    approval_id = approval["id"]
    delay = 0.25
    while True:
        status = approval.get("status")
        if status == "approved":
            return
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
    for attempt in range(attempts):
        status, _ = shared.approvals("POST", f"/v1/approvals/{record['approval_id']}/report", body, timeout=5.0)
        if status in _SETTLED:
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


def report_async(engine: Any, approval_id: str, outcome: str, result: Any = None) -> None:
    """Tell the plane how an approved call went (``executed`` | ``failed`` | ``tampered`` | ``clock_skew``).

    Tried a few times; if the plane cannot take it, queued for the next connected hello. Never raises."""

    def run() -> None:
        try:
            record = {"approval_id": approval_id, "outcome": outcome, "worker_id": engine.config.resolved_worker_id()}
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
        token = _CURRENT.set(request_id)
        with _LOCK:
            _EXECUTING.add(approval_id)
        try:
            with _run_as((own.run_id if own is not None else None) or item.get("run_id")):
                result = executor(**args)
            outcome = "executed"
        except BaseException as exc:  # the tool's own failure, or a refusal by the chain
            result, outcome = None, "failed"
            _call_back({"request_id": request_id, "tool": tool, "outcome": "failed", "error": type(exc).__name__})
        finally:
            _CURRENT.reset(token)
            with _LOCK:
                _EXECUTING.discard(approval_id)
        if outcome == "executed":
            report_async(engine, approval_id, "executed", result)
            _call_back({"request_id": request_id, "tool": tool, "outcome": "executed", "result": result})
        else:
            report_async(engine, approval_id, "failed")
        with _LOCK:
            _PENDING.pop(request_id, None)
    except Exception:
        _LOG.warning("runbound: could not execute approved request %s", approval_id, exc_info=True)
    finally:
        with _LOCK:
            _INFLIGHT.discard(approval_id)


def run_executor(func: Callable) -> Callable:
    """A callable that runs ``func(**args)``; an async tool is awaited on its own loop."""
    if inspect.iscoroutinefunction(func):
        return lambda **kw: asyncio.run(func(**kw))
    return lambda **kw: func(**kw)
