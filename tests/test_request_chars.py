"""A request's input is counted for everything the provider bills as input.

The admission estimate used to read only ``messages``, so a Responses-API call
(``input``) estimated no input at all and an Anthropic ``system`` prompt and
every tool definition were skipped: budgets undercounted those calls. The rule
is that the estimate never undercounts a field the provider bills for.
"""

import json

import pytest

from runbound import api, pricing
from runbound.config import GuardrailConfig
from runbound.engine import Engine, _admission_worst_case
from runbound.exceptions import GuardrailTripped
from runbound.pricing import admission_worst_case, estimated_tokens, request_chars
from runbound.state import SessionState
from runbound.wrappers import messages_chars

TOOL = {"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}
ANTHROPIC_TOOL = {"name": "get_weather", "description": "Weather for a city",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}


def compact(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


# --- chat completions ----------------------------------------------------------


def test_chat_messages_string_and_parts_are_counted():
    request = {"messages": [{"role": "user", "content": "x" * 10},
                            {"role": "user", "content": [{"type": "text", "text": "y" * 7}, {"type": "image_url"}]}]}

    assert request_chars(request) == 17  # an image says nothing about size: not guessed at


def test_an_assistant_tool_call_and_its_result_are_input_on_the_next_turn():
    request = {"messages": [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"id": 42}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "found it"},
    ]}

    assert request_chars(request) == len("lookup") + len('{"id": 42}') + len("found it")


def test_tool_definitions_are_counted_by_their_serialised_size():
    assert request_chars({"messages": [], "tools": [TOOL]}) == compact([TOOL])
    assert request_chars({"messages": [], "functions": [TOOL["function"]]}) == compact([TOOL["function"]])


# --- the Responses API ---------------------------------------------------------


def test_responses_input_string_is_counted():
    assert request_chars({"model": "gpt-4o", "input": "z" * 40}) == 40


def test_responses_instructions_and_input_items_are_counted():
    request = {"instructions": "a" * 5, "input": [
        {"role": "user", "content": "b" * 6},
        {"role": "user", "content": [{"type": "input_text", "text": "c" * 7}]},
        {"type": "function_call", "name": "f", "arguments": '{"k": 1}'},
        {"type": "function_call_output", "output": "done"},
    ]}

    assert request_chars(request) == 5 + 6 + 7 + len('{"k": 1}') + len("done")


def test_responses_tools_are_counted():
    tool = {"type": "function", "name": "get_weather", "description": "d", "parameters": {"type": "object"}}

    assert request_chars({"input": "hi", "tools": [tool]}) == 2 + compact([tool])


# --- Anthropic -----------------------------------------------------------------


def test_anthropic_system_string_and_blocks_are_counted():
    assert request_chars({"system": "s" * 9, "messages": [{"role": "user", "content": "hi"}]}) == 11
    blocks = [{"type": "text", "text": "a" * 4}, {"type": "text", "text": "b" * 5}]
    assert request_chars({"system": blocks, "messages": []}) == 9


def test_anthropic_tool_use_input_and_tool_result_content_are_counted():
    request = {"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {"city": "Paris"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "sunny"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2",
                                       "content": [{"type": "text", "text": "cloudy"}]}]},
    ]}

    assert request_chars(request) == compact({"city": "Paris"}) + len("sunny") + len("cloudy")


def test_anthropic_tools_are_counted():
    assert request_chars({"system": "s", "messages": [], "tools": [ANTHROPIC_TOOL]}) == 1 + compact([ANTHROPIC_TOOL])


# --- never raises, never undercounts what it recognises ---------------------------------


@pytest.mark.parametrize("bad", [None, 5, "text", [], object(), {"messages": 5}, {"messages": "x"},
                                 {"tools": 3}, {"input": 3}, {"system": 3}, {"messages": [None, 7]}])
def test_an_unreadable_request_is_zero_and_never_raises(bad):
    assert request_chars(bad) == 0


def test_a_tool_definition_that_will_not_serialise_is_still_counted():
    class Opaque:
        def __str__(self):
            return "opaque-tool"

    assert request_chars({"tools": [Opaque()]}) >= len("opaque-tool")


def test_the_wrapper_messages_reader_agrees_with_the_pricing_one():
    messages = [{"role": "user", "content": "hello"},
                {"role": "assistant", "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}]

    assert messages_chars(messages) == pricing.messages_chars(messages) == 5 + 1 + 2


# --- the published formula -----------------------------------------------------


def test_admission_worst_case_is_input_at_the_plain_rate_plus_output():
    price = (2.0, 8.0)  # dollars per million tokens

    assert admission_worst_case(price, 1_000, 4_000) == pytest.approx((1_000 / 1e6) * 8.0 + (1_000 / 1e6) * 2.0)
    assert estimated_tokens(4_001) == 1_001  # rounded up


def test_a_cache_column_is_not_used_admission_assumes_no_discount():
    assert admission_worst_case((2.0, 8.0, 0.2, 2.5), 0, 4_000) == admission_worst_case((2.0, 8.0), 0, 4_000)


def test_the_engine_composes_the_same_formula():
    request = {"model": "m", "input": "q" * 400, "tools": [TOOL]}
    chars = request_chars(request)

    assert _admission_worst_case((2.0, 8.0), None, 100, request) == pytest.approx(
        admission_worst_case((2.0, 8.0), 100, chars))
    assert _admission_worst_case((2.0, 8.0), 50, 100, request) == pytest.approx(
        admission_worst_case((2.0, 8.0), 50, chars))  # the request's own cap wins


# --- what it changes: admission now refuses what it used to admit ---------------------


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def admit(request, budget=0.01):
    """One admission against a budget the INPUT alone decides: the output is
    reserved at a single token, so only what the request sends can tip it."""
    config = GuardrailConfig(budget_usd=budget, budget_admission=True, admission_output_tokens=1)
    config.validate()
    eng = Engine(config, observers=[])
    return eng.admit(SessionState("s1"), "openai@default", "gpt-4o", request)


@pytest.mark.parametrize("request_", [
    {"input": "x" * 40_000},  # Responses
    {"system": "x" * 40_000, "messages": [{"role": "user", "content": "hi"}]},  # Anthropic system
    {"messages": [{"role": "user", "content": "hi"}], "tools": [dict(TOOL, function=dict(TOOL["function"], description="x" * 40_000))]},
], ids=["responses input", "anthropic system", "tool definitions"])
def test_a_call_whose_billed_input_alone_exceeds_the_budget_is_refused_before_it_is_made(request_):
    with pytest.raises(GuardrailTripped):
        admit(request_)


def test_a_small_request_of_each_shape_is_still_admitted():
    for request in ({"input": "hi"}, {"system": "be brief", "messages": [{"role": "user", "content": "hi"}]},
                    {"messages": [{"role": "user", "content": "hi"}], "tools": [TOOL]}):
        admit(request)
