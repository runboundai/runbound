"""LangChain / LangGraph tool-call governance, EARLY TRANCHE (AAI-04, LC-04-1). INTERNAL, DISABLED BY DEFAULT, not a supported or public adapter.

    from runbound.integrations import langchain_early
    langchain_early.enable()                      # or RUNBOUND_LANGCHAIN_EARLY=1; until then the two surfaces below refuse to construct
    agent = create_agent(model, tools, middleware=[langchain_early.RunboundMiddleware(effects={"lookup_order": {"read"}})])
    node = langchain_early.govern_tools(tools, effects={"lookup_order": {"read"}})   # a bare ToolNode

What it does: every tool call that reaches ``wrap_tool_call`` is admitted ONCE through the SDK's own engine (``api._begin``: the same evaluator, record and span as a
``@runbound.tool``), as a FRESH call. It is typed (the tool's ``tool_call_schema`` becomes the signature the argument checks and the rule conditions see), its effects are
what the customer states per tool name or ``unclassified`` (never inferred from a name), and its ids come from the runtime (``thread_id`` and ``tool_call_id``) when both exist.

What it does not do (held: LC-EARLY-3): a call that would need a person's approval is refused ``approval_unavailable`` ("unsupported") BEFORE anything is asked and never runs;
there is no approval wait, resume, replay, bind or cancellation claim; the result is observed, not replaced; the old ``RunboundCallbackHandler`` is untouched. Deriving ids from ``tool_call_id`` does NOT make a call replay-safe.

Duplicate install (04-2): the INNERMOST boundary governs and this layer passes the call through, recording ``duplicate_install`` once per (tool, kind), when it can VERIFY that
the call is governed again: the tool's function is a ``@runbound.tool`` wrapper (kind ``decorator``), a ``RunboundCallbackHandler`` is attached to the call or to the tool (kind
``callback_handler``: the handler is the inner boundary, and its checks are the handler's, which are weaker; migrate by removing it), or another layer of this module already holds
the same tool call (kind ``new_path``). A same-named tool, or a governed call made from inside a tool's body (a different boundary), is not a duplicate. See :func:`duplicate_installs`.

A call whose arguments fail the tool's own schema is passed on unchanged: LangChain refuses it before the tool body runs, so it never becomes an action.
"""

from __future__ import annotations

import contextvars
import functools
import importlib.metadata
import inspect
import logging
import os
import sys
import threading
from typing import Any, Callable, Iterable, Mapping

try:
    from langchain.agents.middleware import AgentMiddleware
    from langgraph.prebuilt import ToolNode
except ImportError as missing:  # pragma: no cover - exercised only without the extras
    raise ImportError(f"runbound.integrations.langchain_early needs langchain>=1.0 and langgraph>=1.2 ({missing.name} is missing)") from missing

from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

from .. import _coverage, api, hooks as hooks_module, ids as ids_module
from ..approvals import key_hash
from ..exceptions import ExecutionRefused
from ..policy import ToolRules, normalize_effects

__all__ = ["LangChainEarlyDisabled", "RunboundMiddleware", "duplicate_installs", "enable", "framework_versions", "govern_tools"]

_LOG = logging.getLogger("runbound")

_ENV = "RUNBOUND_LANGCHAIN_EARLY"
_ENV_WAIT = "RUNBOUND_LANGCHAIN_APPROVAL_WAIT"
_enabled = False
_approval_wait = False  # AAI-04-4a: a fresh call that needs a person WAITS for the decision (separate flag, off by default); off, it is refused approval_unavailable as in 04-1
_LOCK = threading.Lock()
_REGISTERED: dict = {}  # (tool name, schema fingerprint) -> (validator, effects)

_DUPLICATES: dict = {}  # (tool name, kind) -> times passed through; the first sighting of each key is the one record
_HELD: "contextvars.ContextVar[frozenset]" = contextvars.ContextVar("runbound_lc_held", default=frozenset())  # (tool name, tool_call_id) pairs this layer is governing right now
RESUME_GATE: Any = None  # AAI-04-4b: set by langchain_resume.enable(); consulted once per governed call, before the fresh-only path
_INTERRUPTS = (GraphBubbleUp,)  # control flow of the graph (interrupt, parent command), never a tool failure

_JSON_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}


