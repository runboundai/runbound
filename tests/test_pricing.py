"""Tests for the pricing table and estimate_cost().

estimate_cost runs in the wrapper hot path: it must be total (never raise for
any input) and must resolve prices by exact match first, then longest prefix.
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
    price_in, price_out = PRICES[model]
    return (tokens_in / 1e6) * price_in + (tokens_out / 1e6) * price_out


# --- price table -----------------------------------------------------------


@pytest.mark.parametrize("model", REQUIRED_MODELS)
def test_table_covers_required_models(model):
    assert model in PRICES


@pytest.mark.parametrize("model", sorted(PRICES))
def test_table_entries_are_positive_input_output_pairs(model):
    entry = PRICES[model]
    assert isinstance(entry, tuple) and len(entry) == 2
    price_in, price_out = entry
    assert price_in > 0 and price_out > 0
    assert price_out >= price_in  # output is never cheaper than input


def test_table_prices_are_in_a_sane_per_million_range():
    # Guards against a per-1K/per-1M unit mixup sneaking into the table.
    for price_in, price_out in PRICES.values():
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
    assert estimate_cost(model, 1_000_000, 1_000_000) == pytest.approx(
        sum(PRICES[model])
    )


def test_cost_math_is_tokens_over_a_million_times_price():
    price_in, price_out = PRICES["gpt-4o"]
    expected = (1234 / 1e6) * price_in + (5678 / 1e6) * price_out
    assert estimate_cost("gpt-4o", 1234, 5678) == pytest.approx(expected)


def test_input_and_output_are_priced_separately():
    price_in, price_out = PRICES["claude-opus-4-5"]
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
