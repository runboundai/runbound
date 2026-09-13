"""Per-provider client wrappers, dispatched by :func:`runbound.wrap`.

Every wrapper module implements the same four functions, which is the whole
interface a new provider has to satisfy::

    matches(client) -> bool                  # does this client have our shape?
    is_wrapped(client) -> bool               # already guarded?
    install(client, report, hooks=None)      # patch every call site in place
    read_usage(response, request_kwargs)     # -> (model, tokens_in, tokens_out)
    read_reasoning(response)                 # -> thinking tokens, 0 if none

``report`` is supplied by the API layer and is the only thing wrappers do with
what they read: they price nothing, decide nothing, and store nothing.

``hooks`` is the other half of that: what to call *around* a request —
``before(provider, model=None, request=None)`` before it goes out (which may
refuse it — ``request`` is the raw call kwargs, read by ``Engine.admit``'s
opt-in ``budget_admission`` estimate; T136), then ``success(provider)`` or
``error(model, exc, duration_s, provider)``,
``release(provider)`` when the call is over however it ended, and
``tool_request(name, args_hash)`` for each tool call the model asked for in
what came back. A streamed call that is garbage collected before it ends
instead reports ``abandoned(model, tokens_in, tokens_out, duration_s,
provider, estimated)`` — see :class:`_StreamGuard` — and an async caller under
a running event loop may owe a throttle delay afterwards, fetched with
``take_pending_delay()``. All of it is optional, and a wrapper installed
without hooks calls :data:`_NO_HOOKS` and behaves exactly as it did before
circuit breaking existed.

``provider`` is not the provider's *name* but :func:`provider_label` — the
client's shape and the host it points at, ``"openai@localhost:11434"`` — so
two OpenAI-compatible endpoints in one process are counted, refused and
reported apart.

Reading those requests is a second thing wrappers know how to do with someone
else's response, and the hashing they share lives here: :func:`request_hash`,
:func:`normalize_arguments` and :func:`emit_tool_requests`, plus
:class:`_StreamRequests` for the streamed case, where one tool call arrives as
a name and a run of argument fragments.

This module also holds what both providers share and neither should own: the
stopwatch every call is timed with, how to tell an ``async`` ``create`` from a
sync one, and the proxies that guard a streamed response. The proxies are
provider-agnostic — the only things that differ per provider are the
``chunk_usage(chunk, usage)`` and ``chunk_requests(chunk, requests)`` hooks
that read that provider's chunk shapes into a :class:`_StreamUsage` and a
:class:`_StreamRequests`.

``PROVIDERS`` is the dispatch order used by :func:`runbound.wrap`.
"""

import asyncio
import contextvars
import dataclasses
import functools
import hashlib
import inspect
import json
import logging
import time
import urllib.parse
import weakref
from typing import Any, Callable, NamedTuple, Sequence

from ..exceptions import GuardrailTripped

_LOG = logging.getLogger("runbound")

#: Set on an installed ``create`` so a second install of any kind is a no-op.
#: One name for both installs and both providers: an instance patch must be
#: able to see that the class underneath it is already guarded, and vice versa.
MARKER = "__runbound_wrapped__"

#: How many ``functools.wraps`` layers to look through when sniffing for async.
_MAX_UNWRAP = 10

#: Set once the process has been told that a stream carried no usage at all.
_ZERO_TOKEN_STREAM_WARNED = False

#: Set once the process has been told that token counts are being estimated.
_ESTIMATED_TOKENS_WARNED = False

#: The host part of a label for a client that would not say where it points.
DEFAULT_HOST = "default"

#: Characters of text one estimated token stands for. A crude, universal
#: approximation — no tokenizer, no dependency, no per-model table.
CHARS_PER_TOKEN = 4

#: What a wrapper hands the API layer:
#: (model, tokens_in, tokens_out, duration_s, tokens_reasoning, tokens_cached_in,
#: tokens_cache_write_in). ``tokens_cached_in`` and ``tokens_cache_write_in``
#: (T139) are both slices of ``tokens_in``, never additional — see
#: :func:`_call_report`'s fallback for a ``report`` that predates either.
Report = Callable[[str | None, int, int, float, int, int, int], None]


class _NoHooks:
    """The hooks of a wrapper installed without any: nothing but no-ops.

    Lets ``install(client, report)`` keep its two-argument form — the shape
    every caller before the circuit breaker used — without a ``None`` check at
    each of the call sites in each wrapper.
    """

    #: These hooks never estimate: they have no configuration to read it from.
    estimate_tokens = False

    def before(
        self, provider: str, model: str | None = None, request: dict | None = None
    ) -> None:
        return None

    def success(self, provider: str) -> None:
        return None

    def error(
        self, model: str | None, exc: BaseException, duration_s: float, provider: str
    ) -> None:
        return None

    def release(self, provider: str) -> None:
        return None

    def tool_request(self, name: str, args_hash: str | None) -> None:
        return None

    def abandoned(
        self,
        model: str | None,
        tokens_in: int,
        tokens_out: int,
        duration_s: float,
        provider: str,
        estimated: bool,
    ) -> None:
        return None

    def take_pending_delay(self) -> float:
        return 0.0


#: The hooks a wrapper uses when it was installed without any.
_NO_HOOKS = _NoHooks()

#: Anything shaped like :class:`_NoHooks`; the api supplies the real one.
Hooks = Any

#: The namespace every model-requested tool call's hash is prefixed with, so
#: requests and executed ``@tool`` calls are never counted as one stream.
REQUEST_NAMESPACE = "req:"


def field(obj: Any, name: str) -> Any:
    """Read ``name`` off an attribute-style or mapping-style object.

    Returns ``None`` for anything missing or that raises on access: everything
    a wrapper reads belongs to somebody else's SDK and may do anything at all.
    """
    try:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)
    except Exception:
        return None