class LangChainEarlyDisabled(RuntimeError):
    """The early LangChain path is internal and off until it is enabled on purpose."""


def enable(*, approval_wait: bool = False) -> None:
    """Turn the early path on for this process (the environment variable ``RUNBOUND_LANGCHAIN_EARLY=1`` does the same).

    ``approval_wait=True`` (or ``RUNBOUND_LANGCHAIN_APPROVAL_WAIT=1``) lets a fresh call that needs a person WAIT for the decision, with the local cancellation fence (AAI-04-4a)."""
    global _enabled, _approval_wait
    _enabled = True
    _approval_wait = _approval_wait or approval_wait


def _waits() -> bool:
    return _approval_wait or os.environ.get(_ENV_WAIT, "").strip().lower() in ("1", "true", "yes")


def _check_enabled() -> None:
    if not (_enabled or os.environ.get(_ENV, "").strip().lower() in ("1", "true", "yes")):
        raise LangChainEarlyDisabled(f"the early LangChain path is internal and disabled by default: call langchain_early.enable() or set {_ENV}=1")


def framework_versions() -> dict:
    """The exact versions this process runs on, for the support matrix (a row is only as good as the versions its test recorded)."""
    out = {"python": ".".join(str(n) for n in sys.version_info[:3])}
    for dist in ("langchain", "langchain-core", "langgraph", "langgraph-prebuilt", "langgraph-checkpoint-sqlite", "pydantic"):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = None
    return out


# --- typed registration -----------------------------------------------------------------------------------------------------------------------------------


def _schema(tool: Any) -> Any:
    """What the model sees of the tool: ``tool_call_schema`` (a pydantic class or a JSON-schema dict; injected arguments are not in it)."""
    return getattr(tool, "tool_call_schema", None) or getattr(tool, "args_schema", None)


def _signature(schema: Any) -> "tuple[inspect.Signature, dict, str] | None":
    """The signature, annotations and a fingerprint a schema gives the argument checks; ``None`` when there is no schema (the arguments are then checked by name only)."""
    params: list = []
    notes: dict = {}
    if isinstance(schema, type) and hasattr(schema, "model_fields"):
        for name, field in schema.model_fields.items():
            default = inspect.Parameter.empty if field.is_required() else field.default
            params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=field.annotation))
            notes[name] = field.annotation
        fingerprint = repr(sorted((n, repr(f.annotation), f.is_required()) for n, f in schema.model_fields.items()))
    elif isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        required = set(schema.get("required") or ())
        for name, spec in schema["properties"].items():
            kind = _JSON_TYPES.get(spec.get("type")) if isinstance(spec, dict) else None
            params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=inspect.Parameter.empty if name in required else None, annotation=kind or inspect.Parameter.empty))
            if kind is not None:
                notes[name] = kind
        fingerprint = repr(sorted((p.name, repr(p.annotation), p.default is inspect.Parameter.empty) for p in params))
    else:
        return None
    return inspect.Signature(params), notes, fingerprint


def _register(name: str, tool: Any, effects: Mapping) -> "tuple[hooks_module.Validator, frozenset]":
    """The tool's validator and effects, built and reported once per (name, schema): the hello lists it with its signature and the rules the customer stated."""
    shape = _signature(_schema(tool))
    stated = normalize_effects(effects.get(name)) if effects.get(name) else frozenset()
    key = (name, None if shape is None else shape[2], stated)
    with _LOCK:
        known = _REGISTERED.get(key)
    if known is not None:
        return known
    def trampoline(**kwargs: Any) -> None:  # never called: it only carries the signature the argument checks read
        raise AssertionError("a registration stand-in is never called")
    if shape is not None:
        trampoline.__signature__ = shape[0]
        trampoline.__annotations__ = dict(shape[1])
    trampoline.__name__ = name
    validator = hooks_module.Validator(trampoline, name)
    rules = ToolRules(effects=stated)
    api._EFFECT_SOURCE[name] = "decorator" if stated else "undeclared"  # the customer's stated effects ride the decorator's source label; nothing else is "declared"
    _coverage.tool_requested(name)
    _coverage.tool_declared(name, trampoline, rules)
    with _LOCK:
        return _REGISTERED.setdefault(key, (validator, stated))


