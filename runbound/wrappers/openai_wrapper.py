"""OpenAI-shaped clients: ``client.chat.completions.create`` and
``client.responses.create``.

Detection is by shape, never by ``isinstance``: the ``openai`` package is not
imported here (or anywhere in runbound), so any OpenAI-compatible client —
Azure OpenAI, Ollama, vLLM, Groq, Together, OpenRouter, LM Studio, a test fake
— is guarded by the same code path.

A client may expose either call surface or both, and every one it exposes gets
patched: guarding chat completions while the Responses API flows past uncounted
is worse than not guarding at all, because it reports success. Which surfaces
were patched is logged at INFO.

This module knows three things and nothing else: where the methods live, how
to read usage off a response, and how to read the tool calls the model asked
for in one. It does not price calls, decide policy or touch session state; it
hands ``(model, tokens_in, tokens_out, duration_s, tokens_reasoning,
tokens_cached_in)`` to the ``report`` callback the API layer passes in, and
each requested tool call to ``hooks.tool_request``.

Sync and async clients are both handled, streamed or not: :func:`install`
looks at each method it is replacing and at the request, and picks one of the
four paths. Streaming usage is best-effort — OpenAI sends it on a final chunk
only when the caller asked for ``stream_options={"include_usage": True}``, and
runbound never adds that option to a request it did not write.
"""

import functools
import logging
from typing import Any, Callable, NamedTuple

from . import (
    MARKER,
    ChunkRequests,
    ChunkText,
    ChunkUsage,
    ClassTarget,
    Hooks,
    Report,
    _NO_HOOKS,
    _StreamRequests,
    _StreamUsage,
    _call_report,
    _elapsed,
    _guard_stream,
    _is_async_callable,
    content_chars,
    emit_tool_requests,
    estimated_tokens,
    estimating,
    fixed_label,
    guarded_create,
    log_guarded_surfaces,
    messages_chars,
    normalize_arguments,
    provider_label,
    resource_label,
    warn_estimated_tokens,
)

_LOG = logging.getLogger("runbound")

#: ``MARKER`` is imported from the package rather than defined here: an
#: instance patch and a class patch have to recognize each other's work, so
#: there is exactly one name for "this create is already guarded".

#: This client's shape — the first half of the label every surface on it shares.
#: One endpoint is down or up, whichever of its APIs the agent is calling; the
#: second half is the host, so two OpenAI-compatible endpoints are two circuits.
PROVIDER = "openai"