def provider_label(shape: str, client: Any) -> str:
    """``"{shape}@{host}"`` — the key one client's calls are counted under.

    The shape says which wrapper reads this client; the host says *which box*
    it talks to. Both matter: a dead vLLM server and OpenAI proper are both
    "openai" shaped, and letting one open the other's circuit would refuse
    traffic to a provider that is perfectly healthy.

    The host is read duck-typed off ``client.base_url`` — a string, an
    ``httpx.URL``, anything with a ``__str__`` — as the URL's lowercased
    netloc, port included. A client that names no endpoint, or whose
    ``base_url`` cannot be read at all, is ``"<shape>@default"``: the SDK's own
    default, whatever that is today.
    """
    return f"{shape}@{_client_host(client)}"


def _client_host(client: Any) -> str:
    """The netloc ``client`` points at, lowercased, or :data:`DEFAULT_HOST`."""
    try:
        base_url = getattr(client, "base_url", None)
        if base_url is None:
            return DEFAULT_HOST
        return urllib.parse.urlsplit(str(base_url)).netloc.strip().lower() or DEFAULT_HOST
    except Exception:
        _LOG.debug("runbound: unreadable base_url on a client", exc_info=True)
        return DEFAULT_HOST


#: Works out which endpoint one call is going to, from that call's arguments.
LabelResolver = Callable[[tuple], str]


def fixed_label(label: str) -> LabelResolver:
    """A resolver for an instance install: every call goes to one endpoint.

    ``wrap(client)`` patches one client's own resources, so the endpoint is
    known at install time and no request pays for working it out again.
    """

    def resolve(args: tuple) -> str:
        return label

    return resolve


def resource_label(shape: str) -> LabelResolver:
    """A resolver for a class install: read the endpoint off the caller.

    A patched ``Completions.create`` is shared by every client in the process,
    so the endpoint is whatever ``self`` — the resource the method was called
    on — points at: ``args[0]._client.base_url``. Anything unreadable (no
    arguments, no ``_client``, no ``base_url``) falls open to
    ``"<shape>@default"`` rather than failing the call.
    """

    def resolve(args: tuple) -> str:
        try:
            if not args:
                return f"{shape}@{DEFAULT_HOST}"
            return provider_label(shape, field(args[0], "_client"))
        except Exception:
            _LOG.debug("runbound: unreadable client on a guarded resource", exc_info=True)
            return f"{shape}@{DEFAULT_HOST}"

    return resolve


class ClassTarget(NamedTuple):
    """One provider-SDK class whose ``create`` :mod:`runbound.autowrap` patches.

    ``module`` is the **full dotted path**, resolved with
    ``importlib.import_module``: ``openai.resources.responses`` is not an
    attribute of ``openai.resources`` until something imports it, so walking
    attributes down from the package would silently leave that surface
    unguarded. ``guard(original, report, hooks)`` builds the replacement, and
    knows which surface's readers it belongs to.
    """

    label: str
    module: str
    attr: str
    guard: Callable[[Callable, Report, Hooks], Callable]


def guarded_create(
    original: Callable,
    report: Report,
    hooks: Hooks,
    label_for: LabelResolver,
    finish: Callable[..., Any],
) -> Callable:
    """Build the reporting replacement for one ``create``, sync or async to match.

    The one code path behind both installs: :func:`runbound.wrap` patching a
    client's own resources, and :mod:`runbound.autowrap` patching the SDK's
    classes. They differ in exactly one thing — how the endpoint label is
    worked out — which is what ``label_for`` is.

    Three moments, in this order: ``hooks.before`` (which may refuse the call
    outright, before the provider is touched), the original, and then either
    ``finish`` — the report, ``hooks.success`` and the tool calls the model
    asked for — or ``hooks.error`` and the original exception, re-raised
    untouched, unless recording the failure is itself what trips the session.

    The label is resolved **once**, at the top, so ``before``, ``error`` and
    ``release`` can never disagree about which endpoint this call belonged to.
    The slot ``before`` took is given back in a ``finally`` on every path —
    except when the call turns out to be a stream, which is one call that has
    barely started and gives its own slot back when it ends.
    """

    def create(*args: Any, **kwargs: Any) -> Any:
        label = label_for(args)
        call_before(hooks, label, _request_model(kwargs), kwargs)
        started_at = _now()
        streaming = False
        try:
            try:
                response = original(*args, **kwargs)
            except Exception as exc:
                hooks.error(_request_model(kwargs), exc, _elapsed(started_at), label)
                raise
            result = finish(
                response, kwargs, report, started_at, hooks, label, is_async=False
            )
            streaming = isinstance(result, _StreamGuard)
            return result
        finally:
            if not streaming:
                release_slot(hooks, label)

    async def acreate(*args: Any, **kwargs: Any) -> Any:
        label = label_for(args)
        call_before(hooks, label, _request_model(kwargs), kwargs)
        started_at = _now()
        streaming = False
        try:
            try:
                response = await original(*args, **kwargs)
            except Exception as exc:
                hooks.error(_request_model(kwargs), exc, _elapsed(started_at), label)
                raise
            result = finish(
                response, kwargs, report, started_at, hooks, label, is_async=True
            )
            streaming = isinstance(result, _StreamGuard)
            if not streaming:
                await _await_pending_delay(hooks)
            return result
        finally:
            if not streaming:
                release_slot(hooks, label)

    installed = acreate if _is_async_callable(original) else create
    try:
        functools.update_wrapper(installed, original)
    except Exception:  # exotic callables need no cosmetics
        pass
    # After update_wrapper, never before: it copies the original's __dict__
    # over ours and would take the marker straight back off again.
    setattr(installed, MARKER, True)
    return installed