def _arguments(tool: Any, raw: Any) -> "dict | None":
    """The call's arguments as the tool's own schema reads them (coerced to the declared types, only what the model sent), or ``None`` when the schema refuses them.

    ``None`` means LangChain will refuse this call before the tool body runs, so it is not an action and is passed on untouched.
    """
    args = dict(raw or {})
    schema = _schema(tool)
    if isinstance(schema, type) and hasattr(schema, "model_validate"):
        try:
            checked = schema.model_validate(args)
        except Exception:
            return None
        return {name: getattr(checked, name) for name in checked.model_fields_set if name in schema.model_fields}
    return args


def _ids(request: Any) -> "dict | None":
    """The runtime's ids for this call, or ``None`` (random ids) when it has no ``thread_id`` or no ``tool_call_id``."""
    try:
        engine, state = api._active()
        config = getattr(request.runtime, "config", None) or {}
        configurable = config.get("configurable") or {}
        tenant = key_hash(state.key) if getattr(state, "key", None) else getattr(engine.config, "service", None)
        if _repeated(request):
            return None  # two calls of one step share this id: a derived action_id would merge them, so both get random ids (the documented exclusion)
        return ids_module.derive(str(tenant or ""), configurable.get("thread_id"), configurable.get("checkpoint_ns"), (request.tool_call or {}).get("id"))
    except Exception:
        return None


def _repeated(request: Any) -> bool:
    """Whether the step this call belongs to carries its ``tool_call_id`` more than once (read from the last AI message of the state)."""
    wanted = (request.tool_call or {}).get("id")
    state = request.state
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    for message in reversed(messages or []):
        calls = getattr(message, "tool_calls", None)
        if calls:
            return sum(1 for c in calls if c.get("id") == wanted) > 1
    return False


def _text(result: Any) -> str:
    content = getattr(result, "content", result)
    return content if isinstance(content, str) else repr(content)


def duplicate_installs() -> dict:
    """``{(tool, kind): count}`` of the calls this layer passed through because a verified inner boundary governs them (kinds: decorator, callback_handler, new_path)."""
    with _LOCK:
        return dict(_DUPLICATES)


def _wrapped_functions(tool: Any) -> list:
    out = []
    for attr in ("func", "coroutine"):
        fn = getattr(tool, attr, None)
        while isinstance(fn, functools.partial):
            fn = fn.func
        if fn is not None:
            out.append(fn)
    return out


def _handlers(request: Any) -> list:
    found: list = []
    sources = [getattr(request.tool, "callbacks", None), (getattr(request.runtime, "config", None) or {}).get("callbacks")]
    for source in sources:
        if source is None:
            continue
        found.extend(getattr(source, "handlers", None) or getattr(source, "inheritable_handlers", None) or (source if isinstance(source, (list, tuple)) else []))
    return found


def _duplicate(request: Any) -> "str | None":
    """The kind of verified duplicate instrumentation this call is, or ``None``. Never by name alone: only the function, the attached handler or the held call itself."""
    from .langchain import RunboundCallbackHandler  # a lazy import: that module needs only langchain-core

    tool = request.tool
    if any(getattr(fn, "__runbound_tool__", None) for fn in _wrapped_functions(tool)):
        return "decorator"
    if any(isinstance(h, RunboundCallbackHandler) for h in _handlers(request)):
        return "callback_handler"
    if (tool.name, (request.tool_call or {}).get("id")) in _HELD.get():
        return "new_path"
    return None


def _note_duplicate(name: str, kind: str) -> None:
    with _LOCK:
        first = (name, kind) not in _DUPLICATES
        _DUPLICATES[(name, kind)] = _DUPLICATES.get((name, kind), 0) + 1
    if first:
        _LOG.warning("runbound: tool %r is governed twice (%s); the innermost boundary governs and the LangChain middleware passes the call through", name, kind)


