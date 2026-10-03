"""Anthropic pricing for 0.11.1: the Claude 5 family, the corrected Opus 4.x rows, one-hour cache writes,
the tokenizer factor, and the two multipliers a request itself can carry.

Every row below was read on 2026-10-03 from https://platform.claude.com/docs/en/about-claude/pricing
(model ids from https://platform.claude.com/docs/en/models/overview). A test pins the table to this list, so a
price can change only by changing both, with a new date and source.
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api, pricing
from runbound.pricing import (
    PRICES,
    admission_worst_case,
    admission_worst_case_tokens,
    estimate_cost,
    price_call,
    price_for,
    request_multiplier,
    token_estimate_factor,
)

SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
MODELS_SOURCE = "https://platform.claude.com/docs/en/models/overview"
READ_ON = "2026-10-03"

#: model -> (input, output, cache read, cache write 5m, cache write 1h), USD per 1M tokens.
PUBLISHED = {
    "claude-fable-5-1": (10, 50, 0.25, 12.50, 20),
    "claude-fable-5": (10, 50, 1.00, 12.50, 20),
    "claude-mythos-5-1": (10, 50, 0.25, 12.50, 20),
    "claude-mythos-5": (10, 50, 1.00, 12.50, 20),
    "claude-opus-5-5": (4, 20, 0.20, 5, 8),
    "claude-opus-5": (5, 25, 0.50, 6.25, 10),
    "claude-opus-4-8": (5, 25, 0.50, 6.25, 10),
    "claude-opus-4-7": (5, 25, 0.50, 6.25, 10),
    "claude-opus-4-6": (5, 25, 0.50, 6.25, 10),
    "claude-opus-4-5": (5, 25, 0.50, 6.25, 10),
    "claude-opus-4-1": (15, 75, 1.50, 18.75, 30),
    "claude-opus-4": (15, 75, 1.50, 18.75, 30),
    "claude-sonnet-5-5": (2, 10, 0.20, 2.50, 4),
    "claude-sonnet-5": (2, 10, 0.20, 2.50, 4),
    "claude-sonnet-4-6": (3, 15, 0.30, 3.75, 6),
    "claude-sonnet-4-5": (3, 15, 0.30, 3.75, 6),
    "claude-sonnet-4": (3, 15, 0.30, 3.75, 6),
    "claude-haiku-4-5": (1, 5, 0.10, 1.25, 2),
}


def test_the_table_carries_the_date_and_both_sources_the_rows_were_read_from():
    assert pricing.PRICES_AS_OF == READ_ON
    assert pricing.ANTHROPIC_PRICES_SOURCE == SOURCE
    assert pricing.ANTHROPIC_MODELS_SOURCE == MODELS_SOURCE


@pytest.mark.parametrize("model", sorted(PUBLISHED))
def test_every_published_row_is_in_the_table_exactly(model):
    assert PRICES[model] == pytest.approx(PUBLISHED[model])


def test_nothing_in_the_table_is_a_claude_row_that_was_not_published_or_kept_on_purpose():
    kept = {"claude-3-7-sonnet", "claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus", "claude-3-haiku"}
    assert {m for m in PRICES if m.startswith("claude-")} == set(PUBLISHED) | kept


@pytest.mark.parametrize("model", sorted(PUBLISHED))
def test_a_five_minute_write_is_1_25x_and_a_one_hour_write_2x_the_input_rate(model):
    price_in, _out, _read, write_5m, write_1h = PRICES[model]
    assert write_5m == pytest.approx(price_in * 1.25) and write_1h == pytest.approx(price_in * 2.0)


@pytest.mark.parametrize("model", sorted(PUBLISHED))
def test_a_cache_read_is_a_tenth_of_input_except_opus_5_5_and_fable_mythos_5_1(model):
    price_in, _out, read = PRICES[model][:3]
    factor = {"claude-opus-5-5": 0.05, "claude-fable-5-1": 0.025, "claude-mythos-5-1": 0.025}.get(model, 0.1)
    assert read == pytest.approx(price_in * factor)


# --- bug (b): Opus 4.6 to 4.8 were priced at the retired $15 / $75 row ------------------------------


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6"])
def test_opus_4_6_to_4_8_are_five_and_twenty_five_not_fifteen_and_seventy_five(model):
    assert price_for(model)[:2] == (5.0, 25.0)
    assert price_for(model + "-20260101")[:2] == (5.0, 25.0)  # a dated snapshot resolves to the same row
    assert estimate_cost(model, 1_000_000, 1_000_000) == pytest.approx(30.0)  # it was 90.0


def test_the_retired_opus_4_and_4_1_keep_their_own_rows():
    assert estimate_cost("claude-opus-4", 1_000_000, 0) == pytest.approx(15.0)
    assert estimate_cost("claude-opus-4-1-20250805", 1_000_000, 0) == pytest.approx(15.0)


# --- bug (a): the Claude 5 family was unpriced, so a money budget refused it -----------------------


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5"])
def test_the_claude_5_family_is_priced(model):
    assert price_for(model) is not None and price_call(model, 1000, 1000) [1] is False
    assert estimate_cost(model, 1_000_000, 0) == pytest.approx(PUBLISHED[model][0])


def test_a_money_budget_no_longer_refuses_a_claude_5_model_as_unpriced():
    api._teardown_for_tests()
    runbound.init(budget_usd=5.0, on_unpriced_model="refuse", on_anomaly="raise")
    runbound.record_call("claude-opus-5-5", 1000, 1000)
    assert runbound.current_session().total_cost_usd == pytest.approx(0.004 + 0.02)
    api._teardown_for_tests()


def test_opus_5_5_is_not_confused_with_opus_5():
    assert price_for("claude-opus-5-5-20261001")[:2] == (4.0, 20.0)
    assert price_for("claude-opus-5-20261001")[:2] == (5.0, 25.0)


# --- bug (c): a one-hour cache write was priced as a five-minute one -----------------------------


def test_a_one_hour_write_costs_2x_input_and_a_five_minute_write_1_25x():
    five = estimate_cost("claude-sonnet-4-5", 1_000_000, 0, tokens_cache_write_in=1_000_000)
    hour = estimate_cost("claude-sonnet-4-5", 1_000_000, 0, tokens_cache_write_in=1_000_000, tokens_cache_write_1h_in=1_000_000)
    assert five == pytest.approx(3.75) and hour == pytest.approx(6.0)


def test_the_one_hour_count_is_a_part_of_the_write_count_and_never_more_than_it():
    mixed = estimate_cost("claude-sonnet-4-5", 1_000_000, 0, tokens_cache_write_in=600_000, tokens_cache_write_1h_in=200_000)
    assert mixed == pytest.approx(0.4 * 3.0 + 0.4 * 3.75 + 0.2 * 6.0)
    clamped = estimate_cost("claude-sonnet-4-5", 1_000_000, 0, tokens_cache_write_in=100_000, tokens_cache_write_1h_in=900_000)
    assert clamped == pytest.approx(0.9 * 3.0 + 0.1 * 6.0)


def test_a_custom_price_without_a_one_hour_column_prices_the_hour_at_its_write_rate():
    custom = {"my-model": (1.0, 2.0, 0.1, 1.5)}
    cost = estimate_cost("my-model", 1_000_000, 0, custom, tokens_cache_write_in=1_000_000, tokens_cache_write_1h_in=1_000_000)
    assert cost == pytest.approx(1.5)
    assert estimate_cost("my-model", 1_000_000, 0, {"my-model": (1.0, 2.0)}, tokens_cache_write_in=1_000_000, tokens_cache_write_1h_in=1_000_000) == pytest.approx(1.0)


class _Bag:
    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        try:
            value = self._data[name]
        except KeyError:
            raise AttributeError(name) from None
        return _Bag(value) if isinstance(value, dict) else value


def _client(usage, model="claude-sonnet-4-5"):
    class Messages:
        def create(self, **kwargs):
            return _Bag({"model": model, "usage": usage, "content": []})

    client = type("FakeAnthropic", (), {})()
    client.messages = Messages()
    return client


def _spend(call):
    api._teardown_for_tests()
    runbound.init()
    call()
    spend = runbound.current_session().total_cost_usd
    api._teardown_for_tests()
    return spend


def test_a_response_with_the_ephemeral_split_is_priced_with_its_one_hour_part_through_the_wrapper():
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 400_000, "ephemeral_1h_input_tokens": 600_000}}

    def call():
        runbound.wrap(_client(usage)).messages.create(model="claude-sonnet-4-5", messages=[])

    assert _spend(call) == pytest.approx(0.4 * 3.75 + 0.6 * 6.0)


def test_a_response_without_the_split_is_all_five_minute():
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000}

    def call():
        runbound.wrap(_client(usage)).messages.create(model="claude-sonnet-4-5", messages=[])

    assert _spend(call) == pytest.approx(3.75)


def test_the_stream_reads_the_one_hour_part_off_message_start():
    from runbound.wrappers import _StreamUsage, anthropic_wrapper

    usage = _StreamUsage(model="claude-sonnet-4-5")
    anthropic_wrapper._chunk_usage(
        _Bag({"type": "message_start", "message": {"model": "claude-sonnet-4-5", "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}}}}),
        usage,
    )
    assert (usage.tokens_cache_write_in, usage.tokens_cache_write_1h_in) == (300, 200)


def test_a_report_that_predates_the_extra_keywords_is_still_called_the_old_way():
    from runbound.wrappers import _call_report

    seen = []

    def old_report(model, tokens_in, tokens_out, duration_s=0.0, tokens_reasoning=0, tokens_cached_in=0, tokens_cache_write_in=0, *, provider=None):
        seen.append((tokens_cache_write_in, provider))

    _call_report(old_report, "claude-sonnet-4-5", 10, 1, 0.1, 0, 0, 5, provider="anthropic", tokens_cache_write_1h_in=3, price_multiplier=1.1)
    assert seen == [(5, "anthropic")]


# --- what a request can change about its price ------------------------------------------------------


@pytest.mark.parametrize(
    "model, kwargs, want",
    [
        ("claude-opus-4-8", {}, 1.0),
        ("claude-opus-4-8", {"inference_geo": "us"}, 1.1),
        ("claude-opus-4-8", {"speed": "fast"}, 2.0),
        ("claude-opus-4-8", {"inference_geo": "us", "speed": "fast"}, 2.2),
        ("claude-opus-5-5", {"speed": "fast"}, 2.0),
        ("claude-opus-5", {"speed": "fast", "inference_geo": "us"}, 2.2),
        ("claude-opus-4-7", {"speed": "fast"}, 1.0),  # fast mode is not published for 4.7
        ("claude-opus-4-6", {"speed": "fast"}, 1.0),
        ("claude-opus-4-6", {"inference_geo": "us"}, 1.1),
        ("claude-sonnet-4-6", {"inference_geo": "us"}, 1.1),
        ("claude-sonnet-4-5", {"inference_geo": "us"}, 1.0),  # before 4.6: no such price
        ("claude-haiku-4-5", {"inference_geo": "us", "speed": "fast"}, 1.0),
        ("claude-opus-4-8", {"inference_geo": "eu"}, 1.0),
        ("claude-opus-4-8", {"extra_body": {"speed": "fast"}}, 2.0),
        ("claude-opus-4-8-20261001", {"speed": "fast"}, 2.0),
        ("gpt-4o", {"speed": "fast", "inference_geo": "us"}, 1.0),
        (None, {"speed": "fast"}, 1.0),
    ],
)
def test_the_multiplier_comes_only_from_a_field_the_request_carries(model, kwargs, want):
    assert request_multiplier(model, kwargs) == pytest.approx(want)


def test_a_request_that_is_not_a_dict_is_no_signal():
    for junk in (None, "us", 7, [], object()):
        assert request_multiplier("claude-opus-4-8", junk) == 1.0


def test_the_multiplier_scales_every_category_of_the_cost():
    base = estimate_cost("claude-opus-4-8", 1000, 1000, tokens_cached_in=100, tokens_cache_write_in=200, tokens_cache_write_1h_in=50)
    scaled = estimate_cost("claude-opus-4-8", 1000, 1000, tokens_cached_in=100, tokens_cache_write_in=200, tokens_cache_write_1h_in=50, multiplier=2.2)
    assert scaled == pytest.approx(base * 2.2)


@pytest.mark.parametrize("junk", [0, -1, float("inf"), float("nan"), "x", None])
def test_a_junk_multiplier_is_ignored(junk):
    assert estimate_cost("claude-opus-4-8", 1000, 1000, multiplier=junk) == estimate_cost("claude-opus-4-8", 1000, 1000)


def test_a_us_only_request_through_the_wrapper_is_billed_at_1_1x_and_without_the_field_it_is_not():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}

    def with_field():
        runbound.wrap(_client(usage, "claude-opus-4-8")).messages.create(model="claude-opus-4-8", messages=[], inference_geo="us")

    def without_field():
        runbound.wrap(_client(usage, "claude-opus-4-8")).messages.create(model="claude-opus-4-8", messages=[])

    assert _spend(with_field) == pytest.approx(5.5)
    assert _spend(without_field) == pytest.approx(5.0)


def test_the_other_provider_is_untouched():
    class Completions:
        def create(self, **kwargs):
            return _Bag({"model": "gpt-4o", "choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})

    client = type("FakeOpenAI", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = Completions()

    def call():
        runbound.wrap(client).chat.completions.create(model="gpt-4o", messages=[], speed="fast", inference_geo="us")

    assert _spend(call) == pytest.approx(2.5)


# --- bug (d): a newer tokenizer counts about 30% more tokens than characters over four ----------------


@pytest.mark.parametrize("model, want", [
    ("claude-opus-4-8", 1.3), ("claude-opus-4-7", 1.3), ("claude-opus-5", 1.3), ("claude-opus-5-5-20261001", 1.3),
    ("claude-sonnet-5", 1.3), ("claude-fable-5-1", 1.3), ("claude-mythos-5", 1.3),
    ("claude-opus-4-6", 1.0), ("claude-opus-4-5", 1.0), ("claude-sonnet-4-6", 1.0), ("claude-haiku-4-5", 1.0),
    ("gpt-4o", 1.0), (None, 1.0), ("", 1.0),
])
def test_the_tokenizer_factor(model, want):
    assert token_estimate_factor(model) == want


def test_admission_scales_the_input_estimate_and_only_the_input():
    price = PRICES["claude-opus-4-8"]
    plain = admission_worst_case(price, 100, 4000)
    scaled = admission_worst_case(price, 100, 4000, token_factor=1.3)
    assert plain == pytest.approx(1000 / 1e6 * 5 + 100 / 1e6 * 25)
    assert scaled == pytest.approx(1300 / 1e6 * 5 + 100 / 1e6 * 25)
    assert admission_worst_case_tokens(100, 4000, 1.3) == 1300 + 100 and admission_worst_case_tokens(100, 4000) == 1100


def test_the_estimate_rounds_the_scaled_input_up_never_down():
    assert admission_worst_case_tokens(0, 4, 1.3) == 2  # 1 token * 1.3 -> 2


def test_budget_admission_reserves_the_scaled_estimate_for_a_new_tokenizer_model():
    api._teardown_for_tests()
    runbound.init(budget_usd=1.0, budget_admission="capped", on_anomaly="raise")
    from runbound.engine import _admission_worst_case

    price = price_for("claude-opus-4-8")
    request = {"messages": [{"role": "user", "content": "x" * 4000}]}
    new = _admission_worst_case(price, 100, 100, request, "claude-opus-4-8")
    old = _admission_worst_case(price, 100, 100, request, "claude-opus-4-6")
    assert new > old and new == pytest.approx(admission_worst_case(price, 100, 4000, 1.3, 1.0))
    fast = _admission_worst_case(price, 100, 100, dict(request, speed="fast", inference_geo="us"), "claude-opus-4-8")
    assert fast == pytest.approx(new * 2.2)
    api._teardown_for_tests()