def call_before(
    hooks: Hooks, provider: str, model: str | None, request: dict | None = None
) -> None:
    """Call ``hooks.before``, passing the model and request kwargs it can use.

    ``request`` is the raw call kwargs (T136) — what ``Engine.admit`` reads
    to estimate an opt-in admission budget. Three shapes, tried in decreasing
    order of how much a hook understands: today's (``provider``, ``model=``,
    ``request=``), the one before T136 added ``request`` (``provider``,
    ``model=`` only), and the original, one-argument ``before`` from before
    the model parameter existed. Mirrors :func:`_call_report`'s tolerance for
    an older signature at each step: only a ``TypeError`` raised by the call
    itself — no frame of ``before`` on its own traceback — falls back a step;
    anything else, including a deliberate
    :class:`~runbound.exceptions.CircuitOpen` or
    :class:`~runbound.exceptions.GuardrailTripped`, propagates.
    """
    try:
        hooks.before(provider, model=model, request=request)
    except TypeError as exc:
        if exc.__traceback__ is not None and exc.__traceback__.tb_next is not None:
            raise
        try:
            hooks.before(provider, model=model)
        except TypeError as exc2:
            if exc2.__traceback__ is not None and exc2.__traceback__.tb_next is not None:
                raise
            hooks.before(provider)


async def _await_pending_delay(hooks: Hooks) -> None:
    """Await the throttle delay the engine stashed for this task, if any.

    ``take_pending_delay`` is how an async caller pays back the delay the
    engine could not make it wait through synchronously (it will not block an
    event loop). Hooks that predate it, or that raise reading it, cost the
    call nothing rather than failing it.
    """
    try:
        delay = getattr(hooks, "take_pending_delay", lambda: 0.0)()
    except Exception:
        delay = 0.0
    if isinstance(delay, (int, float)) and delay > 0:
        await asyncio.sleep(delay)


def estimating(hooks: Hooks) -> bool:
    """Does this call want tokens estimated when the endpoint reports none?

    Read off the hooks at call time, so ``init(estimate_tokens=True)`` applies
    to clients wrapped before it. Hooks written before the estimator existed
    say nothing and therefore say no.
    """
    try:
        return bool(getattr(hooks, "estimate_tokens", False))
    except Exception:
        return False


def release_slot(hooks: Hooks, provider: str) -> None:
    """Give back the in-flight slot ``before`` took, once, without ever raising.

    Tolerates hooks that predate the in-flight cap and have no ``release``,
    exactly as :func:`_call_report` tolerates a ``report`` that predates call
    timing.
    """
    try:
        release = getattr(hooks, "release", None)
        if release is not None:
            release(provider)
    except Exception:
        _LOG.warning("runbound could not release an in-flight slot", exc_info=True)


