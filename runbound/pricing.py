"""Model price table and cost estimation.

Leaf module: stdlib only, no internal imports. `estimate_cost` runs on every
wrapped LLM call, so it is total (never raises, for any input) and does no
I/O. It logs nothing above DEBUG except one WARNING per unknown model, and
only when the caller asks for it (`warn_unpriced`): a model priced at $0.00 is
a model a dollar budget cannot see, which is worth saying once.

Prices are USD per 1M tokens as ``(input, output)``, or with one or both of
two optional extra columns where the provider publishes them: a cached-input
*read* rate (``(input, output, cached_input)``) and, additionally, a
cache-*write* rate (``(input, output, cached_input, cache_write_input)``) —
see `PRICES`. They are published list prices that drift over time; callers
who need exact numbers pass `custom_prices` (also the mechanism for
self-hosted / OpenAI-compatible endpoints). `PRICES_AS_OF` (see `as_of`)
says how old this table is; a price that changed after that date is wrong
until the table is updated, which is exactly what `custom_prices` is for.

`price_for` answers "is this model priced at all", and `price_call` wraps
`estimate_cost` with the `on_unpriced_model` policy ("zero" / "estimate" /
"refuse") for callers that need to know not just the cost but whether it was
a real price or a configured fallback.

**Cached input tokens.** Both OpenAI and Anthropic bill some of a
call's prompt differently when it was served from a cache. A **cache read**
is a discount: every reader in this package treats a cached-read token as
still counted inside `tokens_in` — never a separate total — with
`tokens_cached_in` naming how many of those input tokens were the discounted
kind, priced at a model's published `cached_in` rate where the table has one
(`PRICES[model][2]`) and at the plain input rate otherwise: an unknown
discount is never invented, so an unpriced-for-caching model is simply
charged as if none of its tokens were cached, which over-charges slightly
rather than under-charging silently.

Anthropic also bills a **cache write** (`cache_creation_input_tokens`) at a
125% premium over its input rate — a cost, not a discount, and the one token
class in this whole change that must never be under-priced: under-counting a
*read* discount trips a customer's wall early, which is annoying but safe
(they are stopped before their money is gone); under-counting a *write*
premium does the opposite — the wall fires late, after they have spent past
a budget they set, which is the one direction this product cannot afford.
So it gets its own optional column (`PRICES[model][3]`), read from
`cache_creation_input_tokens`, priced at that rate where the table has one
and at the plain input rate otherwise — the same "never invent, but never
under-price by omission either" fallback the read column already uses,
because the fallback price here is the *honest* one (the plain input rate is
still less than the true 125%, but far closer to it than treating a write as
a 90%-discounted read would be, and it is what "unknown" must mean absent a
published number).

**One-hour cache writes.** Anthropic bills a cache write that asked for the
one-hour lifetime at 2x the input rate, not the five-minute 1.25x. The usage
object splits the two (``cache_creation.ephemeral_5m_input_tokens`` /
``ephemeral_1h_input_tokens``). ``tokens_cache_write_in`` stays the total of
both; ``tokens_cache_write_1h_in`` is the one-hour part of it, priced at the
fifth column of a row (``PRICES[model][4]``), else at the row's five-minute
write rate. A price tuple of two to five numbers is accepted everywhere.

**What a request can change about its price.** Two published multipliers
depend on the request, not the model: ``inference_geo="us"`` (1.1x on every
category, from Claude 4.6 on) and ``speed="fast"`` (2x, on Claude Opus 5.5,
Opus 5 and Opus 4.8). They are applied only when the request itself carries
the field (:func:`request_multiplier`); a plan, an account setting or a header
the SDK cannot see is a documented limit (docs/concepts/what-it-sees.md), and
the estimate is then the base rate. The batch API's 50% discount is a separate
endpoint the SDK does not wrap, and server tools (web search per 1,000
requests, code execution per container-hour) are not priced here.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

_log = logging.getLogger("runbound")

#: Unknown models already warned about (under on_unpriced_model="zero"), so
#: each one is named at most once per process.
_WARNED_MODELS: set[str] = set()

#: Unknown models already warned about because on_unpriced_model="refuse"
#: recorded them anyway (record_call(), or a model unknown before the
#: request) instead of refusing them at the door. A separate set from
#: _WARNED_MODELS: the two modes say different things and a customer running
#: "refuse" should hear the "refuse" message, not the "zero" one, for the
#: same model.
_REFUSE_RECORDED_MODELS: set[str] = set()

#: A prefix only names a model when the rest starts a new segment: "gpt-4o"
#: prices "gpt-4o-2024-11-20" but not "gpt-4oh-no".
_BOUNDARY_CHARS = "-.:"

#: Segments that start a different model family, whatever they follow. These
#: variants are priced separately by every provider, so inheriting the base
#: model's price would not be an estimate, it would be a wrong number.
_FAMILY_SUFFIXES = ("-pro", "-audio", "-realtime", "-search", "-transcribe", "-tts")

#: The date this table (including cached-input rates) was last checked
#: against published list prices. See `as_of`.
PRICES_AS_OF = "2026-10-03"

#: Where the Anthropic rows below were read, on `PRICES_AS_OF`.
ANTHROPIC_PRICES_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
ANTHROPIC_MODELS_SOURCE = "https://platform.claude.com/docs/en/models/overview"

#: model name -> (usd_per_1M_input_tokens, usd_per_1M_output_tokens), or with
#: a published cached-input *read* rate appended (..., usd_per_1M_cached_input),
#: or, for a provider that also publishes a cache-*write* premium, a fourth
#: element (..., usd_per_1M_cache_write_input). Dated releases (e.g.
#: "gpt-4o-2024-11-20") resolve by longest-prefix match. List prices as of
#: `PRICES_AS_OF`; they drift, so pass `custom_prices` for billing, and see
#: `as_of()` — a price that changed after that date is wrong until this table
#: is updated.
#:
#: The cached-read rate is only added where a model actually publishes one:
#: OpenAI discounts a cache hit to 50% of its input rate on every model
#: released after prompt caching shipped (everything here except
#: `gpt-4-turbo` and `gpt-3.5-turbo`, both older); Anthropic discounts a
#: cache hit to 10% of its input rate on every Claude 3-and-later model, all
#: of which are listed below. Anthropic additionally publishes a cache-*write*
#: premium of 125% of its input rate, the fourth element on every Anthropic
#: entry — OpenAI has no cache-write concept, so no OpenAI entry carries one.
#: A model missing the third or fourth element prices that kind of token at
#: the full input rate instead — an honest over-charge for a missing
#: discount, and the closest honest number for a missing premium, but never a
#: guessed rate either way.
PRICES: dict[
    str,
    tuple[float, float]
    | tuple[float, float, float]
    | tuple[float, float, float, float]
    | tuple[float, float, float, float, float],
] = {
    # --- OpenAI ---
    "gpt-4o": (2.50, 10.00, 1.25),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4.1": (2.00, 8.00, 1.00),
    "gpt-4.1-mini": (0.40, 1.60, 0.20),
    "gpt-4.1-nano": (0.10, 0.40, 0.05),
    "gpt-4-turbo": (10.00, 30.00),  # predates prompt caching: no cached rate
    "gpt-3.5-turbo": (0.50, 1.50),  # predates prompt caching: no cached rate
    "gpt-5": (1.25, 10.00, 0.625),
    "gpt-5-mini": (0.25, 2.00, 0.125),
    "gpt-5-nano": (0.05, 0.40, 0.025),
    "o3": (2.00, 8.00, 1.00),
    "o3-mini": (1.10, 4.40, 0.55),
    "o4-mini": (1.10, 4.40, 0.55),
    # --- Anthropic --- (in, out, cache read, cache write 5 minutes, cache write 1 hour).
    # A 5-minute write is 1.25x the input rate and a 1-hour write 2x, on every model; a read is
    # 0.1x except Opus 5.5 (0.05x) and Fable 5.1 / Mythos 5.1 (0.025x). Read on PRICES_AS_OF from
    # ANTHROPIC_PRICES_SOURCE; model ids from ANTHROPIC_MODELS_SOURCE (the page spells out
    # claude-fable-5-1, claude-opus-5-5, claude-sonnet-5-5 and claude-haiku-4-5; the other ids follow
    # its dateless-from-4.6 rule and are not each spelled out there).
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-fable-5": (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-mythos-5-1": (10.00, 50.00, 0.25, 12.50, 20.00),  # invitation-only
    "claude-mythos-5": (10.00, 50.00, 1.00, 12.50, 20.00),  # invitation-only
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00, 8.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-7": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-6": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5": (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-1": (15.00, 75.00, 1.50, 18.75, 30.00),  # retired from the API
    "claude-opus-4": (15.00, 75.00, 1.50, 18.75, 30.00),  # retired from the API
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50, 4.00),  # the introductory $2 / $10, now permanent
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25, 2.00),
    # Older rows, unchanged and not re-verified on PRICES_AS_OF (their 1-hour column follows the 2x rule).
    "claude-3-7-sonnet": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-3-5-sonnet": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-3-5-haiku": (0.80, 4.00, 0.08, 1.00, 1.60),
    "claude-3-opus": (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-3-haiku": (0.25, 1.25, 0.025, 0.3125, 0.50),
}


def as_of() -> str:
    """The date `PRICES` (including its cached-input rates) was last checked.

    A price that changed after this date is wrong until the table is
    updated; pass `custom_prices` for numbers you need to be exact right now.
    """
    return PRICES_AS_OF


_Price = (
    tuple[float, float]
    | tuple[float, float, float]
    | tuple[float, float, float, float]
    | tuple[float, float, float, float, float]
)


def price_for(
    model: str | None, custom_prices: dict[str, _Price] | None = None
) -> _Price | None:
    """The ``(usd_per_1M_input, usd_per_1M_output[, usd_per_1M_cached_input
    [, usd_per_1M_cache_write_input]])`` price for ``model``, if any.

    The third element is present only where the provider publishes a
    cached-*read* rate, and the fourth only where it additionally publishes a
    cache-*write* premium (see `PRICES`) — callers that price cached tokens
    (currently just :func:`estimate_cost`/:func:`price_call`) fall back to
    the plain input rate for whichever of the two is absent.

    The same resolution :func:`estimate_cost` prices with: ``custom_prices``
    is consulted first and wins, then the built-in table, each by exact match
    then longest boundary-respecting prefix (the built-in table additionally
    rejects a prefix followed by another family's name). Returns ``None`` for
    a ``None``/non-string/empty model or one neither table prices — the
    caller's definition of "unpriced". Never raises.
    """
    try:
        if not isinstance(model, str) or not model:
            return None
        price = None
        if custom_prices:
            price = _match(model, custom_prices)
        if price is None:
            price = _match(model, PRICES, exclude_families=True)
        return price
    except Exception:
        _log.debug("price_for failed for model %r", model, exc_info=True)
        return None


def _match(
    model: str, table: dict, *, exclude_families: bool = False
) -> _Price | None:
    """Return the price for `model` in `table`: exact key, else longest prefix.

    A prefix only counts when what follows it starts a new segment, so
    "gpt-4o" prices "gpt-4o-2024-11-20" and not "gpt-4oh-no". With
    `exclude_families`, the longest such prefix is rejected outright when the
    rest names another family ("o3-pro" is not "o3"), rather than falling back
    to a shorter prefix that would be just as wrong.
    """
    price = table.get(model)
    if price is not None:
        return price
    best_key = ""
    best_price = None
    for key, value in table.items():
        if not isinstance(key, str) or len(key) <= len(best_key):
            continue
        if not model.startswith(key):
            continue
        rest = model[len(key):]
        if not rest or (rest[0] not in _BOUNDARY_CHARS and not rest[0].isdigit()):
            continue
        best_key, best_price = key, value
    if best_price is None:
        return None
    if exclude_families and model[len(best_key):].startswith(_FAMILY_SUFFIXES):
        return None
    return best_price


def _warn_unpriced(model: str) -> None:
    """Say once per process that `model` is being counted as free."""
    if model in _WARNED_MODELS:
        return
    _WARNED_MODELS.add(model)
    _log.warning(
        "runbound: no price known for model %r; its cost is counted as $0.00 and "
        "budget_usd cannot see it. Pass custom_prices to fix.",
        model,
    )


def estimate_cost(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    custom_prices: dict[str, _Price] | None = None,
    *,
    tokens_cached_in: int = 0,
    tokens_cache_write_in: int = 0,
    warn_unpriced: bool = False,
    tokens_cache_write_1h_in: int = 0,
    multiplier: float = 1.0,
) -> float:
    """Estimate USD cost of one call.

    `custom_prices` is consulted first and wins over the built-in table.
    Within each table, an exact model match wins, then the longest matching
    prefix (so "gpt-4o-mini-2024-07-18" prices as "gpt-4o-mini", not "gpt-4o").
    A prefix must end on a segment boundary, and in the built-in table it must
    not be followed by another family's name — "o3-pro" is unpriced, not an
    "o3". The custom table is the caller's own and gets no such second-guessing.

    `tokens_cached_in` and `tokens_cache_write_in` are both subsets of
    `tokens_in` — never additional tokens, always a slice of the total
    already passed in, and never overlapping each other. `tokens_cached_in`
    is a cache *read*, priced at the model's published cached-input rate
    (`PRICES[model][2]`, or the matching entry in `custom_prices`) when there
    is one, else the plain input rate: an unknown discount is never invented.
    `tokens_cache_write_in` is a cache *write* — a premium, not a discount —
    priced at `PRICES[model][3]` when published, else also the plain input
    rate: the closest honest number to an unpublished premium, since it is
    still cheaper than the true cost but never mistaken for the read
    discount. Both counts clamp to `tokens_in` (and to each other, so their
    sum never exceeds it) and to zero.

    `warn_unpriced` logs one WARNING per distinct unpriced model per process;
    callers pass it when a dollar limit makes a $0.00 estimate a blind spot
    rather than a detail. It never changes the number returned.

    Returns 0.0 for a None, non-string, or unpriced model. Negative token
    counts clamp to 0. Never raises: any unexpected input yields 0.0.
    """
    try:
        if not isinstance(model, str) or not model:
            return 0.0
        price = None
        if custom_prices:
            price = _match(model, custom_prices)
        if price is None:
            price = _match(model, PRICES, exclude_families=True)
        if price is None:
            if warn_unpriced:
                _warn_unpriced(model)
            return 0.0
        return _cost_from_pair(
            price, tokens_in, tokens_out, tokens_cached_in, tokens_cache_write_in,
            tokens_cache_write_1h_in, multiplier,
        )
    except Exception:  # fail-open: pricing must never break the host call
        _log.debug("estimate_cost failed for model %r", model, exc_info=True)
        return 0.0


def _cost_from_pair(
    pair: _Price,
    tokens_in: int,
    tokens_out: int,
    tokens_cached_in: int = 0,
    tokens_cache_write_in: int = 0,
    tokens_cache_write_1h_in: int = 0,
    multiplier: float = 1.0,
) -> float:
    """``(tokens_in, tokens_out)`` priced at a price tuple, cache reads and
    writes split out.

    ``pair`` is ``(usd_per_1M_in, usd_per_1M_out)``, optionally extended with
    a published cache-read rate and, further, a cache-write rate:
    ``(usd_per_1M_in, usd_per_1M_out, usd_per_1M_cached_in,
    usd_per_1M_cache_write_in)``. ``tokens_cached_in`` and
    ``tokens_cache_write_in`` are both slices of ``tokens_in``: each clamped
    to non-negative, then together clamped so their sum never exceeds
    ``tokens_in`` (a cache write is taken first, since it is read here as the
    smaller, less-likely-to-be-wrong count in practice, then a read absorbs
    whatever remains) — a malformed report cannot make regular tokens go
    negative. Each is priced at its own column when present, else at the
    same rate as the rest of ``tokens_in`` — the "no invented rate" rule,
    for a discount and a premium alike.

    ``tokens_cache_write_1h_in`` is the one-hour part of ``tokens_cache_write_in``
    (clamped to it), priced at the fifth column when the tuple has one, else at
    the five-minute write rate. ``multiplier`` scales the whole cost (a request's
    ``inference_geo`` / ``speed``, see :func:`request_multiplier`); anything that is
    not a positive finite number counts as 1.0.
    """
    price_in, price_out = pair[0], pair[1]
    tokens_in = max(tokens_in, 0)
    write = min(max(tokens_cache_write_in, 0), tokens_in)
    write_1h = min(max(tokens_cache_write_1h_in, 0), write)
    write_5m = write - write_1h
    cached = min(max(tokens_cached_in, 0), tokens_in - write)
    regular = tokens_in - cached - write
    price_cached_in = pair[2] if len(pair) > 2 else price_in
    price_write_in = pair[3] if len(pair) > 3 else price_in
    price_write_1h_in = pair[4] if len(pair) > 4 else price_write_in
    cost_in = (
        (regular / 1e6) * price_in
        + (cached / 1e6) * price_cached_in
        + (write_5m / 1e6) * price_write_in
        + (write_1h / 1e6) * price_write_1h_in
    )
    cost_out = (max(tokens_out, 0) / 1e6) * price_out
    return float((cost_in + cost_out) * _factor(multiplier))


def _factor(value: Any) -> float:
    """``value`` when it is a positive finite number, else 1.0 (a multiplier is never a reason to fail)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return number if number > 0.0 and number != float("inf") else 1.0


def _warn_refuse_recorded(model: str, estimated: bool) -> None:
    """Say once per process that "refuse" recorded, rather than stopped, a call.

    ``on_unpriced_model="refuse"`` is meant to stop a call at the door, before
    it goes out; a call that reaches accounting anyway — inference reported
    for a run the SDK never wrapped, or a model only known after the response
    — cannot be un-made, so it is priced and counted instead of refused.
    Worth saying once: a customer who configured "refuse" expecting every
    unpriced call to stop should learn that this one did not.
    """
    if model in _REFUSE_RECORDED_MODELS:
        return
    _REFUSE_RECORDED_MODELS.add(model)
    _log.warning(
        "runbound: model %r is unpriced and on_unpriced_model=\"refuse\", but this "
        "call could not be stopped at the door (no model was known before the "
        "request, or it was reported via record_call()) and was recorded instead, "
        "priced %s.",
        model,
        "from unpriced_price_per_1m_usd" if estimated else "as $0.00",
    )


def price_call(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    custom_prices: dict[str, _Price] | None = None,
    *,
    tokens_cached_in: int = 0,
    tokens_cache_write_in: int = 0,
    on_unpriced_model: str = "zero",
    unpriced_price_per_1m_usd: tuple[float, float] | None = None,
    tokens_cache_write_1h_in: int = 0,
    multiplier: float = 1.0,
) -> tuple[float, bool]:
    """Price one call under ``on_unpriced_model``, as ``(cost_usd, estimated)``.

    A model that *is* priced (``custom_prices`` or the built-in table) is
    priced exactly like :func:`estimate_cost`, cache reads and writes
    and all, and ``estimated`` is ``False``. An unpriced model is
    priced by ``on_unpriced_model``:

    - ``"zero"`` (default): ``0.0``, warned once per model per process — the
      same warning :func:`estimate_cost` gives with ``warn_unpriced=True``.
    - ``"estimate"``: priced from ``unpriced_price_per_1m_usd``,
      ``estimated=True``. With no such pair configured (config requires one
      for this mode, but this function stays defensive), falls back to the
      "zero" behavior above. That fallback pair never carries a cached rate,
      so ``tokens_cached_in``/``tokens_cache_write_in`` price at the same
      rate as the rest here too.
    - ``"refuse"``: the door is meant to stop this call before it is ever
      priced; a call that still reaches here anyway is priced like
      ``"estimate"`` when a fallback pair is configured, else like ``"zero"``,
      under its own once-per-model warning so it reads as "recorded instead
      of refused", not as an ordinary unpriced call.

    Returns ``(0.0, False)`` for a ``None``/non-string/empty model — nothing
    to price, nothing to warn about. Never raises.
    """
    try:
        if not isinstance(model, str) or not model:
            return 0.0, False
        price = price_for(model, custom_prices)
        if price is not None:
            return (
                _cost_from_pair(
                    price, tokens_in, tokens_out, tokens_cached_in, tokens_cache_write_in,
                    tokens_cache_write_1h_in, multiplier,
                ),
                False,
            )
        if on_unpriced_model == "estimate" and unpriced_price_per_1m_usd:
            return (
                _cost_from_pair(
                    unpriced_price_per_1m_usd,
                    tokens_in,
                    tokens_out,
                    tokens_cached_in,
                    tokens_cache_write_in,
                ),
                True,
            )
        if on_unpriced_model == "refuse":
            estimated = bool(unpriced_price_per_1m_usd)
            _warn_refuse_recorded(model, estimated)
            if estimated:
                return (
                    _cost_from_pair(
                        unpriced_price_per_1m_usd,
                        tokens_in,
                        tokens_out,
                        tokens_cached_in,
                        tokens_cache_write_in,
                    ),
                    True,
                )
            return 0.0, False
        _warn_unpriced(model)
        return 0.0, False
    except Exception:  # fail-open: pricing must never break the host call
        _log.debug("price_call failed for model %r", model, exc_info=True)
        return 0.0, False


# --- what only the model and the request know ---------------------------------

#: Claude 4.7 and later count about 30% more tokens for the same text, so the characters-over-four
#: input estimate runs low for them. Prefixes, matched like a price row (a segment boundary follows).
_NEW_TOKENIZER = (
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5",
    "claude-fable-5", "claude-mythos-5",
)
#: `inference_geo="us"` multiplies every category by 1.1 on Claude 4.6 and later.
_US_ONLY = (
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-6",
    "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
)
#: `speed="fast"` multiplies by 2 on these, and stacks with the US-only 1.1.
_FAST_MODE = ("claude-opus-4-8", "claude-opus-5")

TOKENIZER_FACTOR = 1.3
US_ONLY_MULTIPLIER = 1.1
FAST_MODE_MULTIPLIER = 2.0


def _is_one_of(model: Any, prefixes: tuple[str, ...]) -> bool:
    if not isinstance(model, str) or not model:
        return False
    for key in prefixes:
        if model == key:
            return True
        if model.startswith(key):
            rest = model[len(key):]
            if rest[0] in _BOUNDARY_CHARS or rest[0].isdigit():
                return True
    return False


def token_estimate_factor(model: str | None) -> float:
    """1.3 for the models whose tokenizer counts about 30% more tokens than characters-over-four
    suggests (Claude 4.7 and later), else 1.0. Admission scales its input-token estimate by it."""
    return TOKENIZER_FACTOR if _is_one_of(model, _NEW_TOKENIZER) else 1.0


def request_multiplier(model: str | None, request_kwargs: Any) -> float:
    """The price multiplier a REQUEST carries, else 1.0: 1.1 for ``inference_geo="us"``, 2.0 for
    ``speed="fast"``, 2.2 for both, each only on the models the provider publishes it for.

    Read from the request's own fields (top level, or ``extra_body``). Nothing the SDK cannot see
    counts: an organisation-wide residency setting, a plan or a beta header alone is not a signal, and
    the estimate is then the base rate (docs/concepts/what-it-sees.md). Never raises."""
    try:
        if not isinstance(request_kwargs, dict):
            return 1.0
        extra = request_kwargs.get("extra_body")
        sources = (request_kwargs, extra if isinstance(extra, dict) else {})
        def field(name: str) -> Any:
            for source in sources:
                if source.get(name) is not None:
                    return source[name]
            return None
        multiplier = 1.0
        if field("inference_geo") == "us" and _is_one_of(model, _US_ONLY):
            multiplier *= US_ONLY_MULTIPLIER
        if field("speed") == "fast" and _is_one_of(model, _FAST_MODE):
            multiplier *= FAST_MODE_MULTIPLIER
        return round(multiplier, 10)
    except Exception:
        return 1.0


# --- what a request will cost before it goes ---------------------------------
#
# The worst case of one call, before it is made: the input at the plain input
# rate plus the output it may produce. The input is estimated from the
# characters of everything the provider bills as input, at four characters a
# token. The rule the reader follows is that the estimate never undercounts a
# field the provider bills for; when a field's cost cannot be known (an image,
# a tokenizer) it is left out rather than guessed, and when its shape is not
# recognised it is counted by its serialised size instead of being skipped.

#: Characters an estimated token stands for.
CHARS_PER_TOKEN = 4


def estimated_tokens(chars: int) -> int:
    """``ceil(chars / 4)``, the estimate's whole model of a tokenizer."""
    try:
        return -(-max(int(chars), 0) // CHARS_PER_TOKEN)
    except (TypeError, ValueError):
        return 0


def _scaled(tokens: int, factor: Any) -> int:
    """``tokens`` times ``factor``, rounded up (an estimate never rounds down)."""
    return math.ceil(round(tokens * _factor(factor), 6))


def admission_worst_case(
    price: tuple, output_tokens: int, input_chars: int, token_factor: float = 1.0, multiplier: float = 1.0
) -> float:
    """The dollar worst case of one call: input estimate plus output.

    ``input_chars`` is the request's input in characters (:func:`request_chars`),
    priced at ``price[0]``, the plain input rate: admission cannot know before
    the call how much of its input a cache will serve, so it assumes none (a
    3- or 4-tuple's cache columns are not used here). ``output_tokens`` is what
    the call may produce, priced at ``price[1]``. ``token_factor`` scales the input
    estimate for a tokenizer that counts more tokens than characters over four
    (:func:`token_estimate_factor`), and ``multiplier`` is the request's own price
    multiplier (:func:`request_multiplier`). This is the one formula: the
    engine's budget admission and anything that estimates the same way (a
    gateway, say) compose it from :func:`price_for`, the request's own output
    cap, and this.
    """
    input_tokens = _scaled(estimated_tokens(input_chars), token_factor)
    return float(
        ((input_tokens / 1_000_000.0) * price[0] + (max(int(output_tokens), 0) / 1_000_000.0) * price[1])
        * _factor(multiplier)
    )


def admission_worst_case_tokens(output_tokens: int, input_chars: int, token_factor: float = 1.0) -> int:
    """The token worst case of one call: input estimate plus output.

    The tokens counterpart of :func:`admission_worst_case`, and the same two
    terms: the input is ``estimated_tokens(input_chars)`` (characters divided by
    four, rounded up) and the output is what the call may produce. No price
    enters into it, so a model with no price has a token worst case all the same.
    """
    return _scaled(estimated_tokens(input_chars), token_factor) + max(int(output_tokens), 0)


def _get(obj: Any, name: str) -> Any:
    """``name`` off a mapping or an attribute-style object; ``None`` if absent or unreadable."""
    try:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)
    except Exception:
        return None


def _serialised(value: Any) -> int:
    """The size of a structure as compact JSON, for a field billed by its whole
    text (a tool definition's schema, a tool call's arguments)."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False))
    except Exception:
        return len(str(value))


def _text_of(value: Any) -> int:
    """Characters of a ``system``/``instructions``/content value: a string, or a
    list of parts (strings, or blocks with a ``text``), nested where the
    provider nests them. Anything else with no text says nothing about size."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(_part_chars(part) for part in value)
    return 0


def _part_chars(part: Any) -> int:
    """One content part, or one Responses input item.

    Text is counted as text; what a tool was asked to do (``arguments``,
    ``input``) and what it answered (``content``, ``output``) are billed as
    input on the next turn and are counted by their serialised size.
    """
    if isinstance(part, str):
        return len(part)
    total = 0
    text = _get(part, "text")
    if isinstance(text, str):
        total += len(text)
    for name in ("arguments", "output"):  # a function call item, and its output
        value = _get(part, name)
        if value is not None:
            total += _serialised(value)
    tool_input = _get(part, "input")  # an Anthropic tool_use block
    if isinstance(tool_input, (dict, list, tuple)):
        total += _serialised(tool_input)
    content = _get(part, "content")  # a tool_result block, or a Responses item
    if content is not None:
        total += _text_of(content)
    return total


def _message_chars(message: Any) -> int:
    total = _text_of(_get(message, "content"))
    for call in _get(message, "tool_calls") or ():
        function = _get(call, "function")
        total += len(str(_get(function, "name") or "")) + _serialised(_get(function, "arguments"))
    call = _get(message, "function_call")  # the legacy shape
    if call is not None:
        total += len(str(_get(call, "name") or "")) + _serialised(_get(call, "arguments"))
    return total


def messages_chars(messages: Any) -> int:
    """Characters of a chat request's ``messages``: text, and the tool calls,
    tool uses and tool results that ride in them. 0 if unreadable."""
    try:
        if isinstance(messages, (str, bytes)) or messages is None:
            return 0
        return sum(_message_chars(message) for message in messages)
    except Exception:
        _log.debug("runbound: unreadable messages while estimating tokens", exc_info=True)
        return 0


def request_chars(request: Any) -> int:
    """Characters of input a request will be billed for, 0 if unreadable.

    Everything the providers bill as input: chat ``messages`` (and the tool
    calls in them), the Responses API's ``instructions`` and ``input`` (a string,
    or items), Anthropic's ``system`` (a string, or blocks), and the tool
    definitions (``tools``, and the legacy ``functions``), counted by their
    serialised size. Never raises.
    """
    try:
        if not isinstance(request, dict):
            return 0
        total = messages_chars(request.get("messages"))
        total += _text_of(request.get("system"))
        total += _text_of(request.get("instructions"))
        total += _text_of(request.get("input"))
        for name in ("tools", "functions"):
            tools = request.get(name)
            if isinstance(tools, (list, tuple)) and tools:
                total += _serialised(list(tools))
        return total
    except Exception:
        _log.debug("runbound: unreadable request while estimating tokens", exc_info=True)
        return 0

