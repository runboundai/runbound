"""A LangChain / LangGraph callback handler.

For agents whose tool calls are dispatched by a framework rather than your own
code, so there is no function to decorate with :func:`runbound.tool`::

    import runbound
    from runbound.integrations.langchain import RunboundCallbackHandler

    runbound.init(tool_policy=runbound.ToolPolicy(...))

    agent.invoke(
        {"input": "research the market"},
        config={"callbacks": [RunboundCallbackHandler()]},
    )

The handler sets ``raise_error = True`` so that LangChain lets
:class:`~runbound.exceptions.GuardrailTripped` out of the callback and the
chain actually stops. That setting cuts both ways — LangChain would propagate
*any* exception from a handler method — so every method below catches and logs
everything else itself. A bug in runbound can cost you observability; it can
never take down the chain.

``langchain_core`` is imported lazily, on first access to
``RunboundCallbackHandler``, so this module imports cleanly without it.
"""

import functools
import logging
import time
import warnings
from collections.abc import Callable
from typing import Any

from .. import _coverage, api
from ..exceptions import GuardrailTripped

_LOG = logging.getLogger("runbound")

_INSTALL_HINT = "runbound's LangChain integration requires langchain-core: pip install langchain-core"
_UNKNOWN_TOOL = "<tool>"

#: Built on first access to ``RunboundCallbackHandler``; see ``__getattr__``.
_HANDLER_CLASS: type | None = None

#: The old name warns on its first use in a process, not on every access.
_ALIAS_WARNED = False


# --- reading what LangChain hands us ----------------------------------------


def _as_model(value: Any) -> str | None:
    """A model name from an untyped payload: anything but a non-empty str -> None."""
    return value if isinstance(value, str) and value else None


def _tool_name(serialized: Any) -> str:
    """The tool's name from LangChain's ``serialized`` mapping.

    Falls back to ``"<tool>"`` when the mapping is absent or nameless. Raises
    whatever ``serialized`` raises — the caller is fail-open and a lying
    mapping is a bug worth logging.
    """
    name = serialized.get("name") if hasattr(serialized, "get") else None
    return _as_model(name) or _UNKNOWN_TOOL


# --- the handler ------------------------------------------------------------


def _fail_open(method: Callable) -> Callable:
    """Let only :class:`GuardrailTripped` out of a handler method.

    Required because the handler sets ``raise_error = True``: LangChain
    re-raises whatever a callback raises, so anything unintended escaping here
    would break the user's chain.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        try:
            method(self, *args, **kwargs)
        except GuardrailTripped:
            raise
        except Exception:
            _LOG.warning(
                "runbound's LangChain handler failed in %s; continuing",
                method.__name__,
                exc_info=True,
            )

    return wrapper


def _build_handler_class() -> type:
    """Define the handler against a lazily imported ``langchain_core``."""
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except Exception as exc:
        raise ImportError(_INSTALL_HINT) from exc

    class RunboundCallbackHandler(BaseCallbackHandler):  # type: ignore[misc, valid-type]
        """Feeds a LangChain run's tool calls to runbound.

        Pass one per ``invoke``/``stream`` call (or to the constructor of your
        chain) via ``config={"callbacks": [RunboundCallbackHandler()]}``. It
        holds no state of its own; the session lives in :mod:`runbound.api`,
        so several handlers share one step count and one tally per tool.

        Does nothing at all until :func:`runbound.init` has been called.
        """

        #: Let GuardrailTripped out of the callback so the chain stops.
        raise_error = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            #: run_id -> (tool name, monotonic start), for the ``tool_result`` event's duration.
            self._started: dict = {}

        @_fail_open
        def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:
            """Record and vet a tool call *before* the tool runs.

            LangChain calls this ahead of execution, so a refused call never
            runs. The action policy is enforced here, on the tool's name and the input string
            LangChain hands us as its single argument; a
            :class:`~runbound.exceptions.PolicyViolation` is a
            ``GuardrailTripped``, so ``raise_error`` lets it stop the chain.
            ``max_actions_per_run`` is enforced here too — the tool's event
            already counts towards ``executed_actions`` regardless of framework
            origin, so the cap applies to it the same way.

            This is also where a LangChain agent's tools become known: the
            framework owns the dispatch, so there is no ``@runbound.tool`` to
            declare them and the callback is the only place the name is ever
            said. They join the tool report undecorated, which is honest —
            nothing here guards them by name. ``decorated=False`` on the
            admit call below is exactly that: a LangChain tool declares no
            effects, so capability class rules never refuse it here — only the
            halt, the action cap and the policy do.
            """
            name = _tool_name(serialized)
            _coverage.tool_requested(name)
            api._observe(
                kind="tool_call",
                tool_name=name,
                args_hash=api._args_hash(name, (input_str,), {}),
            )
            started = api._start_run(name, call_id=str(kwargs.get("run_id")) if kwargs.get("run_id") else None)
            try:
                api._admit_action(name, (input_str,), {}, frozenset(), False, allow_rewrite=False)
            except GuardrailTripped as refusal:
                api._emit_result(name, "refused", started, [], exc=refusal)
                raise
            self._started[kwargs.get("run_id")] = (name, started)

        @_fail_open
        def on_tool_end(self, output: Any, **kwargs: Any) -> None:
            """Record how the tool ended: duration, size, and what the scan found.

            LangChain callbacks observe; they cannot replace a tool's output, so the
            result reaches the model as the tool returned it. The ``tool_result``
            event still carries the scan's flags, the truncation that *would* apply
            and the duration, never the output itself. Decorate the tool with
            ``@runbound.tool`` to have its output redacted and truncated for real.
            """
            name, started = self._started.pop(kwargs.get("run_id"), (_UNKNOWN_TOOL, time.monotonic()))
            api._finish(name, output, (), {}, started, [], frozenset(), (), None)

        @_fail_open
        def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
            """Record a failed tool call. The tool's own error stays the one raised."""
            name = kwargs.get("name") or _tool_name(kwargs.get("serialized"))
            api._record_tool_error(_as_model(name) or _UNKNOWN_TOOL, error)
            _name, started = self._started.pop(kwargs.get("run_id"), (None, time.monotonic()))
            api._emit_result(_as_model(name) or _UNKNOWN_TOOL, "error", started, [], exc=error)

        @_fail_open
        def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
            """Observed only. A chain error is usually a tool error already recorded."""
            _LOG.debug("runbound saw a LangChain chain error: %r", error)

    return RunboundCallbackHandler


def __getattr__(name: str) -> Any:
    """Build ``RunboundCallbackHandler`` on first access (PEP 562).

    Deferring it is what keeps ``langchain_core`` optional; the class is cached
    after the first access, and the ImportError is raised fresh every time so
    the install hint is never swallowed by a stale cache.

    ``GuardrailCallbackHandler`` is the deprecated former name: the same class,
    with one ``DeprecationWarning`` the first time it is used.
    """
    global _HANDLER_CLASS, _ALIAS_WARNED
    if name in ("RunboundCallbackHandler", "GuardrailCallbackHandler"):
        if name == "GuardrailCallbackHandler" and not _ALIAS_WARNED:
            _ALIAS_WARNED = True
            warnings.warn(
                "GuardrailCallbackHandler is renamed RunboundCallbackHandler; the old name will be removed in a future release",
                DeprecationWarning,
                stacklevel=2,
            )
        if _HANDLER_CLASS is None:
            _HANDLER_CLASS = _build_handler_class()
        return _HANDLER_CLASS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["RunboundCallbackHandler"]
