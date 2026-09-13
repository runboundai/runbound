"""Anthropic-shaped clients: anything exposing ``client.messages.create``.

Same contract as the OpenAI wrapper and deliberately the same shape, kept as a
separate module because the two providers change independently: this one knows
where Anthropic puts ``create`` and that its usage fields are
``input_tokens`` / ``output_tokens``. The ``anthropic`` package is never
imported; detection is by duck-typing alone.

Sync and async clients are both handled, streamed or not. Streaming usage
arrives split across two event types: ``message_start`` carries the input
tokens, and each ``message_delta`` carries the running output count. Tool
calls the model asks for are read the same way — whole off a finished
response, assembled from ``content_block`` events off a streamed one.
"""

import logging
from typing import Any, Callable

from . import (
    MARKER,
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

#: ``MARKER`` comes from the package: an instance patch and a class patch have
#: to recognize each other's work, so there is one name for "already guarded".

#: This client's shape — the first half of its circuit label, whose second half
#: is the endpoint it points at: Anthropic's health is its own, and so is a
#: proxy's or a gateway's.
PROVIDER = "anthropic"

_INPUT_FIELDS = ("input_tokens", "prompt_tokens")
_OUTPUT_FIELDS = ("output_tokens", "completion_tokens")
#: No Anthropic usage object counts thinking tokens separately today; these are
#: the names a future one would plausibly use, read for free if it ever does.
_REASONING_FIELDS = ("thinking_tokens", "reasoning_tokens")
#: Cache tokens (T139): unlike OpenAI, Anthropic's `input_tokens` EXCLUDES
#: both of these — they are separate, additive counts — so `read_usage` folds
#: both into `tokens_in` for a correct total. Each is also reported onward on
#: its own: the read count (`read_cached`) at its discounted rate, the write
#: count (`read_cache_write`) at its own 125%-of-input premium — a cache
#: write costs *more* than a plain input token, never priced as if it were a
#: discounted read. See `pricing.py`'s `PRICES[model][3]`.
_CACHE_READ_FIELDS = ("cache_read_input_tokens",)
_CACHE_WRITE_FIELDS = ("cache_creation_input_tokens",)


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` off an attribute-style or mapping-style object.

    Returns ``None`` for anything missing or that raises on access.
    """
    try:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)
    except Exception:
        return None


def _create_method(client: Any) -> Callable | None:
    """Return the client's bound ``messages.create``, or None."""
    method = _field(_field(client, "messages"), "create")
    return method if callable(method) else None


def matches(client: Any) -> bool:
    """True if ``client`` looks like an Anthropic messages client."""
    return _create_method(client) is not None


def is_wrapped(client: Any) -> bool:
    """True if this client's create method is already guarded."""
    return _field(_create_method(client), MARKER) is True


def install(client: Any, report: Report, hooks: Hooks = None) -> None:
    """Replace ``client.messages.create`` with a reporting version.

    An ``async def`` original gets an ``async def`` replacement. The original
    call happens first and its response comes back untouched; usage is only
    available afterwards. ``report`` may raise
    :class:`~runbound.exceptions.GuardrailTripped`, which stops the agent
    *after* this response is in hand.

    The patched surface is logged at INFO, so a caller can see what ``wrap()``
    actually covers.

    The call is timed from just before the original runs; a ``report`` that
    predates timing and takes only three arguments still works.
    ``create(stream=True)`` returns its event iterator wrapped in a guard that
    yields events untouched and reports the call, with the time the whole
    stream took, when it ends. See
    :class:`runbound.wrappers._GuardedStream`.

    ``hooks`` (optional, the api's) runs ``before`` each request — which may
    refuse it with :class:`~runbound.exceptions.CircuitOpen` when this
    endpoint's circuit is open, or because too many calls to it are already in
    flight — and reports the outcome afterwards, including every tool call the
    model asked for, which is how a model looping on one tool is caught even
    when the developer dispatches those calls by hand. Without hooks the
    wrapper observes exactly what it always did.

    Which endpoint that is comes from :func:`provider_label`, worked out once
    here and closed over: ``"anthropic@api.anthropic.com"`` for the real thing,
    ``"anthropic@my-proxy:8080"`` for a gateway in front of it.
    """
    original = _create_method(client)
    hooks = _NO_HOOKS if hooks is None else hooks
    label = provider_label(PROVIDER, client)
    is_async = _is_async_callable(original)

    client.messages.create = guarded_create(
        original, report, hooks, fixed_label(label), _finish
    )
    log_guarded_surfaces([("messages.create", is_async)])


def guard_class_create(original: Callable, report: Report, hooks: Hooks) -> Callable:
    """Guard a ``create`` patched onto ``anthropic``'s own class, not a client.

    Used by :mod:`runbound.autowrap`. One function serves every client in the
    process, so the endpoint is read per call off the resource it was called on
    — see :func:`runbound.wrappers.resource_label`.
    """
    return guarded_create(original, report, hooks, resource_label(PROVIDER), _finish)


def _finish(
    response: Any,
    kwargs: dict,
    report: Report,
    started_at: float,
    hooks: Hooks = _NO_HOOKS,
    label: str = PROVIDER,
    *,
    is_async: bool,
) -> Any:
    """Report a finished call, or hand back the stream that will report itself.

    A streamed call reports its own outcome when it ends, so the guard carries
    the hooks rather than this function claiming a success that has not
    happened yet.

    A finished one is reported in a fixed order: usage, then ``hooks.success``
    — the call did succeed — then the tool calls the model asked for, last
    because that is the step that can raise: a model looping on one tool trips
    the session here, with the response already in the caller's hands and its
    dispatch still ahead of them.
    """
    stream = _guard_stream(
        response,
        kwargs,
        report,
        _chunk_usage,
        is_async=is_async,
        started_at=started_at,
        hooks=hooks,
        provider=label,
        chunk_requests=_chunk_requests,
        chunk_text=_chunk_text,
        request_chars=_request_chars(kwargs) if estimating(hooks) else 0,
    )
    if stream is not None:
        return stream
    _report_call(report, response, kwargs, started_at, hooks)
    hooks.success(label)
    emit_tool_requests(hooks, read_tool_requests(response))
    return response


def _report_call(
    report: Report, response: Any, request_kwargs: dict, started_at: float, hooks: Hooks
) -> None:
    """Read a finished non-streamed call and hand it to ``report``."""
    model, tokens_in, tokens_out = read_usage(response, request_kwargs)
    if not (tokens_in or tokens_out) and estimating(hooks):
        tokens_in = estimated_tokens(_request_chars(request_kwargs))
        tokens_out = estimated_tokens(_response_chars(response))
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
        read_cache_write(response),
    )


def _request_chars(request_kwargs: dict) -> int:
    """Characters of text a messages request sent, system prompt included."""
    total = messages_chars(request_kwargs.get("messages"))
    system = request_kwargs.get("system")
    return total + (len(system) if isinstance(system, str) else 0)


def request_output_cap(kwargs: dict) -> int | None:
    """The output-token cap this request stated (T136 admission).

    Anthropic's ``messages.create`` requires ``max_tokens`` on every request
    (unlike OpenAI's optional caps), so this reads that field alone. ``None``
    when it is missing or unreadable — fail-open, never a reason to treat a
    well-formed request as capless.
    """
    try:
        value = kwargs.get("max_tokens")
        if value is None:
            return None
        value = int(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _response_chars(response: Any) -> int:
    """Characters of text a messages response answered with."""
    try:
        total = 0
        for block in _field(response, "content") or ():
            text = _field(block, "text")
            if isinstance(text, str):
                total += len(text)
        return total
    except Exception:
        _LOG.debug("runbound: unreadable messages answer while estimating", exc_info=True)
        return 0


def _chunk_text(chunk: Any) -> str:
    """The text one streamed event carried: a ``text_delta`` content block."""
    if _field(chunk, "type") != "content_block_delta":
        return ""
    delta = _field(chunk, "delta")
    if _field(delta, "type") != "text_delta":
        return ""
    text = _field(delta, "text")
    return text if isinstance(text, str) else ""


def read_usage(response: Any, request_kwargs: dict) -> tuple[str | None, int, int]:
    """Extract ``(model, tokens_in, tokens_out)`` from a messages response.

    Missing or malformed usage reads as zero tokens; the model falls back to
    the request's ``model`` argument.

    ``tokens_in`` folds in both ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens`` (T139): Anthropic's ``input_tokens`` is
    only the *uncached* portion of the prompt, so a cache hit (or a cache
    write) would otherwise vanish from the total instead of just moving
    to a different rate. See :func:`read_cached` for the discounted subset
    and :func:`read_cache_write` for the premium one.
    """
    usage = _field(response, "usage")
    model = _field(response, "model") or request_kwargs.get("model")
    tokens_in = (
        _tokens(usage, _INPUT_FIELDS)
        + _tokens(usage, _CACHE_READ_FIELDS)
        + _tokens(usage, _CACHE_WRITE_FIELDS)
    )
    return (
        model if isinstance(model, str) and model else None,
        tokens_in,
        _tokens(usage, _OUTPUT_FIELDS),
    )


def read_reasoning(response: Any) -> int:
    """Thinking tokens on a messages response, 0 — as today's API always is.

    Anthropic bills extended thinking inside ``output_tokens`` and reports no
    separate count, so this reads 0 unless a future usage object grows one of
    ``_REASONING_FIELDS``.
    """
    return _tokens(_field(response, "usage"), _REASONING_FIELDS)


def read_cached(response: Any) -> int:
    """Cache-*read* tokens billed at Anthropic's discounted rate (T139), or 0.

    Only ``cache_read_input_tokens`` — the subset of ``tokens_in`` (see
    :func:`read_usage`) that was a cache hit and gets the discount.
    ``cache_creation_input_tokens`` (a cache *write*) is folded into
    ``tokens_in`` too, but never reported here: see :func:`read_cache_write`
    — it costs more than a plain input token, not less, so it must never be
    priced as if it were cached.
    """
    return _tokens(_field(response, "usage"), _CACHE_READ_FIELDS)


def read_cache_write(response: Any) -> int:
    """Cache-*write* tokens billed at Anthropic's 125% premium (T139), or 0.

    Only ``cache_creation_input_tokens`` — the subset of ``tokens_in`` (see
    :func:`read_usage`) that wrote a new cache entry. Priced (in
    :mod:`runbound.pricing`) at ``PRICES[model][3]`` when the model publishes
    one, else at the plain input rate — never at the cache-*read* discount
    :func:`read_cached` reports, which would price a write as if it cost
    less than an ordinary token when it actually costs more.
    """
    return _tokens(_field(response, "usage"), _CACHE_WRITE_FIELDS)


def read_tool_requests(response: Any) -> list[tuple[str, str]]:
    """Tool calls a messages response asked for, in the order it asked.

    ``content[*]`` with ``type == "tool_use"``: a name and an already-decoded
    ``input`` object, which is re-encoded with sorted keys so the order the
    model happened to emit its arguments in cannot hide a repeat. A response
    that cannot be read this way carries no requests rather than failing the
    call it belongs to.
    """
    try:
        requests = []
        for block in _field(response, "content") or ():
            if _field(block, "type") != "tool_use":
                continue
            name = _field(block, "name")
            if isinstance(name, str) and name:
                requests.append((name, normalize_arguments(_field(block, "input"))))
        return requests
    except Exception:
        _LOG.debug("runbound: unreadable tool calls on a messages response", exc_info=True)
        return []


def _chunk_requests(chunk: Any, requests: _StreamRequests) -> None:
    """Read one streamed event's news about the tool calls being streamed.

    A tool call opens with a ``content_block_start`` naming it and then arrives
    as ``input_json_delta`` fragments, both carrying the block ``index`` the
    fragments are joined under. A tool call whose input never produced a
    fragment ends with empty arguments rather than the ``{}`` the non-streamed
    path would report — the only place the two hash differently.
    """
    kind = _field(chunk, "type")
    if kind == "content_block_start":
        block = _field(chunk, "content_block")
        if _field(block, "type") == "tool_use":
            requests.add(_field(chunk, "index"), name=_field(block, "name"))
    elif kind == "content_block_delta":
        delta = _field(chunk, "delta")
        if _field(delta, "type") == "input_json_delta":
            requests.add(_field(chunk, "index"), arguments=_field(delta, "partial_json"))


def _chunk_usage(chunk: Any, usage: _StreamUsage) -> None:
    """Read one streamed event into the running usage.

    ``message_start`` opens the stream with the model and the input tokens;
    every ``message_delta`` restates the output tokens produced so far. Both
    are folded in with ``max``, so events arriving out of order or repeating
    an earlier count cannot lower the totals. Every other event type is
    ignored.
    """
    kind = _field(chunk, "type")
    if kind == "message_start":
        message = _field(chunk, "message")
        model = _field(message, "model")
        if isinstance(model, str) and model:
            usage.model = model
        message_usage = _field(message, "usage")
        cache_read = _tokens(message_usage, _CACHE_READ_FIELDS)
        cache_write = _tokens(message_usage, _CACHE_WRITE_FIELDS)
        usage.tokens_in = max(
            usage.tokens_in,
            _tokens(message_usage, _INPUT_FIELDS) + cache_read + cache_write,
        )
        usage.tokens_cached_in = max(usage.tokens_cached_in, cache_read)
        usage.tokens_cache_write_in = max(usage.tokens_cache_write_in, cache_write)
        usage.tokens_out = max(usage.tokens_out, _tokens(message_usage, _OUTPUT_FIELDS))
        usage.tokens_reasoning = max(
            usage.tokens_reasoning, _tokens(message_usage, _REASONING_FIELDS)
        )
    elif kind == "message_delta":
        delta_usage = _field(chunk, "usage")
        usage.tokens_out = max(usage.tokens_out, _tokens(delta_usage, _OUTPUT_FIELDS))
        usage.tokens_reasoning = max(
            usage.tokens_reasoning, _tokens(delta_usage, _REASONING_FIELDS)
        )


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


#: Where the ``anthropic`` package itself keeps the method every client's
#: ``messages`` resource is bound from — what :mod:`runbound.autowrap`
#: patches, so a client built anywhere is guarded without a ``wrap()`` call.
CLASS_TARGETS = (
    ClassTarget(
        "anthropic:messages",
        "anthropic.resources.messages",
        "Messages",
        guard_class_create,
    ),
    ClassTarget(
        "anthropic:messages.async",
        "anthropic.resources.messages",
        "AsyncMessages",
        guard_class_create,
    ),
)
