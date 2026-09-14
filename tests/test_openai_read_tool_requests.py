"""openai_wrapper.read_tool_requests: the fifth public function (found by T174).

CONTRIBUTING.md promises every wrapper module exposes it; anthropic_wrapper
always did and this one did not.
"""

from runbound.wrappers import openai_wrapper


def test_a_chat_completion_tool_call_is_read():
    response = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]}}]}
    assert openai_wrapper.read_tool_requests(response) == [
        ("get_weather", '{"city": "Paris"}')]


def test_a_responses_api_function_call_is_read():
    response = {"output": [
        {"type": "message", "content": []},
        {"type": "function_call", "name": "get_weather", "arguments": '{"city": "Paris"}'},
    ]}
    assert openai_wrapper.read_tool_requests(response) == [
        ("get_weather", '{"city": "Paris"}')]


def test_both_surfaces_hash_the_same_call_identically():
    chat = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "t", "arguments": '{"b": 2, "a": 1}'}}]}}]}
    responses = {"output": [{"type": "function_call", "name": "t", "arguments": '{"a": 1, "b": 2}'}]}
    assert openai_wrapper.read_tool_requests(chat) == openai_wrapper.read_tool_requests(responses)


def test_a_response_with_no_tool_calls_or_no_shape_reads_none():
    assert openai_wrapper.read_tool_requests({"choices": [{"message": {"content": "hi"}}]}) == []
    assert openai_wrapper.read_tool_requests(object()) == []
    assert openai_wrapper.read_tool_requests(None) == []


def test_both_wrappers_expose_the_same_five_functions():
    from runbound.wrappers import anthropic_wrapper

    five = ("matches", "is_wrapped", "install", "read_usage", "read_tool_requests")
    for module in (openai_wrapper, anthropic_wrapper):
        missing = [name for name in five if not callable(getattr(module, name, None))]
        assert not missing, f"{module.__name__} lacks {missing}"
