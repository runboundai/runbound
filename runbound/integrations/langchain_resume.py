"""LangChain / LangGraph SUSPENDED approval: interrupt, resume and replay (AAI-04-4b, RFC 0a.2 and section 9). INTERNAL, DISABLED BY DEFAULT, not a supported or public adapter.

    from runbound.integrations import langchain_early, langchain_resume
    langchain_early.enable(); langchain_resume.enable()        # or RUNBOUND_LANGCHAIN_RESUME=1; init(approval_bind=True) and a checkpointer are required

Plugs into ``langchain_early._Governed`` at ONE point (``RESUME_GATE``): for a call whose runtime names it stably (``thread_id`` and a non-empty, unrepeated ``tool_call_id``,
so ``replay_safe``), the gate runs INSTEAD of the fresh-only path. Any other call is not handled here (``NOT_HANDLED``) and stays exactly the early path.

The order for a replay-safe call (the plane's record is resolved BEFORE any admission, whatever the current verdict would be):

1. ``approvals.resolve_replay``: ONE authenticated ``GET /v1/approvals/by-request/{request_id}``. ``request_id = H(tenant, attempt_id)``.
2. ``exists: false`` and the trusted resume context does not say an approval existed -> a first run: the SDK engine admits it (evaluation #1). An allow runs (no replay safety is
   claimed for it). A deny is refused. An ask FILES a suspended, bind-required request (idempotent on ``request_id``) and the graph is interrupted OUTSIDE any tool body.
3. ``pending`` -> the same interrupt again; no admission, no evaluation, no new request.
4. ``approved`` / bind-required / not bound / before ``resume_by`` -> ``resume_admit`` -> bind -> restriction re-check -> LOCAL start marker -> body. The resume payload the
   application hands back is NEVER authority: only the plane's row decides, and the arguments digest is recomputed from the arguments the graph holds now.
5. ``executing`` / terminal -> no dispatch (``prior_outcome_unknown`` or the recorded status). ``denied`` / ``expired`` / ``withdrawn`` / ``approved_unconsumed`` -> refused with that reason.
6. Anything ambiguous (unreachable plane, auth failure, a generic 404, ``key_mismatch``, a resume context that carries an approval while the plane has none, a context this module
   cannot read) -> refused ``replay_unavailable``. None of these is a first run, whatever the tool's fail mode.

The resume context is read from LangGraph's own per-task scratchpad (pinned in the ``langchain-test`` extra): an unreadable scratchpad refuses, never guesses.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from .. import api, approvals, ids as ids_module
from ..exceptions import ExecutionRefused
from . import langchain_early as early

__all__ = ["NOT_HANDLED", "ResumeGate", "enable", "disable"]

_LOG = logging.getLogger("runbound")
_ENV = "RUNBOUND_LANGCHAIN_RESUME"
NOT_HANDLED = object()  # "this call is not mine": the early path carries on exactly as before


def enable() -> None:
    """Turn the resume gate on for this process (``RUNBOUND_LANGCHAIN_RESUME=1`` does the same). Needs ``langchain_early`` enabled."""
    early.RESUME_GATE = ResumeGate()


def disable() -> None:
    early.RESUME_GATE = None


def _env_gate() -> None:
    if early.RESUME_GATE is None and os.environ.get(_ENV, "").strip().lower() in ("1", "true", "yes"):
        enable()


class _ContextUnreadable(Exception):
    pass


def _resume_context() -> list:
    """The resume values LangGraph holds for THIS task: ``[]`` on a fresh run, a non-empty list when the task is being resumed. Reads the task scratchpad (a pinned dependency);
    raises if it cannot, so the caller refuses rather than guessing which of the two it is."""
    try:
        from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD
        from langgraph.config import get_config

        scratchpad = get_config()["configurable"][CONFIG_KEY_SCRATCHPAD]
        values = list(scratchpad.resume)
        pending = scratchpad.get_null_resume(False)
        if pending is not None:
            values.append(pending)
        return values
    except Exception as exc:
        raise _ContextUnreadable(str(exc)) from exc


def _interrupt(payload: dict) -> None:
    """Raise the graph interrupt, OUTSIDE any tool body. ``interrupt()`` returns a held resume value instead of raising, so every held value is consumed first (the
    application's answer is not authority; it only says it is back): the first call with none left raises ``GraphInterrupt``."""
    for _ in range(16):
        interrupt(payload)
    raise RuntimeError("langgraph kept resuming this interrupt")  # pragma: no cover - 16 held resume values for one task does not happen


def _refused(request: Any, reason: str, detail: "str | None" = None) -> ToolMessage:
    """What the graph gets for a call that was not dispatched (RFC section 9): the reason code, never the arguments."""
    call = request.tool_call or {}
    text = f"refused {reason}" + (f" ({detail})" if detail else "")
    return ToolMessage(content=text, name=call.get("name"), tool_call_id=call.get("id") or "", status="error")


class ResumeGate:
    """The one object ``langchain_early._Governed`` consults before its fresh-only path."""

    # --- shared ------------------------------------------------------------------------------------------------------------------------

    def _identity(self, ids: "dict | None") -> "tuple | None":
        engine, state = api._active()
        if engine is None or state is None or not ids or not ids.get("attempt_id"):
            return None
        tenant = approvals.key_hash(state.key) if getattr(state, "key", None) else getattr(engine.config, "service", None)
        return engine, state, ids_module.request_id_for(str(tenant or ""), ids["attempt_id"])

    @staticmethod
    def _payload(name: str, ids: dict, request_id: str, approval_id: "str | None", args_digest: "str | None") -> dict:
        return {"kind": "runbound.approval", "request_id": request_id, "approval_id": approval_id, "attempt_id": ids.get("attempt_id"), "tool": name, "args_digest": args_digest}

    def _lookup(self, engine: Any, request_id: str, context: list):
        return approvals.resolve_replay(engine.shared, request_id, True if context else None)

    def _refuse(self, request: Any, name: str, kwargs: dict, effects: Any, ids: dict, request_id: str, status: str, detail: "str | None", approval_id: "str | None") -> ToolMessage:
        """A refusal of the replay table is a decision: it is RECORDED (one refused span, the refusal event; nothing dispatched) and then handed to the graph as the reason code."""
        try:
            api._refuse_replayed(name, kwargs, effects, ids, request_id, status, detail, approval_id)
        except ExecutionRefused:
            pass  # recorded; the graph gets the specific reason below
        except Exception:
            _LOG.warning("runbound: could not record the refused replay of %r", name, exc_info=True)
        return _refused(request, status, detail)

    def _decide(self, request: Any, name: str, kwargs: dict, effects: Any, ids: dict, request_id: str, engine: Any) -> "tuple[str, Any]":
        """Resolve the plane's record: ``("run", replay)`` / ``("interrupt", payload)`` / ``("done", ToolMessage)``. Blocking."""
        try:
            context = _resume_context()
        except _ContextUnreadable:
            return "done", self._refuse(request, name, kwargs, effects, ids, request_id, "replay_unavailable", "resume_context_unreadable", None)
        replay = self._lookup(engine, request_id, context)
        action = replay.action
        if action == "first_run":
            return "first", replay
        if action == "pending":
            approval = replay.approval or {}
            return "interrupt", self._payload(name, ids, request_id, approval.get("id"), approval.get("args_digest"))
        if action == "resume":
            return "resume", replay
        return "done", self._refuse(request, name, kwargs, effects, ids, request_id, replay.reason or "replay_unavailable", replay.detail, (replay.approval or {}).get("id"))

    @staticmethod
    def _resumed(request_id: str, replay: Any) -> dict:
        approval = replay.approval or {}
        return {"request_id": request_id, "approval_id": approval.get("id"), "args_digest": approval.get("args_digest"), "quota_held": approval.get("quota_held")}

    # --- the call ----------------------------------------------------------------------------------------------------------------------

    def run(self, request: Any, handler: Callable, name: str, kwargs: dict, validator: Any, effects: Any, ids: "dict | None") -> Any:
        who = self._identity(ids)
        if who is None:
            return NOT_HANDLED  # not replay-safe (random ids): the early path, unchanged
        engine, _state, request_id = who
        verdict, value = self._decide(request, name, kwargs, effects, ids, request_id, engine)
        if verdict == "done":
            return value
        if verdict == "interrupt":
            _interrupt(value)
        token = early._HELD.set(early._HELD.get() | {(name, (request.tool_call or {}).get("id"))})
        try:
            run_body = lambda final: handler(early._with_args(request, final))  # noqa: E731
            if verdict == "resume":
                return api._govern_resumed(name, kwargs, run_body, effects=effects, validator=validator, ids=ids, text=early._text, passthrough=early._INTERRUPTS,
                                           resumed=self._resumed(request_id, value))
            try:
                return api._govern_suspendable(name, kwargs, run_body, effects=effects, validator=validator, ids=ids, text=early._text, passthrough=early._INTERRUPTS,
                                                suspend={"request_id": request_id})
            except approvals.Suspended as asked:
                payload = self._payload(name, ids, request_id, asked.approval_id, asked.args_digest)
        except ExecutionRefused as refused:
            return early._refusal(request, refused)
        finally:
            early._HELD.reset(token)
        _interrupt(payload)  # outside the tool body, the SDK call and the held-token scope

    async def arun(self, request: Any, handler: Callable, name: str, kwargs: dict, validator: Any, effects: Any, ids: "dict | None") -> Any:
        who = self._identity(ids)
        if who is None:
            return NOT_HANDLED
        engine, _state, request_id = who
        verdict, value = await asyncio.to_thread(self._decide, request, name, kwargs, effects, ids, request_id, engine)
        if verdict == "done":
            return value
        if verdict == "interrupt":
            _interrupt(value)
        token = early._HELD.set(early._HELD.get() | {(name, (request.tool_call or {}).get("id"))})

        async def run_body(final: dict) -> Any:
            return await handler(early._with_args(request, final))

        try:
            if verdict == "resume":
                return await api._agovern_resumed(name, kwargs, run_body, effects=effects, validator=validator, ids=ids, text=early._text, passthrough=early._INTERRUPTS,
                                                  resumed=self._resumed(request_id, value))
            try:
                return await api._agovern_suspendable(name, kwargs, run_body, effects=effects, validator=validator, ids=ids, text=early._text, passthrough=early._INTERRUPTS,
                                                       suspend={"request_id": request_id})
            except approvals.Suspended as asked:
                payload = self._payload(name, ids, request_id, asked.approval_id, asked.args_digest)
        except ExecutionRefused as refused:
            return early._refusal(request, refused)
        finally:
            early._HELD.reset(token)
        _interrupt(payload)


_env_gate()