def estimated_tokens(chars: int) -> int:
    """``ceil(chars / 4)`` — the estimator's whole model of a tokenizer.

    Deliberately crude and deliberately cheap: it exists so that a self-hosted
    server that reports no usage at all is counted as *something* rather than
    as free traffic. Never used when the endpoint reported real usage.
    """
    try:
        return -(-max(int(chars), 0) // CHARS_PER_TOKEN)
    except (TypeError, ValueError):
        return 0


def warn_estimated_tokens(model: str | None) -> None:
    """Say once per process that token counts are being estimated, not read."""
    global _ESTIMATED_TOKENS_WARNED
    if _ESTIMATED_TOKENS_WARNED:
        return
    _ESTIMATED_TOKENS_WARNED = True
    _LOG.warning(
        "runbound: token usage estimated (chars/4) for model %r — the endpoint "
        "returned no usage",
        model,
    )


def content_chars(content: Any) -> int:
    """Characters of text in one message's ``content``, 0 for anything else.

    A message carries either a plain string or a list of parts, of which only
    the ones with a ``text`` say anything about size — an image part costs
    tokens too, but guessing how many would be worse than not guessing.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, (list, tuple)):
        total = 0
        for part in content:
            text = field(part, "text")
            if isinstance(text, str):
                total += len(text)
        return total
    return 0


def messages_chars(messages: Any) -> int:
    """Characters of text across a chat request's ``messages``, 0 if unreadable."""
    try:
        if isinstance(messages, (str, bytes)) or messages is None:
            return 0
        return sum(content_chars(field(message, "content")) for message in messages)
    except Exception:
        _LOG.debug("runbound: unreadable messages while estimating tokens", exc_info=True)
        return 0


def request_hash(name: str, arguments: str) -> str:
    """The loop hash of a tool call the model *asked* for.

    ``"req:"`` + sha256 of a canonical repr of ``(name, arguments)``: the same
    tool asked for with the same arguments always hashes the same, and the
    prefix keeps the request stream apart from the hashes of tools that
    actually ran. Only the digest is ever recorded — arguments never leave the
    caller's process.
    """
    canonical = repr((name, arguments))
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()
    return REQUEST_NAMESPACE + digest


def normalize_arguments(value: Any) -> str:
    """One tool call's arguments as the stable string its hash is taken over.

    Providers hand the same arguments over in several shapes: a JSON string
    (OpenAI), a decoded object (Anthropic, and OpenAI-compatible endpoints
    that decode for you), or a run of streamed fragments joined back together.
    Anything that decodes to JSON is re-encoded with sorted keys, so key order
    and whitespace — which a model varies freely between otherwise identical
    calls — cannot hide a loop, and a streamed call hashes the same as the
    non-streamed one it is a copy of. Anything else is used as it stands, and
    an unreadable value carries no loop signal rather than failing.
    """
    try:
        if value is None:
            return ""
        decoded = value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except Exception:
                return value
        if isinstance(decoded, (dict, list)):
            return json.dumps(decoded, sort_keys=True, default=str)
        return value if isinstance(value, str) else str(value)
    except Exception:
        _LOG.debug("runbound: unreadable tool-call arguments", exc_info=True)
        return ""


def emit_tool_requests(hooks: Hooks, requests: Sequence[tuple[str, str]]) -> None:
    """Report every tool call one response asked for, in the order it asked.

    Each ``(name, arguments)`` pair becomes one ``hooks.tool_request`` with its
    :func:`request_hash`. A :class:`GuardrailTripped` raised by detection —
    the model is looping — propagates to the caller, which is the whole point:
    it reaches them holding the response, before they dispatch what it asked
    for. Anything else that goes wrong is logged and dropped, and the
    remaining requests are still reported.
    """
    for name, arguments in requests:
        try:
            hooks.tool_request(name, request_hash(name, arguments))
        except GuardrailTripped:
            raise
        except Exception:
            _LOG.debug("runbound: could not record a model-requested tool call", exc_info=True)


class _StreamRequests:
    """Model-requested tool calls being assembled from a stream's chunks.

    A streamed tool call arrives in pieces: a name once, then its arguments a
    fragment at a time, interleaved with every other call in the same
    response. Fragments are keyed by whatever the provider indexes them with
    and joined in arrival order; a part that never gets a name is dropped,
    because a nameless request says nothing.

    :meth:`replace` is for providers that restate the whole set on a terminal
    event — that copy is complete and authoritative, so it wins over the
    fragments.
    """

    def __init__(self) -> None:
        self._parts: dict[Any, list] = {}
        self._final: list[tuple[str, str]] | None = None

    def add(self, key: Any, name: Any = None, arguments: Any = None) -> None:
        """Fold one chunk's news about the call at ``key`` into what is known."""
        part = self._parts.setdefault(key, ["", []])
        if isinstance(name, str) and name and not part[0]:
            part[0] = name
        if isinstance(arguments, str) and arguments:
            part[1].append(arguments)

    def replace(self, requests: Sequence[tuple[str, str]]) -> None:
        """Take a terminal event's complete list over the assembled fragments."""
        self._final = list(requests)

    def collected(self) -> list[tuple[str, str]]:
        """Every named request the stream carried, ready to emit."""
        if self._final is not None:
            return [(name, normalize_arguments(args)) for name, args in self._final]
        return [
            (name, normalize_arguments("".join(fragments)))
            for name, fragments in self._parts.values()
            if name
        ]


def _request_model(request_kwargs: dict) -> str | None:
    """The model a request asked for, when it named one readably.

    A failed call has no response to read the model off, so the request is all
    there is; anything that is not a non-empty string is reported as unknown.
    """
    try:
        model = request_kwargs.get("model")
    except Exception:
        return None
    return model if isinstance(model, str) and model else None


def _now() -> float:
    """The stopwatch every wrapper times its calls with.

    One function, one clock: reading ``time`` off this module's namespace at
    call time is what lets a test swap the clock for every path at once.
    """
    return time.monotonic()


def _elapsed(started_at: float | None) -> float:
    """Seconds since ``started_at``, never negative, 0.0 if it was not timed."""
    if started_at is None:
        return 0.0
    return max(_now() - started_at, 0.0)


def _call_report(
    report: Report,
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    duration_s: float,
    tokens_reasoning: int,
    tokens_cached_in: int = 0,
    tokens_cache_write_in: int = 0,
) -> None:
    """Hand one call's usage to ``report``, tolerating an older ``report``.

    Four shapes are tried, newest first, each falling back to the last only
    on a binding failure: the current 7-argument form (T139 added
    ``tokens_cache_write_in`` for Anthropic's cache-write premium), the
    6-argument form that predates it (T139's own ``tokens_cached_in``), the
    5-argument form before that, and the 3-argument ``(model, tokens_in,
    tokens_out)`` form from before timing existed at all. Only a
    ``TypeError`` raised with no frame of ``report`` on its traceback counts
    as "did not accept this shape" — one from inside ``report`` propagates,
    so a broken callback is never invoked twice.
    """
    try:
        report(
            model,
            tokens_in,
            tokens_out,
            duration_s,
            tokens_reasoning,
            tokens_cached_in,
            tokens_cache_write_in,
        )
    except TypeError as exc:
        if exc.__traceback__ is not None and exc.__traceback__.tb_next is not None:
            raise
        _LOG.debug("runbound: report() predates cache-write reporting; trying 6 args")
        try:
            report(model, tokens_in, tokens_out, duration_s, tokens_reasoning, tokens_cached_in)
        except TypeError as exc2:
            if exc2.__traceback__ is not None and exc2.__traceback__.tb_next is not None:
                raise
            _LOG.debug("runbound: report() predates cached-token reporting; trying 5 args")
            try:
                report(model, tokens_in, tokens_out, duration_s, tokens_reasoning)
            except TypeError as exc3:
                if exc3.__traceback__ is not None and exc3.__traceback__.tb_next is not None:
                    raise
                _LOG.debug("runbound: report() predates call timing; reporting tokens only")
                report(model, tokens_in, tokens_out)  # type: ignore[call-arg]


def log_guarded_surfaces(surfaces: Sequence[tuple[str, bool]]) -> None:
    """Log at INFO exactly which call surfaces an ``install`` just patched.

    ``surfaces`` pairs each surface's dotted name with whether the method it
    replaced was ``async``. Wrapping is silent surgery on someone else's
    object, and "wrap() succeeded" says nothing about *what* it covers — this
    line is how a caller learns that, say, ``responses.create`` is guarded and
    ``embeddings.create`` is not. Patching nothing logs nothing: a second
    ``wrap()`` of the same client claims no work.
    """
    if not surfaces:
        return
    modes = {is_async for _, is_async in surfaces}
    mode = "async" if modes == {True} else "sync" if modes == {False} else "mixed"
    _LOG.info("runbound: guarding %s (%s)", ", ".join(name for name, _ in surfaces), mode)


def _warn_zero_token_stream() -> None:
    """Say once per process that a guarded stream carried no usage at all.

    A stream whose provider never sent usage records a well-formed zero-token
    step, which every token and dollar limit then reads as free traffic — the
    guard reports green while counting nothing. Once per process is enough:
    the cause is a request option the caller controls, not a transient.
    """
    global _ZERO_TOKEN_STREAM_WARNED
    if _ZERO_TOKEN_STREAM_WARNED:
        return
    _ZERO_TOKEN_STREAM_WARNED = True
    _LOG.warning(
        "runbound: a streamed call ended with no usage data; token/cost detection "
        "cannot see this traffic. For OpenAI pass stream_options={'include_usage': True}."
    )


def _failing(exc_info: tuple) -> bool:
    """True if a ``__exit__`` was called because the block raised.

    ``with`` always passes three arguments and a clean exit passes three
    ``None``s, but this proxy is handed whatever the host passes it.
    """
    return bool(exc_info) and exc_info[0] is not None


@dataclasses.dataclass
class _StreamUsage:
    """What a stream's chunks have said so far about the call they belong to.

    Starts from the request's ``model`` (chunks that name one override it) and
    zero tokens, so a stream that never reveals its usage still reports a
    well-formed, zero-token step.
    """

    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    tokens_cached_in: int = 0  # T139: subset of tokens_in served from cache (read)
    tokens_cache_write_in: int = 0  # T139: subset of tokens_in that wrote a cache entry


#: Reads one chunk into the running :class:`_StreamUsage`. Provider-supplied.
ChunkUsage = Callable[[Any, _StreamUsage], None]

#: Reads one chunk's model-requested tool calls into the running
#: :class:`_StreamRequests`. Provider-supplied, and optional: a stream whose
#: shapes nobody taught us to read simply reports no requests.
ChunkRequests = Callable[[Any, _StreamRequests], None]

#: Reads the text one chunk carried, for the token estimator. Provider-supplied
#: and optional; anything unreadable is no text.
ChunkText = Callable[[Any], str]


class _UsageParseTracker:
    """Delegates to a real :class:`_StreamUsage`, noting a token-field write.

    Handed to a provider's ``chunk_usage`` reader in the running usage's own
    place: every reader in this package writes ``tokens_in``, ``tokens_out``
    or ``tokens_reasoning`` exactly when the chunk it just read carried a
    usage object of some kind — and leaves them untouched on a chunk that
    carried none (see ``openai_wrapper._chunk_usage``'s early return and
    ``anthropic_wrapper._chunk_usage``'s event-kind gate). A write through
    this tracker, whatever value it carries, is therefore exactly the "a
    usage object was actually parsed" signal :attr:`_StreamLedger.usage_seen`
    needs: a stream whose only usage block truthfully said zero tokens must
    be believed, not re-estimated as if it had said nothing at all.
    """

    _TOKEN_FIELDS = frozenset(
        {
            "tokens_in",
            "tokens_out",
            "tokens_reasoning",
            "tokens_cached_in",
            "tokens_cache_write_in",
        }
    )

    def __init__(self, usage: "_StreamUsage") -> None:
        self.__dict__["_usage"] = usage
        self.__dict__["parsed"] = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_usage"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._TOKEN_FIELDS:
            self.__dict__["parsed"] = True
        setattr(self.__dict__["_usage"], name, value)


class _StreamLedger:
    """What a stream's finalizer needs, kept apart from the proxy itself.

    :func:`weakref.finalize` holds a strong reference to whatever arguments it
    is given but only a *weak* one to the object it is watching — so this is
    the object those arguments may be, and the proxy (:class:`_StreamGuard`)
    must never appear among them, directly or by way of a closure. Nothing
    here points back at the proxy, the provider's stream, or the reporting
    callback: just the running usage, the counters an abandoned report needs,
    and the hooks to report it through.

    ``usage`` is the *same* :class:`_StreamUsage` instance the proxy reads
    chunks into, not a copy — the ledger sees every update as it happens
    without the proxy having to push anything to it.

    ``last_at`` is the stopwatch reading from the most recently observed
    chunk, kept apart from ``started_at`` so an abandoned stream's reported
    duration is how long the call actually ran — request out, last chunk in —
    rather than how long it happened to then sit unread in memory before the
    collector got to it; see :func:`_abandoned_duration`. ``None`` until the
    first chunk is observed.

    ``context`` is a snapshot of the caller's ``contextvars.Context`` — most
    importantly, which :func:`runbound.session` the call was made under —
    taken here, at construction time, rather than read later from whatever
    thread ends up finalizing this ledger. The collector runs on its own
    schedule, possibly on another thread or inside a caller-unrelated task
    (or another session's block entirely); running the abandoned report
    inside this snapshot rather than whatever happens to be current there is
    what keeps a partial call charged to the session that actually opened it.
    See :func:`_report_abandoned`.
    """

    def __init__(
        self,
        usage: _StreamUsage,
        hooks: Hooks,
        provider: str,
        started_at: float | None,
        request_chars: int,
        estimating: bool,
    ) -> None:
        self.usage = usage
        self.hooks = hooks
        self.provider = provider
        self.started_at = started_at
        self.last_at: float | None = None
        self.request_chars = request_chars
        self.estimating = estimating
        self.chars = 0
        self.chunks = 0
        self.usage_seen = False
        self.emitted = False
        self.failed = False
        self.released = False
        self.context = contextvars.copy_context()


def _abandoned_duration(ledger: "_StreamLedger") -> float:
    """How long an abandoned stream actually ran — never how long it waited.

    ``last_at`` minus ``started_at``: the moment the last chunk was observed
    minus the moment the request went out, never negative. ``0.0`` when the
    call was never timed (``started_at`` is ``None``) or no chunk was ever
    observed (``last_at`` is ``None`` — a stream abandoned before its first
    chunk). Deliberately never a reading taken when the finalizer happens to
    run: a stream that streamed one chunk instantly and then sat unread in
    memory for minutes took the time up to that chunk, not the time until
    someone noticed.
    """
    if ledger.started_at is None or ledger.last_at is None:
        return 0.0
    return max(ledger.last_at - ledger.started_at, 0.0)


def _abandoned_tokens(ledger: "_StreamLedger") -> tuple[int, int, bool]:
    """``(tokens_in, tokens_out, estimated)`` for a stream nobody finished.

    ``tokens_out`` is the provider's own count when any chunk carried usage at
    all, even a truthful zero — only a stream that never said anything about
    usage falls back to ``ceil(chars / 4)`` of the text it did stream, and
    ``estimated`` says which happened. ``tokens_in`` mirrors the non-streamed
    estimator: the provider's count when present, else the request's char
    count when this call was estimating, else 0 — a stream that never opted
    into estimation reports no guessed input tokens either.
    """
    usage = ledger.usage
    estimated = not ledger.usage_seen
    if ledger.usage_seen:
        tokens_out = usage.tokens_out
    else:
        tokens_out = estimated_tokens(ledger.chars) if ledger.chars else 0
    if usage.tokens_in:
        tokens_in = usage.tokens_in
    elif ledger.estimating:
        tokens_in = estimated_tokens(ledger.request_chars)
    else:
        tokens_in = 0
    return tokens_in, tokens_out, estimated


def _report_abandoned_call(ledger: "_StreamLedger") -> None:
    """The ``hooks.abandoned`` call itself, run inside the caller's context.

    Split out from :func:`_report_abandoned` so it can be handed to
    ``ledger.context.run`` — a ``contextvars.Context`` runs a plain callable,
    not an inline block. Runs once, when the stream is collected still
    holding an in-flight call: exhausted, closed or exited streams already set
    ``ledger.emitted`` or ``ledger.failed`` on their own way out, and this is a
    no-op for those.

    Reports ``hooks.abandoned`` — never ``success`` or ``error``, so the
    circuit learns nothing from a call nobody waited for.
    """
    if ledger.emitted or ledger.failed:
        return
    tokens_in, tokens_out, estimated = _abandoned_tokens(ledger)
    abandoned = getattr(ledger.hooks, "abandoned", None)
    if callable(abandoned):
        abandoned(
            ledger.usage.model,
            tokens_in,
            tokens_out,
            _abandoned_duration(ledger),
            ledger.provider,
            estimated,
        )


def _report_abandoned(ledger: "_StreamLedger") -> None:
    """The :func:`weakref.finalize` callback for a collected, unfinished stream.

    Takes only ``ledger`` — never the proxy it belonged to, which is the whole
    point: a callback that closed over ``self`` would keep every guarded
    stream alive forever.

    Runs :func:`_report_abandoned_call` inside ``ledger.context`` when there
    is one — a snapshot of the ``contextvars.Context`` the stream was opened
    under, taken back when the ledger was built — rather than directly: the
    collector runs this on its own schedule, often on a different thread or
    inside a different (or no) :func:`runbound.session` block than the one
    that made the call, and calling straight through would silently charge
    whatever session happens to be current *there* instead of the one that
    actually opened the stream. A ledger with no context (built before this
    existed, or by a caller that constructs one directly) falls back to
    calling straight through, exactly as before.

    Gives back the in-flight slot, exactly once however this stream's life
    ends, outside that context: which provider's counter it belongs to is
    decided by ``provider``, not by session. Nothing here ever raises: a
    finalizer's exception prints a warning to stderr and is otherwise
    ignored, which is worse than logging it ourselves.
    """
    try:
        context = ledger.context
        if context is not None:
            context.run(_report_abandoned_call, ledger)
        else:
            _report_abandoned_call(ledger)
    except Exception:
        _LOG.debug("runbound: could not report an abandoned stream", exc_info=True)
    finally:
        if not ledger.released:
            ledger.released = True
            release_slot(ledger.hooks, ledger.provider)


def _is_async_callable(fn: Any) -> bool:
    """True if calling ``fn`` produces an awaitable.

    Looks through ``functools.wraps``/``functools.partial`` layers and at the
    ``__call__`` of callable objects, because SDK methods are often decorated
    or are instances rather than plain functions. Anything that cannot be
    inspected counts as sync — a sync wrapper around an async method still
    returns the coroutine untouched, whereas the reverse would not work.
    """
    target = fn
    for _ in range(_MAX_UNWRAP):
        try:
            if asyncio.iscoroutinefunction(target):
                return True
            wrapped = getattr(target, "__wrapped__", None)
        except Exception:
            return False
        if wrapped is None:
            break
        target = wrapped
    try:
        return asyncio.iscoroutinefunction(getattr(type(fn), "__call__", None))
    except Exception:
        return False


def _guard_stream(
    response: Any,
    request_kwargs: dict,
    report: Report,
    chunk_usage: ChunkUsage,
    *,
    is_async: bool,
    started_at: float | None = None,
    hooks: Hooks = _NO_HOOKS,
    provider: str = "",
    chunk_requests: ChunkRequests | None = None,
    chunk_text: ChunkText | None = None,
    request_chars: int = 0,
) -> Any:
    """Wrap a streamed ``response`` in its guard, or return ``None``.

    ``None`` means "this is not a stream we can guard" — the request did not
    ask for one, or what came back is not iterable — and the caller should
    treat the response as an ordinary one.

    ``started_at`` is the stopwatch reading from just before the provider's
    call, so the reported duration spans the whole stream: request out, last
    chunk in.
    """
    try:
        if not request_kwargs.get("stream"):
            return None
        protocol = "__aiter__" if is_async else "__iter__"
        if not hasattr(response, protocol):
            return None
        guard = _AsyncGuardedStream if is_async else _GuardedStream
        return guard(
            response,
            chunk_usage,
            report,
            _request_model(request_kwargs),
            started_at,
            hooks,
            provider,
            chunk_requests=chunk_requests,
            chunk_text=chunk_text,
            request_chars=request_chars,
        )
    except Exception:
        _LOG.warning("runbound could not guard a streamed response; continuing", exc_info=True)
        return None


class _StreamGuard:
    """Bookkeeping shared by the sync and async streamed-response proxies.

    Holds the running usage, reports it at most once, and delegates every
    attribute it does not define to the provider's own stream object.

    ``started_at`` is a stopwatch reading from before the provider's call; the
    duration is measured when the stream ends, so it covers the time the caller
    actually spent waiting on tokens. ``None`` means the call was not timed and
    reports a 0.0 duration.

    A stream is also one provider call as far as the circuit is concerned:
    reaching the end reports ``hooks.success``, and an exception raised while
    iterating reports ``hooks.error`` once — see :meth:`_fail`. A stream that
    is instead simply dropped — never exhausted, closed or exited — is caught
    by :func:`weakref.finalize` on ``self``, registered here against a
    :class:`_StreamLedger` that holds everything :func:`_report_abandoned`
    needs and nothing that would keep this proxy alive: see that class.
    """

    def __init__(
        self,
        stream: Any,
        chunk_usage: ChunkUsage,
        report: Report,
        model: str | None,
        started_at: float | None = None,
        hooks: Hooks = _NO_HOOKS,
        provider: str = "",
        chunk_requests: ChunkRequests | None = None,
        chunk_text: ChunkText | None = None,
        request_chars: int = 0,
    ) -> None:
        self._stream = stream
        self._chunk_usage = chunk_usage
        self._chunk_requests = chunk_requests
        self._report = report
        self._usage = _StreamUsage(model=model)
        self._requests = _StreamRequests()
        self._started_at = started_at
        self._hooks = hooks
        self._provider = provider
        self._iterator: Any = None
        # Whether this call estimates is decided once, when the stream starts:
        # a stream is one call, and one call is counted one way throughout.
        # The text reader itself is kept regardless — an abandoned stream's
        # chars/4 fallback applies whether or not estimation was opted into.
        self._estimating = estimating(hooks)
        self._chunk_text = chunk_text
        self._ledger = _StreamLedger(
            self._usage, hooks, provider, started_at, request_chars, self._estimating
        )
        self._finalizer = weakref.finalize(self, _report_abandoned, self._ledger)
        # Not atexit: a process that exits with the stream still referenced
        # never reports it. The default (True) would run this during
        # interpreter teardown, when threads, logging handlers and the
        # network may already be gone — the wrong moment to call out.
        self._finalizer.atexit = False

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this proxy does not define; __dict__
        # rather than self._stream so a half-built proxy cannot recurse.
        return getattr(self.__dict__["_stream"], name)

    def _observe(self, chunk: Any) -> Any:
        """Fold ``chunk`` into the running usage and return it unchanged.

        Usage and requests are read independently: a chunk shape that defeats
        one of them still tells the other everything it can. Chunk, char and
        timing counts feed only :class:`_StreamLedger` — an abandoned
        stream's own report — and are kept unconditionally, unlike the opt-in
        estimator :meth:`_estimate` applies on the ordinary path.

        Usage is read through a :class:`_UsageParseTracker` rather than
        ``self._usage`` directly, so ``ledger.usage_seen`` reflects whether
        the reader actually parsed a usage block — even a truthful all-zero
        one — rather than whether the running totals happen to be nonzero,
        which a genuine zero could never be told apart from "said nothing".
        """
        try:
            tracker = _UsageParseTracker(self._usage)
            self._chunk_usage(chunk, tracker)
            if tracker.parsed:
                self._ledger.usage_seen = True
        except Exception:
            _LOG.debug("runbound: unreadable usage on a stream chunk", exc_info=True)
        if self._chunk_requests is not None:
            try:
                self._chunk_requests(chunk, self._requests)
            except Exception:
                _LOG.debug(
                    "runbound: unreadable tool calls on a stream chunk", exc_info=True
                )
        self._ledger.chunks += 1
        self._ledger.last_at = _now()
        if self._chunk_text is not None:
            try:
                text = self._chunk_text(chunk)
                if isinstance(text, str):
                    self._ledger.chars += len(text)
            except Exception:
                _LOG.debug("runbound: unreadable text on a stream chunk", exc_info=True)
        return chunk

    def _estimate(self) -> None:
        """Fill in chars/4 token counts for a stream that carried no usage.

        Only ever called when the estimator is on and the stream reported
        nothing at all: a provider that sends usage is always believed.
        """
        if not self._estimating:
            return
        if self._usage.tokens_in or self._usage.tokens_out or self._usage.tokens_reasoning:
            return
        self._usage.tokens_in = estimated_tokens(self._ledger.request_chars)
        self._usage.tokens_out = estimated_tokens(self._ledger.chars)
        if self._usage.tokens_in or self._usage.tokens_out:
            warn_estimated_tokens(self._usage.model)

    def _release(self) -> None:
        """Give this call's in-flight slot back, exactly once per stream.

        A stream that dies mid-flight and is then closed reaches both
        :meth:`_fail` and :meth:`_emit`; the slot was taken once and is given
        back once — tracked on :attr:`_ledger`, not on this proxy, so a stream
        that is instead abandoned and collected still gives its slot back
        exactly once, from :func:`_report_abandoned`.
        """
        if self._ledger.released:
            return
        self._ledger.released = True
        release_slot(self._hooks, self._provider)

    def _emit(self, suppress_trip: bool = False) -> None:
        """Report the accumulated usage and the elapsed time, once per stream.

        A stream that ended without a single token warns first (see
        :func:`_warn_zero_token_stream`) — that happens before the report, so
        a limit tripping on this step cannot swallow the warning. Then, in
        order: the usage report, ``hooks.success``, and the tool calls the
        model asked for over the course of the stream — the same order the
        non-streamed path uses, and for the same reason: the call really did
        succeed, whatever the requests it carried turn out to mean.

        Only :class:`GuardrailTripped` escapes; it reaches whoever was
        consuming the stream, which is the point. ``suppress_trip`` says the
        host is already unwinding on an exception of its own, and that one is
        the one it needs: the trip is logged instead of raised, because
        replacing the application's real error with ours would hide it. The
        call is recorded either way, so the session still knows it happened.

        Setting ``_ledger.emitted`` here is also what tells a later
        :func:`_report_abandoned` — should this proxy be collected after all —
        that this stream already reported the ordinary way and needs no
        report of its own.
        """
        if self._ledger.emitted:
            return
        self._ledger.emitted = True
        self._release()
        self._estimate()
        if not (self._usage.tokens_in or self._usage.tokens_out or self._usage.tokens_reasoning):
            _warn_zero_token_stream()
        try:
            _call_report(
                self._report,
                self._usage.model,
                self._usage.tokens_in,
                self._usage.tokens_out,
                _elapsed(self._started_at),
                self._usage.tokens_reasoning,
                self._usage.tokens_cached_in,
                self._usage.tokens_cache_write_in,
            )
            if not self._ledger.failed:
                self._hooks.success(self._provider)
                emit_tool_requests(self._hooks, self._requests.collected())
        except GuardrailTripped:
            if not suppress_trip:
                raise
            _LOG.warning(
                "runbound: session tripped while a stream was closing on another "
                "error; keeping the original exception",
                exc_info=True,
            )
        except Exception:
            _LOG.warning("runbound failed to record a streamed call; continuing", exc_info=True)

    def _fail(self, exc: BaseException) -> None:
        """Report a stream that died mid-flight, once, and never raise.

        A provider that drops a stream halfway is failing exactly as one that
        refuses the request is, and the circuit counts it the same way. Unlike
        the non-streamed path, a trip discovered here is logged rather than
        raised: the caller is already unwinding on the provider's own
        exception, and swapping ours in would hide what actually broke.

        Setting ``_ledger.failed`` is what keeps a later
        :func:`_report_abandoned` from also reporting this stream as an
        abandoned one: it already reported, as an error.
        """
        if self._ledger.failed:
            return
        self._ledger.failed = True
        self._release()
        try:
            self._hooks.error(
                self._usage.model, exc, _elapsed(self._started_at), self._provider
            )
        except GuardrailTripped:
            _LOG.warning(
                "runbound: session tripped while a stream was failing; keeping the "
                "original exception",
                exc_info=True,
            )
        except Exception:
            _LOG.warning("runbound failed to record a broken stream; continuing", exc_info=True)


class _GuardedStream(_StreamGuard):
    """A synchronous streamed response, guarded.

    Chunks are yielded exactly as the provider produced them, in order, and
    exactly one ``llm_call`` is reported when the stream ends — by exhaustion,
    by :meth:`close`, or by leaving a ``with`` block. Usage is read off the
    chunks best-effort; a stream that never carries any reports a zero-token
    step, which still counts as one step.

    A stream that is instead abandoned — never exhausted, never closed —
    counts too: once nothing else refers to it, ``weakref.finalize`` reports
    it as a partial call, via ``hooks.abandoned`` rather than ``hooks.success``
    or ``hooks.error``, so the circuit learns nothing from it. See
    :class:`_StreamGuard` and :func:`_report_abandoned`. This still relies on
    the stream actually being collected — a reference cycle that survives
    until the next full ``gc`` pass delays the report, and a process that
    exits first never runs it at all.
    """

    def __iter__(self) -> "_GuardedStream":
        return self

    def __next__(self) -> Any:
        if self._iterator is None:
            self._iterator = iter(self._stream)
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._emit()
            raise
        except Exception as exc:
            self._fail(exc)
            raise
        return self._observe(chunk)

    def close(self) -> None:
        """Close the underlying stream if it can be, and report once."""
        try:
            closer = getattr(self._stream, "close", None)
            if callable(closer):
                closer()
        finally:
            self._emit()

    def __enter__(self) -> "_GuardedStream":
        enter = getattr(self._stream, "__enter__", None)
        if enter is None:
            raise TypeError(f"{type(self._stream).__name__!r} is not a context manager")
        enter()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            exit_ = getattr(self._stream, "__exit__", None)
            suppress = exit_(*exc_info) if exit_ is not None else False
        finally:
            self._emit(suppress_trip=_failing(exc_info))
        return suppress


class _AsyncGuardedStream(_StreamGuard):
    """An asynchronous streamed response, guarded.

    Identical contract to :class:`_GuardedStream` over ``async for``:
    untouched chunks, one report at exhaustion, :meth:`aclose` or
    ``async with`` exit, and — same as the sync proxy — a report from
    ``weakref.finalize`` if it is instead abandoned and collected.

    Exhaustion is also where a pending throttle delay the engine stashed for
    this task is paid: :func:`_await_pending_delay` runs right after
    :meth:`_emit`, before ``StopAsyncIteration`` propagates.
    """

    def __aiter__(self) -> "_AsyncGuardedStream":
        return self

    async def __anext__(self) -> Any:
        if self._iterator is None:
            self._iterator = self._stream.__aiter__()
        try:
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._emit()
            await _await_pending_delay(self._hooks)
            raise
        except Exception as exc:
            self._fail(exc)
            raise
        return self._observe(chunk)

    async def aclose(self) -> None:
        """Close the underlying stream if it can be, and report once."""
        try:
            closer = getattr(self._stream, "aclose", None) or getattr(self._stream, "close", None)
            if callable(closer):
                closed = closer()
                if inspect.isawaitable(closed):
                    await closed
        finally:
            self._emit()

    async def __aenter__(self) -> "_AsyncGuardedStream":
        enter = getattr(self._stream, "__aenter__", None)
        if enter is None:
            raise TypeError(f"{type(self._stream).__name__!r} is not an async context manager")
        await enter()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            exit_ = getattr(self._stream, "__aexit__", None)
            suppress = await exit_(*exc_info) if exit_ is not None else False
        finally:
            self._emit(suppress_trip=_failing(exc_info))
        return suppress


# Imported last: the provider modules read the shared machinery above off this
# package while it is still being initialized.
from . import anthropic_wrapper, openai_wrapper  # noqa: E402

#: Tried in order; the first module that `matches()` a client wraps it.
PROVIDERS = (openai_wrapper, anthropic_wrapper)

__all__ = [
    "PROVIDERS",
    "anthropic_wrapper",
    "emit_tool_requests",
    "estimated_tokens",
    "normalize_arguments",
    "openai_wrapper",
    "provider_label",
    "request_hash",
]
