"""A blocked tool has one refusal reason, ``tool_blocked``, whether code or a console row blocked it."""

import pytest

import runbound
from runbound import api
from runbound.exceptions import ExecutionRefused
from runbound.policy_rows import Row, enforce


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    runbound.init()
    yield
    api._teardown_for_tests()


def test_blocked_true_in_code_refuses_with_tool_blocked():
    @runbound.tool(effects={"read"}, blocked=True)
    def drop_table():
        return "dropped"

    with pytest.raises(ExecutionRefused) as caught:
        drop_table()
    assert caught.value.reason == "tool_blocked"
    assert not caught.value.retryable


def test_a_deny_list_entry_refuses_with_tool_blocked():
    runbound.init(tool_policy=runbound.ToolPolicy(deny=["shell"]))

    @runbound.tool(effects={"read"}, reviewed=True)
    def shell():
        return "ran"

    with pytest.raises(ExecutionRefused) as caught:
        shell()
    assert caught.value.reason == "tool_blocked"


def test_a_row_block_and_a_code_block_give_the_same_reason():
    engine, session = api._active()
    call = runbound.ToolCall("wire", (), {}, None, {})
    with pytest.raises(ExecutionRefused) as caught:
        enforce(engine, session, call, Row(tool="wire", blocked=True), [])
    assert caught.value.reason == "tool_blocked"


def test_the_other_code_rules_keep_the_policy_reason():
    @runbound.tool(effects={"read"}, reviewed=True, constraint=lambda call: False)
    def picky():
        return "ran"

    with pytest.raises(ExecutionRefused) as caught:
        picky()
    assert caught.value.reason == "policy"
