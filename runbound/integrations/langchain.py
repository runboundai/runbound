"""A LangChain / LangGraph callback handler.

For agents whose LLM and tool calls are made by a framework rather than by
your own code, so there is no client to :func:`runbound.wrap` and no function
to decorate with :func:`runbound.tool`::

    import runbound
    from runbound.integrations.langchain import GuardrailCallbackHandler

    runbound.init(budget_usd=5.00, loop_threshold=3, on_anomaly="raise")

    agent.invoke(
        {"input": "research the market"},
        config={"callbacks": [GuardrailCallbackHandler()]},
    )

The handler sets ``raise_error = True`` so that LangChain lets
:class:`~runbound.exceptions.GuardrailTripped` out of the callback and the
chain actually stops. That setting cuts both ways — LangChain would propagate
*any* exception from a handler method — so every method below catches and logs
everything else itself. A bug in runbound can cost you observability; it can
never take down the chain.

``langchain_core`` is imported lazily, on first access to
``GuardrailCallbackHandler``, so this module imports cleanly without it.

Limitations: LangChain reports token usage on ``on_llm_end`` only, so streamed
responses whose provider omits usage are recorded as zero-token steps, and
budget checks land after a call rather than before it.
"""

import functools
import logging
from collections.abc import Callable
from typing import Any

from .. import _coverage, api
from ..exceptions import GuardrailTripped

_LOG = logging.getLogger("runbound")

_INSTALL_HINT = "runbound's LangChain integration requires langchain-core: pip install langchain-core"
_UNKNOWN_TOOL = "<tool>"

#: Built on first access to ``GuardrailCallbackHandler``; see ``__getattr__``.
_HANDLER_CLASS: type | None = None


# --- reading what LangChain hands us ----------------------------------------


def _as_int(value: Any) -> int:
    """A token count from an untyped payload: non-numbers and negatives -> 0."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


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


def _safe(read: Callable[[Any], Any], response: Any, default: Any) -> Any:
    """Run a usage reader over a duck-typed response; on any failure, ``default``."""
    try:
        return read(response)
    except Exception:
        _LOG.debug("runbound could not read usage via %s", read.__name__, exc_info=True)
        return default


def _usage_from_llm_output(response: Any) -> tuple[str | None, int, int]:
    """``(model, tokens_in, tokens_out)`` from ``LLMResult.llm_output``."""
    llm_output = getattr(response, "llm_output", None)
    if not hasattr(llm_output, "get"):
        return None, 0, 0
    model = _as_model(llm_output.get("model_name"))
    usage = llm_output.get("token_usage")
    if not hasattr(usage, "get"):
        return model, 0, 0
    return model, _as_int(usage.get("prompt_tokens")), _as_int(usage.get("completion_tokens"))


def _usage_from_generations(response: Any) -> tuple[int, int]:
    """``(tokens_in, tokens_out)`` from the first generation's ``usage_metadata``.

    Chat models report usage there rather than in ``llm_output``.
    """
    for batch in getattr(response, "generations", None) or ():
        for generation in batch or ():
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if hasattr(usage, "get"):
                return _as_int(usage.get("input_tokens")), _as_int(usage.get("output_tokens"))
    return 0, 0


def _extract_usage(response: Any) -> tuple[str | None, int, int]:
    """Best-effort ``(model, tokens_in, tokens_out)`` from an LLMResult.

    Every lookup is optional: a response that carries no usage — or that
    misbehaves entirely — yields zeros, so the call is still recorded as a
    step rather than being lost.
    """
    model, tokens_in, tokens_out = _safe(_usage_from_llm_output, response, (None, 0, 0))
    if not tokens_in and not tokens_out:
        tokens_in, tokens_out = _safe(_usage_from_generations, response, (0, 0))
    return model, tokens_in, tokens_out


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

    class GuardrailCallbackHandler(BaseCallbackHandler):  # type: ignore[misc, valid-type]
        """Feeds a LangChain run's tool and model calls to runbound.

        Pass one per ``invoke``/``stream`` call (or to the constructor of your
        chain) via ``config={"callbacks": [GuardrailCallbackHandler()]}``. It
        holds no state of its own; the session lives in :mod:`runbound.api`,
        so several handlers share one budget and one step count.

        Does nothing at all until :func:`runbound.init` has been called.
        """

        #: Let GuardrailTripped out of the callback so the chain stops.
        raise_error = True

        @_fail_open
        def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:
            """Record and vet a tool call *before* the tool runs.

            LangChain calls this ahead of execution, so with
            ``on_anomaly="raise"`` a loop is broken on the repeat that would
            have made it — the tool never runs. An action policy is enforced
            here for the same reason, on the tool's name and the input string
            LangChain hands us as its single argument; a
            :class:`~runbound.exceptions.PolicyViolation` is a
            ``GuardrailTripped``, so ``raise_error`` lets it stop the chain.

            This is also where a LangChain agent's tools become known: the
            framework owns the dispatch, so there is no ``@runbound.tool`` to
            declare them and the callback is the only place the name is ever
            said. They join the tool report undecorated, which is honest —
            nothing here guards them by name.
            """
            name = _tool_name(serialized)
            _coverage.tool_requested(name)
            api._observe(
                kind="tool_call",
                tool_name=name,
                args_hash=api._args_hash(name, (input_str,), {}),
            )
            api._enforce_policy(name, (input_str,), {})

        @_fail_open
        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            """Record a finished model call, priced from whatever usage it carries."""
            api._record_llm_call(*_extract_usage(response))

        @_fail_open
        def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
            """Record a failed tool call. The tool's own error stays the one raised."""
            name = kwargs.get("name") or _tool_name(kwargs.get("serialized"))
            api._record_tool_error(_as_model(name) or _UNKNOWN_TOOL, error)

        @_fail_open
        def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
            """Observed only. Model errors are the framework's to retry or raise."""
            _LOG.debug("runbound saw a LangChain LLM error: %r", error)

        @_fail_open
        def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
            """Observed only. A chain error is usually a tool error already recorded."""
            _LOG.debug("runbound saw a LangChain chain error: %r", error)

    return GuardrailCallbackHandler


def __getattr__(name: str) -> Any:
    """Build ``GuardrailCallbackHandler`` on first access (PEP 562).

    Deferring it is what keeps ``langchain_core`` optional; the class is cached
    after the first access, and the ImportError is raised fresh every time so
    the install hint is never swallowed by a stale cache.
    """
    if name == "GuardrailCallbackHandler":
        global _HANDLER_CLASS
        if _HANDLER_CLASS is None:
            _HANDLER_CLASS = _build_handler_class()
        return _HANDLER_CLASS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["GuardrailCallbackHandler"]
