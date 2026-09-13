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

**Cached input tokens (T139).** Both OpenAI and Anthropic bill some of a
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
"""

from __future__ import annotations

import logging

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
PRICES_AS_OF = "2026-09-12"

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
    str, tuple[float, float] | tuple[float, float, float] | tuple[float, float, float, float]
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
    # --- Anthropic --- (in, out, cache-read rate at 10% of in, cache-write
    # rate at 125% of in)
    "claude-opus-4-5": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-opus-4-1": (15.00, 75.00, 1.50, 18.75),
    "claude-opus-4": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-4": (3.00, 15.00, 0.30, 3.75),
    "claude-3-7-sonnet": (3.00, 15.00, 0.30, 3.75),
    "claude-3-5-sonnet": (3.00, 15.00, 0.30, 3.75),
    "claude-3-5-haiku": (0.80, 4.00, 0.08, 1.00),
    "claude-3-opus": (15.00, 75.00, 1.50, 18.75),
    "claude-3-haiku": (0.25, 1.25, 0.025, 0.3125),
}


def as_of() -> str:
    """The date `PRICES` (including its cached-input rates) was last checked.

    A price that changed after this date is wrong until the table is
    updated; pass `custom_prices` for numbers you need to be exact right now.
    """
    return PRICES_AS_OF


_Price = (
    tuple[float, float] | tuple[float, float, float] | tuple[float, float, float, float]
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
) -> float:
    """Estimate USD cost of one call.

    `custom_prices` is consulted first and wins over the built-in table.
    Within each table, an exact model match wins, then the longest matching
    prefix (so "gpt-4o-mini-2024-07-18" prices as "gpt-4o-mini", not "gpt-4o").
    A prefix must end on a segment boundary, and in the built-in table it must
    not be followed by another family's name — "o3-pro" is unpriced, not an
    "o3". The custom table is the caller's own and gets no such second-guessing.

    `tokens_cached_in` and `tokens_cache_write_in` are both subsets of
    `tokens_in` (T139) — never additional tokens, always a slice of the total
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
            price, tokens_in, tokens_out, tokens_cached_in, tokens_cache_write_in
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
    """
    price_in, price_out = pair[0], pair[1]
    tokens_in = max(tokens_in, 0)
    write = min(max(tokens_cache_write_in, 0), tokens_in)
    cached = min(max(tokens_cached_in, 0), tokens_in - write)
    regular = tokens_in - cached - write
    price_cached_in = pair[2] if len(pair) > 2 else price_in
    price_write_in = pair[3] if len(pair) > 3 else price_in
    cost_in = (
        (regular / 1e6) * price_in
        + (cached / 1e6) * price_cached_in
        + (write / 1e6) * price_write_in
    )
    cost_out = (max(tokens_out, 0) / 1e6) * price_out
    return float(cost_in + cost_out)


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
) -> tuple[float, bool]:
    """Price one call under ``on_unpriced_model``, as ``(cost_usd, estimated)``.

    A model that *is* priced (``custom_prices`` or the built-in table) is
    priced exactly like :func:`estimate_cost`, cache reads and writes
    (``T139``) and all, and ``estimated`` is ``False``. An unpriced model is
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
                    price, tokens_in, tokens_out, tokens_cached_in, tokens_cache_write_in
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
