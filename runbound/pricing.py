"""Model price table and cost estimation.

Leaf module: stdlib only, no internal imports. `estimate_cost` runs on every
wrapped LLM call, so it is total (never raises, for any input) and does no
I/O. It logs nothing above DEBUG except one WARNING per unknown model, and
only when the caller asks for it (`warn_unpriced`): a model priced at $0.00 is
a model a dollar budget cannot see, which is worth saying once.

Prices are USD per 1M tokens as (input, output) and are published list prices
that drift over time; callers who need exact numbers pass `custom_prices`
(also the mechanism for self-hosted / OpenAI-compatible endpoints).

`price_for` answers "is this model priced at all", and `price_call` wraps
`estimate_cost` with the `on_unpriced_model` policy ("zero" / "estimate" /
"refuse") for callers that need to know not just the cost but whether it was
a real price or a configured fallback.
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

#: model name -> (usd_per_1M_input_tokens, usd_per_1M_output_tokens).
#: Dated releases (e.g. "gpt-4o-2024-11-20") resolve by longest-prefix match.
#: List prices as of 2026-09; they drift, so pass `custom_prices` for billing.
PRICES: dict[str, tuple[float, float]] = {
    # --- OpenAI ---
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # --- Anthropic ---
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-haiku": (0.25, 1.25),
}


def price_for(
    model: str | None, custom_prices: dict[str, tuple[float, float]] | None = None
) -> tuple[float, float] | None:
    """The ``(usd_per_1M_input, usd_per_1M_output)`` price for ``model``, if any.

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
) -> tuple[float, float] | None:
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
    custom_prices: dict[str, tuple[float, float]] | None = None,
    *,
    warn_unpriced: bool = False,
) -> float:
    """Estimate USD cost of one call.

    `custom_prices` is consulted first and wins over the built-in table.
    Within each table, an exact model match wins, then the longest matching
    prefix (so "gpt-4o-mini-2024-07-18" prices as "gpt-4o-mini", not "gpt-4o").
    A prefix must end on a segment boundary, and in the built-in table it must
    not be followed by another family's name — "o3-pro" is unpriced, not an
    "o3". The custom table is the caller's own and gets no such second-guessing.

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
        price_in, price_out = price
        cost_in = (max(tokens_in, 0) / 1e6) * price_in
        cost_out = (max(tokens_out, 0) / 1e6) * price_out
        return float(cost_in + cost_out)
    except Exception:  # fail-open: pricing must never break the host call
        _log.debug("estimate_cost failed for model %r", model, exc_info=True)
        return 0.0


def _cost_from_pair(pair: tuple[float, float], tokens_in: int, tokens_out: int) -> float:
    """``(tokens_in, tokens_out)`` priced at a ``(usd_per_1M_in, usd_per_1M_out)`` pair."""
    price_in, price_out = pair
    return float((max(tokens_in, 0) / 1e6) * price_in + (max(tokens_out, 0) / 1e6) * price_out)


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
    custom_prices: dict[str, tuple[float, float]] | None = None,
    *,
    on_unpriced_model: str = "zero",
    unpriced_price_per_1m_usd: tuple[float, float] | None = None,
) -> tuple[float, bool]:
    """Price one call under ``on_unpriced_model``, as ``(cost_usd, estimated)``.

    A model that *is* priced (``custom_prices`` or the built-in table) is
    priced exactly like :func:`estimate_cost`, and ``estimated`` is ``False``.
    An unpriced model is priced by ``on_unpriced_model``:

    - ``"zero"`` (default): ``0.0``, warned once per model per process — the
      same warning :func:`estimate_cost` gives with ``warn_unpriced=True``.
    - ``"estimate"``: priced from ``unpriced_price_per_1m_usd``,
      ``estimated=True``. With no such pair configured (config requires one
      for this mode, but this function stays defensive), falls back to the
      "zero" behavior above.
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
            return _cost_from_pair(price, tokens_in, tokens_out), False
        if on_unpriced_model == "estimate" and unpriced_price_per_1m_usd:
            return _cost_from_pair(unpriced_price_per_1m_usd, tokens_in, tokens_out), True
        if on_unpriced_model == "refuse":
            estimated = bool(unpriced_price_per_1m_usd)
            _warn_refuse_recorded(model, estimated)
            if estimated:
                return _cost_from_pair(unpriced_price_per_1m_usd, tokens_in, tokens_out), True
            return 0.0, False
        _warn_unpriced(model)
        return 0.0, False
    except Exception:  # fail-open: pricing must never break the host call
        _log.debug("price_call failed for model %r", model, exc_info=True)
        return 0.0, False