class _Governed:
    """The one place a tool call is turned into a governed call; both surfaces use it."""

    def __init__(self, effects: "Mapping[str, Iterable[str]] | None") -> None:
        self.effects = dict(effects or {})

    def _plan(self, request: Any) -> "tuple | None":
        """``None`` (nothing to govern here), ``(name, kind)`` (a verified duplicate: pass through) or ``(name, None, kwargs, validator, effects)``."""
        engine, state = api._active()
        tool = request.tool
        if engine is None or state is None or tool is None:
            return None  # Runbound is not initialised, or the tool is not registered with the node: nothing to govern here (LangChain refuses an unknown tool itself)
        kind = _duplicate(request)  # BEFORE anything is registered: the inner boundary's own declaration of the tool must stand
        if kind is not None:
            return tool.name, kind
        kwargs = _arguments(tool, (request.tool_call or {}).get("args"))
        if kwargs is None:
            return None
        validator, effects = _register(tool.name, tool, self.effects)
        return tool.name, None, kwargs, validator, effects

    def run(self, request: Any, handler: Callable) -> Any:
        plan = self._plan(request)
        if plan is None:
            return handler(request)
        if plan[1] is not None:
            _note_duplicate(plan[0], plan[1])
            return handler(request)
        name, _, kwargs, validator, effects = plan
        if RESUME_GATE is not None:
            handled = RESUME_GATE.run(request, handler, name, kwargs, validator, effects, _ids(request))
            if handled is not _NOT_HANDLED():
                return handled
        token = _HELD.set(_HELD.get() | {(name, (request.tool_call or {}).get("id"))})
        try:
            return api._govern_external(name, kwargs, lambda final: handler(_with_args(request, final)), effects=effects, validator=validator, ids=_ids(request), text=_text, passthrough=_INTERRUPTS, waits=_waits())
        except ExecutionRefused as refused:
            return _refusal(request, refused)
        finally:
            _HELD.reset(token)

    async def arun(self, request: Any, handler: Callable) -> Any:
        plan = self._plan(request)
        if plan is None:
            return await handler(request)
        if plan[1] is not None:
            _note_duplicate(plan[0], plan[1])
            return await handler(request)
        name, _, kwargs, validator, effects = plan
        if RESUME_GATE is not None:
            handled = await RESUME_GATE.arun(request, handler, name, kwargs, validator, effects, _ids(request))
            if handled is not _NOT_HANDLED():
                return handled
        token = _HELD.set(_HELD.get() | {(name, (request.tool_call or {}).get("id"))})

        async def body(final: dict) -> Any:
            return await handler(_with_args(request, final))

        try:
            return await api._agovern_external(name, kwargs, body, effects=effects, validator=validator, ids=_ids(request), text=_text, passthrough=_INTERRUPTS, waits=_waits())
        except ExecutionRefused as refused:
            return _refusal(request, refused)
        finally:
            _HELD.reset(token)


def _NOT_HANDLED() -> Any:
    from .langchain_resume import NOT_HANDLED

    return NOT_HANDLED


def _refusal(request: Any, refused: ExecutionRefused) -> ToolMessage:
    """What the graph gets for a refused call (RFC section 9): a tool message naming the reason code, never the arguments; the tool did not run and the graph carries on."""
    call = request.tool_call or {}
    return ToolMessage(content=f"refused {refused.reason}", name=call.get("name"), tool_call_id=call.get("id") or "", status="error")


def _with_args(request: Any, final: dict) -> Any:
    """The request carrying the arguments the chain finally allowed (a pre-hook may have rewritten them); the original when nothing changed."""
    original = (request.tool_call or {}).get("args") or {}
    if all(original.get(k) == v for k, v in final.items()) and set(final) <= set(original):
        return request
    return request.override(tool_call={**request.tool_call, "args": {**original, **final}})


# --- the two surfaces -------------------------------------------------------------------------------------------------------------------------------------


class RunboundMiddleware(AgentMiddleware):
    """For ``langchain.agents.create_agent``: ``wrap_tool_call`` / ``awrap_tool_call`` admit each call once, as a fresh call (see the module doc)."""

    def __init__(self, *, effects: "Mapping[str, Iterable[str]] | None" = None) -> None:
        _check_enabled()
        super().__init__()
        self._governed = _Governed(effects)

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        return self._governed.run(request, handler)

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        return await self._governed.arun(request, handler)


def govern_tools(tools: Iterable[Any], *, effects: "Mapping[str, Iterable[str]] | None" = None, **node_kwargs: Any) -> ToolNode:
    """A ``ToolNode`` over ``tools`` whose every call passes the same admission as :class:`RunboundMiddleware`."""
    _check_enabled()
    governed = _Governed(effects)
    return ToolNode(list(tools), wrap_tool_call=governed.run, awrap_tool_call=governed.arun, **node_kwargs)
