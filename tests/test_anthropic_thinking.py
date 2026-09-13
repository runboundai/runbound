"""T173: Anthropic states its extended-thinking count, and the wrapper reads it.

Found by the conformance kit (Wave G, T143) replaying a real `anthropic` 1.5.0
response: `usage.output_tokens_details.thinking_tokens` was 115 and the wrapper
reported 0. The count is a *subset* of `output_tokens`, so nothing about the
bill changes -- what changed is that a customer can now see how much of an
answer was thinking, on the event and on the wire.
"""

import runbound
from runbound.wrappers import anthropic_wrapper


class _Bag:
    """An attribute view over a dict, the way an SDK model object presents."""

    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        try:
            value = self._data[name]
        except KeyError:  # a real model object raises, it does not return None
            raise AttributeError(name) from None
        return _Bag(value) if isinstance(value, dict) else value


def _response(usage: dict, model="claude-haiku-4-5"):
    return _Bag({"model": model, "usage": usage, "content": []})


def test_nested_thinking_tokens_are_read():
    usage = {"input_tokens": 45, "output_tokens": 131,
             "output_tokens_details": {"thinking_tokens": 115}}
    assert anthropic_wrapper.read_reasoning(_response(usage)) == 115


def test_thinking_is_a_subset_of_output_and_never_added_to_it():
    usage = {"input_tokens": 45, "output_tokens": 131,
             "output_tokens_details": {"thinking_tokens": 115}}
    _, tokens_in, tokens_out = anthropic_wrapper.read_usage(_response(usage), {})
    assert (tokens_in, tokens_out) == (45, 131)  # not 131 + 115


def test_a_response_that_did_not_think_reads_zero():
    assert anthropic_wrapper.read_reasoning(_response({"output_tokens": 12})) == 0


def test_a_null_details_object_reads_zero_rather_than_raising():
    usage = {"output_tokens": 1, "output_tokens_details": None}
    assert anthropic_wrapper.read_reasoning(_response(usage)) == 0


def test_a_top_level_count_is_still_read_if_a_release_ever_states_one():
    usage = {"output_tokens": 131, "thinking_tokens": 115}
    assert anthropic_wrapper.read_reasoning(_response(usage)) == 115


def test_the_stream_reads_thinking_off_message_delta():
    """message_start carries `output_tokens_details: null`; the delta carries it."""
    usage = anthropic_wrapper._StreamUsage()
    anthropic_wrapper._chunk_usage(
        _Bag({"type": "message_start",
              "message": {"model": "claude-haiku-4-5",
                          "usage": {"input_tokens": 45, "output_tokens": 1,
                                    "output_tokens_details": None}}}),
        usage,
    )
    assert usage.tokens_reasoning == 0
    anthropic_wrapper._chunk_usage(
        _Bag({"type": "message_delta",
              "usage": {"output_tokens": 79,
                        "output_tokens_details": {"thinking_tokens": 64}}}),
        usage,
    )
    assert (usage.tokens_out, usage.tokens_reasoning) == (79, 64)


def test_a_guarded_call_reports_thinking_all_the_way_to_the_event():
    """The whole path, not just the reader: wrap a client and read the report."""
    reported = []

    class _Messages:
        def create(self, **kwargs):
            return _response({"input_tokens": 45, "output_tokens": 131,
                              "output_tokens_details": {"thinking_tokens": 115}})

    class _Client:
        def __init__(self):
            self.messages = _Messages()

    client = _Client()
    anthropic_wrapper.install(client, lambda *args: reported.append(args))
    client.messages.create(model="claude-haiku-4-5", max_tokens=1200)
    assert reported[0][4] == 115  # tokens_reasoning, the fifth positional
    assert reported[0][2] == 131  # tokens_out, unchanged
