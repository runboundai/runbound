"""Tests for the pricing table and estimate_cost().

estimate_cost runs in the wrapper hot path: it must be total (never raise for
any input) and must resolve prices by exact match first, then longest prefix.

T139 gave some `PRICES` entries a third element — a published cached-input
rate — so every helper below that used to unpack `PRICES[model]` as a plain
2-tuple now reads `price[0]`/`price[1]` instead: the *reason* each such test
changed, cited once here rather than repeated at each call site. Cached-rate
pricing itself is exercised in the "cached input tokens" section below.
"""

import pytest

from runbound.pricing import PRICES, estimate_cost

REQUIRED_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "o3",
    "o4-mini",
    "claude-sonnet-4-5",
    "claude-opus-4-5",
    "claude-haiku-4-5",
]


def _expected(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES[model][0], PRICES[model][1]
    return (tokens_in / 1e6) * price_in + (tokens_out / 1e6) * price_out


# --- price table -----------------------------------------------------------


@pytest.mark.parametrize("model", REQUIRED_MODELS)
def test_table_covers_required_models(model):
    assert model in PRICES


@pytest.mark.parametrize("model", sorted(PRICES))
def test_table_entries_are_positive_input_output_pairs(model):
    # T139: an entry is (in, out), (in, out, cached_in), or — for a provider
    # that also publishes a cache-write premium — (in, out, cached_in,
    # cache_write_in). Was a strict 2-tuple before this table grew extra
    # columns.
    entry = PRICES[model]
    assert isinstance(entry, tuple) and len(entry) in (2, 3, 4)
    price_in, price_out = entry[0], entry[1]
    assert price_in > 0 and price_out > 0
    assert price_out >= price_in  # output is never cheaper than input


@pytest.mark.parametrize("model", sorted(PRICES))
def test_table_cached_rate_is_a_real_discount_when_present(model):
    entry = PRICES[model]
    if len(entry) < 3:
        return
    price_in, price_cached_in = entry[0], entry[2]
    assert 0 < price_cached_in < price_in  # a cache read is billed, just less


@pytest.mark.parametrize("model", sorted(PRICES))
def test_table_cache_write_rate_is_a_real_premium_when_present(model):
    entry = PRICES[model]
    if len(entry) < 4:
        return
    price_in, price_write_in = entry[0], entry[3]
    assert price_write_in > price_in  # a cache write costs more, not less


@pytest.mark.parametrize("model", [m for m in PRICES if len(PRICES[m]) > 3])
def test_cache_write_rate_is_125_percent_of_input(model):
    price_in, _price_out, _price_cached_in, price_write_in = PRICES[model]
    assert price_write_in == pytest.approx(price_in * 1.25)


def test_table_prices_are_in_a_sane_per_million_range():
    # Guards against a per-1K/per-1M unit mixup sneaking into the table.
    # T139: unpacks price_in/price_out by position rather than a bare 2-tuple
    # unpack, since some entries now carry a third (cached_in) element.
    for entry in PRICES.values():
        price_in, price_out = entry[0], entry[1]
        assert 0.01 <= price_in <= 200.0
        assert 0.01 <= price_out <= 1000.0


def test_mini_variants_are_cheaper_than_their_base_model():
    assert PRICES["gpt-4o-mini"][0] < PRICES["gpt-4o"][0]
    assert PRICES["gpt-4.1-mini"][0] < PRICES["gpt-4.1"][0]
    assert PRICES["gpt-4.1-nano"][0] < PRICES["gpt-4.1-mini"][0]
    assert PRICES["claude-haiku-4-5"][0] < PRICES["claude-opus-4-5"][0]


# --- exact matching --------------------------------------------------------


@pytest.mark.parametrize("model", REQUIRED_MODELS)
def test_exact_match_uses_table_price(model):
    # T139: sum(PRICES[model]) summed a third (cached_in) element too, once
    # a model had one; only (in, out) belong in an all-uncached-tokens total.
    assert estimate_cost(model, 1_000_000, 1_000_000) == pytest.approx(
        sum(PRICES[model][:2])
    )


def test_cost_math_is_tokens_over_a_million_times_price():
    price_in, price_out = PRICES["gpt-4o"][0], PRICES["gpt-4o"][1]
    expected = (1234 / 1e6) * price_in + (5678 / 1e6) * price_out
    assert estimate_cost("gpt-4o", 1234, 5678) == pytest.approx(expected)


def test_input_and_output_are_priced_separately():
    price_in, price_out = PRICES["claude-opus-4-5"][0], PRICES["claude-opus-4-5"][1]
    assert price_in != price_out
    only_in = estimate_cost("claude-opus-4-5", 1_000_000, 0)
    only_out = estimate_cost("claude-opus-4-5", 0, 1_000_000)
    assert only_in == pytest.approx(price_in)
    assert only_out == pytest.approx(price_out)


# --- prefix matching -------------------------------------------------------


@pytest.mark.parametrize(
    "dated,base",
    [
        ("gpt-4o-2024-11-20", "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("gpt-4.1-2025-04-14", "gpt-4.1"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("claude-opus-4-5-20251101", "claude-opus-4-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("o4-mini-2025-04-16", "o4-mini"),
    ],
)
def test_dated_variant_matches_base_model_by_prefix(dated, base):
    assert estimate_cost(dated, 1_000, 2_000) == pytest.approx(
        _expected(base, 1_000, 2_000)
    )


def test_longest_prefix_wins_for_mini_over_base():
    mini = estimate_cost("gpt-4o-mini-2024-07-18", 1_000_000, 0)
    assert mini == pytest.approx(PRICES["gpt-4o-mini"][0])
    assert mini != pytest.approx(PRICES["gpt-4o"][0])


def test_longest_prefix_wins_for_nano_over_mini():
    nano = estimate_cost("gpt-4.1-nano-2025-04-14", 1_000_000, 0)
    assert nano == pytest.approx(PRICES["gpt-4.1-nano"][0])


def test_prefix_match_does_not_run_backwards():
    # "gpt-4" is a prefix of "gpt-4o", but a shorter model name must not
    # inherit a longer key's price.
    assert estimate_cost("gpt-4", 1_000_000, 1_000_000) == 0.0


# --- custom_prices ---------------------------------------------------------


def test_custom_prices_override_the_table():
    custom = {"gpt-4o": (1.0, 2.0)}
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000, custom) == pytest.approx(3.0)


def test_custom_prices_add_an_unknown_model():
    custom = {"llama-3.1-70b-local": (0.2, 0.4)}
    assert estimate_cost(
        "llama-3.1-70b-local", 500_000, 500_000, custom
    ) == pytest.approx(0.3)


def test_custom_prices_match_by_prefix_too():
    custom = {"my-model": (10.0, 20.0)}
    assert estimate_cost("my-model-v2-20250101", 1_000_000, 0, custom) == pytest.approx(
        10.0
    )


def test_custom_prices_win_even_when_table_has_an_exact_match():
    custom = {"gpt-4o": (100.0, 100.0)}
    assert estimate_cost("gpt-4o-2024-11-20", 1_000_000, 0, custom) == pytest.approx(
        100.0
    )


def test_custom_prices_fall_through_to_table_when_no_match():
    custom = {"some-other-model": (99.0, 99.0)}
    assert estimate_cost("gpt-4o", 1_000_000, 0, custom) == pytest.approx(
        PRICES["gpt-4o"][0]
    )


def test_empty_custom_prices_behaves_like_none():
    assert estimate_cost("gpt-4o", 1_000, 1_000, {}) == pytest.approx(
        estimate_cost("gpt-4o", 1_000, 1_000)
    )


def test_custom_prices_can_price_a_model_at_zero():
    assert estimate_cost("local-llm", 5_000_000, 5_000_000, {"local-llm": (0.0, 0.0)}) == 0.0


# --- unknown / zero / boundary --------------------------------------------


def test_none_model_is_free():
    assert estimate_cost(None, 1_000_000, 1_000_000) == 0.0


def test_unknown_model_is_free():
    assert estimate_cost("totally-made-up-model", 1_000_000, 1_000_000) == 0.0


def test_empty_model_name_is_free():
    assert estimate_cost("", 1_000_000, 1_000_000) == 0.0


def test_zero_tokens_cost_nothing():
    assert estimate_cost("gpt-4o", 0, 0) == 0.0


@pytest.mark.parametrize(
    "tokens_in,tokens_out",
    [(-1000, 0), (0, -1000), (-1000, -1000), (-5, 1_000_000)],
)
def test_negative_tokens_clamp_to_zero(tokens_in, tokens_out):
    cost = estimate_cost("gpt-4o", tokens_in, tokens_out)
    expected = _expected("gpt-4o", max(tokens_in, 0), max(tokens_out, 0))
    assert cost == pytest.approx(expected)
    assert cost >= 0.0


def test_result_is_always_a_float():
    assert isinstance(estimate_cost("gpt-4o", 0, 0), float)
    assert isinstance(estimate_cost(None, 0, 0), float)
    assert isinstance(estimate_cost("gpt-4o", 10, 10), float)


# --- fail-open: never raises ----------------------------------------------


@pytest.mark.parametrize(
    "model", [123, 4.5, object(), b"gpt-4o", ["gpt-4o"], {"gpt-4o": 1}, True]
)
def test_non_string_model_returns_zero_without_raising(model):
    assert estimate_cost(model, 100, 100) == 0.0


@pytest.mark.parametrize("tokens", ["100", None, object(), [1], float("nan")])
def test_junk_token_counts_never_raise(tokens):
    cost = estimate_cost("gpt-4o", tokens, tokens)
    assert isinstance(cost, float)


@pytest.mark.parametrize(
    "custom",
    [
        "not-a-dict",
        42,
        {"gpt-4o": "cheap"},
        {"gpt-4o": (1.0,)},
        {"gpt-4o": None},
        {123: (1.0, 2.0)},
    ],
)
def test_malformed_custom_prices_never_raise(custom):
    cost = estimate_cost("gpt-4o", 1_000, 1_000, custom)
    assert isinstance(cost, float)
    assert cost >= 0.0


def test_exploding_custom_prices_mapping_never_raises():
    class Exploding(dict):
        def get(self, key, default=None):
            raise RuntimeError("boom")

        def __iter__(self):
            raise RuntimeError("boom")

    assert isinstance(estimate_cost("gpt-4o", 100, 100, Exploding()), float)


def test_table_is_not_mutated_by_custom_prices():
    before = dict(PRICES)
    estimate_cost("gpt-4o", 100, 100, {"gpt-4o": (999.0, 999.0)})
    assert PRICES == before


def test_pricing_module_has_no_internal_imports():
    import runbound.pricing as pricing

    source = open(pricing.__file__, encoding="utf-8").read()
    assert "runbound." not in source.replace("runbound.pricing", "")


# --- cached input tokens (T139) ---------------------------------------------


def test_a_cached_heavy_call_costs_the_documented_mix():
    """The exact example from the launch plan, computed by hand.

    1000 of 1200 input tokens were a cache hit on gpt-4o (2.50 / 10.00 per
    1M, cached at 50%: 1.25 per 1M): 200 regular input tokens at the full
    rate, 1000 cached at half, plus 50 output tokens at the output rate.
    """
    price_in, price_out, price_cached_in = PRICES["gpt-4o"]
    assert price_cached_in == pytest.approx(price_in * 0.5)
    expected = (200 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (50 / 1e6) * price_out
    cost = estimate_cost("gpt-4o", 1200, 50, tokens_cached_in=1000)
    assert cost == pytest.approx(expected)
    # Sanity: the discount must actually be cheaper than pricing everything
    # at the full input rate — otherwise this "bug fix" fixed nothing.
    full_rate = estimate_cost("gpt-4o", 1200, 50, tokens_cached_in=0)
    assert cost < full_rate


def test_anthropic_cached_heavy_call_costs_the_documented_mix():
    """Same shape, Anthropic's 10%-of-input cached read rate."""
    price_in, price_out, price_cached_in, _price_write_in = PRICES["claude-sonnet-4-5"]
    assert price_cached_in == pytest.approx(price_in * 0.10)
    expected = (200 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (50 / 1e6) * price_out
    cost = estimate_cost("claude-sonnet-4-5", 1200, 50, tokens_cached_in=1000)
    assert cost == pytest.approx(expected)


def test_a_call_mixing_a_cache_read_and_a_cache_write_prices_both_rates():
    """The example the coordinator asked for: read and write in one call.

    claude-sonnet-4-5: 3.00 / 15.00 per 1M, cache read at 0.30 (10%), cache
    write at 3.75 (125%). 200 fresh + 1000 cache-read + 300 cache-write
    input tokens, 80 output.
    """
    price_in, price_out, price_cached_in, price_write_in = PRICES["claude-sonnet-4-5"]
    assert (price_cached_in, price_write_in) == pytest.approx((0.30, 3.75))
    expected = (
        (200 / 1e6) * price_in
        + (1000 / 1e6) * price_cached_in
        + (300 / 1e6) * price_write_in
        + (80 / 1e6) * price_out
    )
    cost = estimate_cost(
        "claude-sonnet-4-5",
        1500,
        80,
        tokens_cached_in=1000,
        tokens_cache_write_in=300,
    )
    assert cost == pytest.approx(expected)
    # The write premium must bite, in both directions it could be gotten
    # wrong: pricing the write tokens as a (cheap) cache read...
    priced_as_read = (
        (200 / 1e6) * price_in + (1300 / 1e6) * price_cached_in + (80 / 1e6) * price_out
    )
    assert cost > priced_as_read
    # ...or, the rejected simplification this replaces, folding the write
    # tokens into the plain-rate "regular" bucket instead of pricing them at
    # their own premium — cheaper than the true cost either way.
    priced_as_the_old_simplification = (
        (500 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (80 / 1e6) * price_out
    )
    assert cost > priced_as_the_old_simplification


def test_an_anthropic_model_with_no_published_write_rate_uses_the_full_input_rate():
    # Every Anthropic entry in the table publishes a write rate today; this
    # pins the fallback for a hypothetical one that does not, via a custom
    # 3-tuple that stops short of a fourth element.
    custom = {"claude-hypothetical": (3.00, 15.00, 0.30)}
    with_write = estimate_cost(
        "claude-hypothetical", 1000, 0, custom, tokens_cache_write_in=1000
    )
    without_write = estimate_cost(
        "claude-hypothetical", 1000, 0, custom, tokens_cache_write_in=0
    )
    assert with_write == pytest.approx(without_write)  # priced at the plain rate either way
    assert with_write == pytest.approx(0.003)  # 1000 / 1e6 * 3.00


def test_custom_prices_with_a_cache_write_rate_are_honored():
    custom = {"my-model": (10.0, 10.0, 2.0, 12.5)}
    cost = estimate_cost(
        "my-model", 1_000_000, 0, custom, tokens_cache_write_in=1_000_000
    )
    assert cost == pytest.approx(12.5)


def test_cache_write_and_cache_read_never_overlap_when_they_overclaim_tokens_in():
    # A wrapper bug reporting write + read > tokens_in must not go negative:
    # write is clamped to tokens_in first, then read absorbs only what is
    # left of tokens_in after that — the two never double-count a token.
    cost = estimate_cost(
        "claude-sonnet-4-5", 100, 0, tokens_cached_in=90, tokens_cache_write_in=90
    )
    price_in, _price_out, price_cached_in, price_write_in = PRICES["claude-sonnet-4-5"]
    # write claims 90 (clamped to tokens_in=100); read absorbs the remaining
    # 10, not its full requested 90; regular is 0.
    expected = (10 / 1e6) * price_cached_in + (90 / 1e6) * price_write_in
    assert cost == pytest.approx(expected)


def test_negative_cache_write_tokens_clamp_to_zero():
    negative = estimate_cost("claude-sonnet-4-5", 1_000, 0, tokens_cache_write_in=-500)
    none = estimate_cost("claude-sonnet-4-5", 1_000, 0, tokens_cache_write_in=0)
    assert negative == pytest.approx(none)


def test_junk_tokens_cache_write_in_never_raises():
    for junk in ("100", None, object(), [1], float("nan")):
        cost = estimate_cost("claude-sonnet-4-5", 1_000, 0, tokens_cache_write_in=junk)
        assert isinstance(cost, float)


def test_all_cached_prices_at_the_cached_rate():
    price_in, _price_out, price_cached_in = PRICES["gpt-4o"]
    cost = estimate_cost("gpt-4o", 1_000_000, 0, tokens_cached_in=1_000_000)
    assert cost == pytest.approx(price_cached_in)
    assert cost != pytest.approx(price_in)


def test_a_model_with_no_published_cached_rate_prices_cached_tokens_at_the_full_rate():
    # gpt-4-turbo predates prompt caching: a 2-tuple entry, no third element.
    assert len(PRICES["gpt-4-turbo"]) == 2
    with_cached = estimate_cost("gpt-4-turbo", 1_000, 0, tokens_cached_in=1_000)
    without_cached = estimate_cost("gpt-4-turbo", 1_000, 0, tokens_cached_in=0)
    assert with_cached == pytest.approx(without_cached)


def test_an_unpriced_model_ignores_tokens_cached_in_and_stays_free():
    assert estimate_cost("totally-made-up-model", 1_000, 0, tokens_cached_in=1_000) == 0.0


def test_zero_cached_tokens_is_the_same_as_not_passing_the_argument():
    with_zero = estimate_cost("gpt-4o", 1_000, 500, tokens_cached_in=0)
    without = estimate_cost("gpt-4o", 1_000, 500)
    assert with_zero == pytest.approx(without)


def test_cached_tokens_greater_than_tokens_in_clamps_to_tokens_in():
    # A caller reporting more "cached" than "total input" is a wrapper bug,
    # not a reason to charge negative regular tokens.
    over = estimate_cost("gpt-4o", 1_000, 0, tokens_cached_in=5_000)
    all_cached = estimate_cost("gpt-4o", 1_000, 0, tokens_cached_in=1_000)
    assert over == pytest.approx(all_cached)


def test_negative_cached_tokens_clamp_to_zero():
    negative = estimate_cost("gpt-4o", 1_000, 0, tokens_cached_in=-500)
    none = estimate_cost("gpt-4o", 1_000, 0, tokens_cached_in=0)
    assert negative == pytest.approx(none)


def test_custom_prices_with_a_cached_rate_are_honored():
    custom = {"my-model": (10.0, 10.0, 2.0)}
    cost = estimate_cost("my-model", 1_000_000, 0, custom, tokens_cached_in=1_000_000)
    assert cost == pytest.approx(2.0)


def test_custom_prices_without_a_cached_rate_price_cached_tokens_at_full_rate():
    custom = {"my-model": (10.0, 10.0)}
    cost = estimate_cost("my-model", 1_000_000, 0, custom, tokens_cached_in=1_000_000)
    assert cost == pytest.approx(10.0)


def test_junk_tokens_cached_in_never_raises():
    for junk in ("100", None, object(), [1], float("nan")):
        cost = estimate_cost("gpt-4o", 1_000, 0, tokens_cached_in=junk)
        assert isinstance(cost, float)


def test_as_of_returns_the_table_date():
    from runbound.pricing import PRICES_AS_OF, as_of

    assert as_of() == PRICES_AS_OF
    assert isinstance(as_of(), str) and as_of()  # non-empty