#: Chat completions and the Responses API name the same counts differently.
_INPUT_FIELDS = ("prompt_tokens", "input_tokens")
_OUTPUT_FIELDS = ("completion_tokens", "output_tokens")
#: Reasoning tokens hang off ``usage.completion_tokens_details`` (chat) or
#: ``usage.output_tokens_details`` (responses).
_DETAILS_FIELDS = ("completion_tokens_details", "output_tokens_details")
_REASONING_FIELDS = ("reasoning_tokens",)
#: Cached-input tokens (T139) hang off the *input*-side details object:
#: ``usage.prompt_tokens_details`` (chat) or ``usage.input_tokens_details``
#: (responses) — the mirror of `_DETAILS_FIELDS`, which is the output side.
_CACHED_DETAILS_FIELDS = ("prompt_tokens_details", "input_tokens_details")
_CACHED_FIELDS = ("cached_tokens",)


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` off an attribute-style or mapping-style object.

    Returns ``None`` for anything that is missing or that raises on access —
    a client is someone else's object and may do anything on attribute access.
    """
    try:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)
    except Exception:
        return None


class _Surface(NamedTuple):
    """One patchable ``create``: what to call it, where it lives, how to read it.

    The two APIs answer in different shapes, so each carries its own readers:
    usage and model-requested tool calls, from a finished response and from a
    stream's chunks, plus the three text readers the opt-in token estimator
    needs when an endpoint reports no usage at all.
    """

    name: str
    path: tuple[str, ...]
    chunk_usage: ChunkUsage
    read_requests: Callable[[Any], list[tuple[str, str]]]
    chunk_requests: ChunkRequests
    request_chars: Callable[[dict], int]
    response_chars: Callable[[Any], int]
    chunk_text: ChunkText


def _resource(client: Any, path: tuple[str, ...]) -> Any:
    """Walk ``path`` down from ``client``, or None if any step is missing."""
    node = client
    for name in path:
        node = _field(node, name)
        if node is None:
            return None
    return node


def _present(client: Any) -> list[tuple[_Surface, Callable]]:
    """Every surface this client actually exposes, with its bound ``create``."""
    found = []
    for surface in SURFACES:
        method = _field(_resource(client, surface.path), "create")
        if callable(method):
            found.append((surface, method))
    return found


def matches(client: Any) -> bool:
    """True if ``client`` exposes any OpenAI call surface runbound guards."""
    return bool(_present(client))


def is_wrapped(client: Any) -> bool:
    """True if *every* surface this client exposes is already guarded.

    A client whose chat completions are guarded but whose ``responses.create``
    is not counts as unwrapped, so wrapping it again covers what is missing
    rather than reporting a client that is only half watched.
    """
    found = _present(client)
    return bool(found) and all(_field(method, MARKER) is True for _, method in found)


def install(client: Any, report: Report, hooks: Hooks = None) -> None:
    """Replace every present ``create`` with a reporting version, in place.

    An ``async def`` original gets an ``async def`` replacement, so the client
    keeps behaving exactly as its own users expect. Either way the original
    runs first and its response is returned untouched; reporting happens after,
    because usage only exists once the call returns. ``report`` may raise
    :class:`~runbound.exceptions.GuardrailTripped` — by then the host already
    has its response, and the *next* call is the one being prevented.

    Surfaces already carrying the marker are left alone, so installing twice
    cannot double-count. What was patched is logged at INFO; a surface that
    refuses to be patched is logged as unguarded and costs the others nothing.

    The call is timed from just before the original runs; a ``report`` that
    predates timing and takes only three arguments still works. A request that
    asked for a stream gets its iterator back wrapped in a guard: chunks pass
    through untouched and the call is reported, with the time the whole stream
    took, when it ends. See :class:`runbound.wrappers._GuardedStream`.

    ``hooks`` (optional, the api's) is what turns failures into consequences:
    ``before`` runs ahead of every request and may raise
    :class:`~runbound.exceptions.CircuitOpen` (or refuse a call that would
    exceed the in-flight cap), and a request that raises is handed to ``error``
    before its own exception is re-raised. Every tool call the model asks for
    in a response goes to ``tool_request``, which is how a model looping on one
    tool is caught even when the developer dispatches those calls by hand.
    Installed without hooks, this wrapper observes exactly what it always did.

    All of that is reported under this client's :func:`provider_label` —
    ``"openai@localhost:11434"``, ``"openai@api.openai.com"`` — worked out once
    here, so a self-hosted box and OpenAI proper never share a circuit or a
    concurrency budget just because they share a shape.
    """
    hooks = _NO_HOOKS if hooks is None else hooks
    label = provider_label(PROVIDER, client)
    guarded = []
    for surface, original in _present(client):
        if _field(original, MARKER) is True:
            continue
        try:
            installed = _install_for(surface, original, report, hooks, fixed_label(label))
            setattr(_resource(client, surface.path), "create", installed)
        except Exception:  # one stubborn resource does not unguard the others
            _LOG.warning(
                "runbound could not guard %s; it will run unguarded", surface.name, exc_info=True
            )
            continue
        guarded.append((surface.name, _is_async_callable(original)))
    log_guarded_surfaces(guarded)


def _install_for(
    surface: _Surface,
    original: Callable,
    report: Report,
    hooks: Hooks,
    label_for: Any,
) -> Callable:
    """One surface's guarded ``create``, however the endpoint is worked out.

    The whole difference between ``wrap(client)`` and
    :mod:`runbound.autowrap` lives in ``label_for``: a constant for a client
    whose endpoint is known at install time, a read off ``self._client`` for a
    patched class shared by every client in the process.
    """
    return guarded_create(
        original, report, hooks, label_for, functools.partial(_finish, surface=surface)
    )


def guard_class_create(
    original: Callable, report: Report, hooks: Hooks, *, surface: _Surface
) -> Callable:
    """Guard a ``create`` patched onto the SDK's own class, not onto a client.

    Used by :mod:`runbound.autowrap`. Every client in the process shares this
    one function, so the endpoint is read per call off the resource it was
    called on — see :func:`runbound.wrappers.resource_label`.
    """
    return _install_for(surface, original, report, hooks, resource_label(PROVIDER))


def _finish(
    response: Any,
    kwargs: dict,
    report: Report,
    started_at: float,
    hooks: Hooks = _NO_HOOKS,
    label: str = PROVIDER,
    *,
    surface: _Surface,
    is_async: bool,
) -> Any:
    """Report a finished call, or hand back the stream that will report itself.

    A streamed call has not succeeded yet — it has barely started — so the
    guard it is wrapped in carries the hooks and reports the outcome when the
    stream ends.

    A finished one is reported in a fixed order: usage, then
    ``hooks.success``, then the tool calls the model asked for. Success comes
    before the requests because it is simply true — the provider answered —
    and the requests come last because reporting them is what can raise: a
    model looping on the same tool trips the session here, with the response
    already in the caller's hands and its dispatch still ahead of them.
    """
    stream = _guard_stream(
        response,
        kwargs,
        report,
        surface.chunk_usage,
        is_async=is_async,
        started_at=started_at,
        hooks=hooks,
        provider=label,
        chunk_requests=surface.chunk_requests,
        chunk_text=surface.chunk_text,
        request_chars=surface.request_chars(kwargs) if estimating(hooks) else 0,
    )
    if stream is not None:
        return stream
    _report_call(report, response, kwargs, started_at, surface, hooks)
    hooks.success(label)
    emit_tool_requests(hooks, surface.read_requests(response))
    return response


def _report_call(
    report: Report,
    response: Any,
    request_kwargs: dict,
    started_at: float,
    surface: _Surface,
    hooks: Hooks,
) -> None:
    """Read a finished non-streamed call and hand it to ``report``."""
    model, tokens_in, tokens_out = read_usage(response, request_kwargs)
    if not (tokens_in or tokens_out) and estimating(hooks):
        tokens_in = estimated_tokens(surface.request_chars(request_kwargs))
        tokens_out = estimated_tokens(surface.response_chars(response))
        if tokens_in or tokens_out:
            warn_estimated_tokens(model)
    _call_report(
        report,
        model,
        tokens_in,
        tokens_out,
        _elapsed(started_at),
        read_reasoning(response),
        read_cached(response),
    )


def read_usage(response: Any, request_kwargs: dict) -> tuple[str | None, int, int]:
    """Extract ``(model, tokens_in, tokens_out)`` from a completion or response.

    Both APIs put the counts on ``response.usage`` and differ only in the field
    names — ``prompt_tokens``/``completion_tokens`` for chat completions,
    ``input_tokens``/``output_tokens`` for the Responses API — so one reader
    covers both. Missing, malformed or absent usage reads as zero tokens rather
    than an error: an unrecorded call is better than a broken one. The model
    falls back to the request's ``model`` argument when the response does not
    echo it.
    """
    usage = _field(response, "usage")
    model = _field(response, "model") or request_kwargs.get("model")
    return (
        model if isinstance(model, str) and model else None,
        _tokens(usage, _INPUT_FIELDS),
        _tokens(usage, _OUTPUT_FIELDS),
    )


def read_reasoning(response: Any) -> int:
    """Thinking tokens a reasoning model burned, or 0.

    A subset of ``tokens_out``, not an addition to it, under
    ``usage.completion_tokens_details.reasoning_tokens`` for chat completions
    and ``usage.output_tokens_details.reasoning_tokens`` for the Responses API.
    Models that do not reason omit the whole details object, and an SDK object
    that raises while being read counts as absent rather than as an error.
    """
    return _reasoning(_field(response, "usage"))


def _reasoning(usage: Any) -> int:
    """Thinking tokens on one usage object, 0 when absent or unreadable."""
    for name in _DETAILS_FIELDS:
        tokens = _tokens(_field(usage, name), _REASONING_FIELDS)
        if tokens:
            return tokens
    return 0


def read_cached(response: Any) -> int:
    """Cached prompt tokens billed at OpenAI's discounted rate (T139), or 0.

    A slice of ``tokens_in``, not additional tokens — ``prompt_tokens`` /
    ``input_tokens`` already include a cache hit, this just says how many of
    them were one. ``usage.prompt_tokens_details.cached_tokens`` for chat
    completions, ``usage.input_tokens_details.cached_tokens`` for the
    Responses API. Absent or unreadable reads as 0 ("no cached tokens seen"),
    the same fail-open reading every other usage field here gets.
    """
    return _cached(_field(response, "usage"))


def _cached(usage: Any) -> int:
    """Cached-input tokens on one usage object, 0 when absent or unreadable."""
    for name in _CACHED_DETAILS_FIELDS:
        tokens = _tokens(_field(usage, name), _CACHED_FIELDS)
        if tokens:
            return tokens
    return 0


def _responses_chunk_usage(chunk: Any, usage: _StreamUsage) -> None:
    """Read one Responses-API stream event into the running usage.

    Events wrap the response they belong to, and the terminal
    ``response.completed`` event is the one whose copy carries the call's
    totals — so both ``event.response.usage`` and a usage object attached
    directly to the event are read. Counts are totals rather than increments,
    so the largest seen wins and a later partial event cannot lower them. A
    stream that never says anything readable leaves the zeros it started with.
    """
    inner = _field(chunk, "response")
    model = _field(chunk, "model") or _field(inner, "model")
    if isinstance(model, str) and model:
        usage.model = model
    for candidate in (_field(chunk, "usage"), _field(inner, "usage")):
        if candidate is None:
            continue
        usage.tokens_in = max(usage.tokens_in, _tokens(candidate, _INPUT_FIELDS))
        usage.tokens_out = max(usage.tokens_out, _tokens(candidate, _OUTPUT_FIELDS))
        usage.tokens_reasoning = max(usage.tokens_reasoning, _reasoning(candidate))
        usage.tokens_cached_in = max(usage.tokens_cached_in, _cached(candidate))


def _chunk_usage(chunk: Any, usage: _StreamUsage) -> None:
    """Read one streamed chunk into the running usage.

    Every chunk names the model; only the last one carries usage, and only
    when the caller requested ``stream_options={"include_usage": True}``.
    Counts are cumulative totals rather than increments, so the largest seen
    wins and a chunk with a partial or absent count cannot lower them.
    """
    model = _field(chunk, "model")
    if isinstance(model, str) and model:
        usage.model = model
    chunk_usage = _field(chunk, "usage")
    if chunk_usage is None:
        return
    usage.tokens_in = max(usage.tokens_in, _tokens(chunk_usage, _INPUT_FIELDS))
    usage.tokens_out = max(usage.tokens_out, _tokens(chunk_usage, _OUTPUT_FIELDS))
    usage.tokens_reasoning = max(usage.tokens_reasoning, _reasoning(chunk_usage))
    usage.tokens_cached_in = max(usage.tokens_cached_in, _cached(chunk_usage))


def _tokens(usage: Any, names: tuple[str, ...]) -> int:
    """First readable non-negative integer among ``names``, else 0."""
    for name in names:
        value = _field(usage, name)
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            _LOG.debug("runbound: unreadable usage field %s=%r", name, value)
    return 0


def _chat_tool_requests(response: Any) -> list[tuple[str, str]]:
    """Tool calls a chat completion asked for, in the order it asked.

    ``choices[*].message.tool_calls[*].function.{name,arguments}`` — read
    duck-typed, so a dict-shaped response from an OpenAI-compatible endpoint
    reads exactly like an SDK object. A response that raises while being read,
    or that is shaped nothing like this, carries no requests rather than
    failing the call it belongs to.
    """
    try:
        requests = []
        for choice in _field(response, "choices") or ():
            message = _field(choice, "message")
            for call in _field(message, "tool_calls") or ():
                function = _field(call, "function")
                name = _field(function, "name")
                if isinstance(name, str) and name:
                    requests.append((name, normalize_arguments(_field(function, "arguments"))))
        return requests
    except Exception:
        _LOG.debug("runbound: unreadable tool calls on a chat completion", exc_info=True)
        return []


def _responses_tool_requests(response: Any) -> list[tuple[str, str]]:
    """Tool calls a Responses-API response asked for, in output order.

    The Responses API puts them in ``output`` alongside messages and reasoning
    items, told apart by ``type == "function_call"``. Unreadable reads as none.
    """
    try:
        requests = []
        for item in _field(response, "output") or ():
            if _field(item, "type") != "function_call":
                continue
            name = _field(item, "name")
            if isinstance(name, str) and name:
                requests.append((name, normalize_arguments(_field(item, "arguments"))))
        return requests
    except Exception:
        _LOG.debug("runbound: unreadable tool calls on a response", exc_info=True)
        return []


def _chat_chunk_requests(chunk: Any, requests: _StreamRequests) -> None:
    """Read one chat chunk's slice of the tool calls being streamed.

    A streamed tool call is announced once with its name and then dribbles its
    arguments out over later chunks, each identified by the ``index`` it holds
    in its choice's ``tool_calls`` — that pair is the key the fragments are
    joined under, so several parallel calls in one response stay apart.
    """
    for choice in _field(chunk, "choices") or ():
        delta = _field(choice, "delta")
        for call in _field(delta, "tool_calls") or ():
            function = _field(call, "function")
            requests.add(
                (_field(choice, "index"), _field(call, "index")),
                _field(function, "name"),
                _field(function, "arguments"),
            )


def _responses_chunk_requests(chunk: Any, requests: _StreamRequests) -> None:
    """Read one Responses-API event's news about the tool calls being streamed.

    The terminal ``response.completed`` event carries a complete copy of the
    response, so its ``function_call`` items are taken whole and win over
    anything assembled so far. A stream that ends without one falls back to
    the per-call ``response.function_call_arguments.done`` events, which state
    one call's finished arguments (and, on the SDKs that send it, its name).
    """
    kind = _field(chunk, "type")
    if kind == "response.completed":
        completed = _responses_tool_requests(_field(chunk, "response"))
        if completed:
            requests.replace(completed)
    elif kind == "response.function_call_arguments.done":
        requests.add(
            _field(chunk, "item_id") or _field(chunk, "output_index"),
            _field(chunk, "name"),
            _field(chunk, "arguments"),
        )


# --- the request's own output-token cap, for admission (T136) --------------

#: Checked in the order a request is likeliest to carry it: the older chat
#: completions ``max_tokens``, the newer ``max_completion_tokens``, then the
#: Responses API's ``max_output_tokens``.
_OUTPUT_CAP_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def request_output_cap(kwargs: dict) -> int | None:
    """The output-token cap this request stated, if any.

    The first present, positive value among :data:`_OUTPUT_CAP_FIELDS` wins.
    ``None`` for a request with no cap at all, or one that cannot be read —
    the caller's definition of "the request stated no limit."
    """
    try:
        for name in _OUTPUT_CAP_FIELDS:
            value = kwargs.get(name)
            if value is None:
                continue
            value = int(value)
            if value > 0:
                return value
        return None
    except (TypeError, ValueError):
        return None


# --- text, for the opt-in token estimator -----------------------------------


def _chat_request_chars(request_kwargs: dict) -> int:
    """Characters of text a chat request sent, 0 for anything unreadable."""
    return messages_chars(request_kwargs.get("messages"))


def _chat_response_chars(response: Any) -> int:
    """Characters of text a chat completion answered with."""
    try:
        total = 0
        for choice in _field(response, "choices") or ():
            total += content_chars(_field(_field(choice, "message"), "content"))
        return total
    except Exception:
        _LOG.debug("runbound: unreadable chat answer while estimating", exc_info=True)
        return 0


def _chat_chunk_text(chunk: Any) -> str:
    """The text one chat chunk carried: ``choices[0].delta.content``."""
    for choice in _field(chunk, "choices") or ():
        text = _field(_field(choice, "delta"), "content")
        if isinstance(text, str):
            return text
    return ""


def _responses_request_chars(request_kwargs: dict) -> int:
    """Characters of text a Responses request sent.

    ``input`` is either a plain string or a list of items, each with the same
    ``content`` shape a chat message has.
    """
    try:
        value = request_kwargs.get("input")
        if isinstance(value, str):
            return len(value)
        if value is None:
            return 0
        return sum(content_chars(_field(item, "content")) for item in value)
    except Exception:
        _LOG.debug("runbound: unreadable responses input while estimating", exc_info=True)
        return 0


def _responses_response_chars(response: Any) -> int:
    """Characters of text a Responses answer carried.

    ``output_text`` is the SDK's own convenience join; without it, the text
    blocks under each ``output`` item are added up.
    """
    try:
        text = _field(response, "output_text")
        if isinstance(text, str):
            return len(text)
        total = 0
        for item in _field(response, "output") or ():
            total += content_chars(_field(item, "content"))
        return total
    except Exception:
        _LOG.debug("runbound: unreadable responses answer while estimating", exc_info=True)
        return 0


def _responses_chunk_text(chunk: Any) -> str:
    """The text one Responses event carried, on the delta events that have it."""
    if _field(chunk, "type") == "response.output_text.delta":
        delta = _field(chunk, "delta")
        if isinstance(delta, str):
            return delta
    return ""


#: Every call surface this wrapper guards, in the order it reports them.
SURFACES = (
    _Surface(
        "chat.completions.create",
        ("chat", "completions"),
        _chunk_usage,
        _chat_tool_requests,
        _chat_chunk_requests,
        _chat_request_chars,
        _chat_response_chars,
        _chat_chunk_text,
    ),
    _Surface(
        "responses.create",
        ("responses",),
        _responses_chunk_usage,
        _responses_tool_requests,
        _responses_chunk_requests,
        _responses_request_chars,
        _responses_response_chars,
        _responses_chunk_text,
    ),
)

_CHAT_SURFACE, _RESPONSES_SURFACE = SURFACES

#: Where the ``openai`` package itself keeps the methods every client's
#: resources are bound from — what :mod:`runbound.autowrap` patches so that a
#: client built anywhere, by anyone, is guarded without a ``wrap()`` call.
#: Module paths are full and dotted on purpose: ``openai.resources.responses``
#: is not an attribute of ``openai.resources`` until it is imported.
CLASS_TARGETS = (
    ClassTarget(
        "openai:chat",
        "openai.resources.chat.completions",
        "Completions",
        functools.partial(guard_class_create, surface=_CHAT_SURFACE),
    ),
    ClassTarget(
        "openai:chat.async",
        "openai.resources.chat.completions",
        "AsyncCompletions",
        functools.partial(guard_class_create, surface=_CHAT_SURFACE),
    ),
    ClassTarget(
        "openai:responses",
        "openai.resources.responses",
        "Responses",
        functools.partial(guard_class_create, surface=_RESPONSES_SURFACE),
    ),
    ClassTarget(
        "openai:responses.async",
        "openai.resources.responses",
        "AsyncResponses",
        functools.partial(guard_class_create, surface=_RESPONSES_SURFACE),
    ),
)
