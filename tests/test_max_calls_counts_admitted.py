"""``max_calls`` counts calls that were admitted, not attempts (V5a).

A call a constraint, a validator, a capability class or a person refused never ran; it must not use up
the tool's allowance. ``tool_calls()`` still reports attempts (a refused attempt was made).
"""

import threading
import time

import pytest

import runbound
from runbound import api
from runbound.exceptions import ExecutionRefused


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    runbound.init()
    yield
    api._teardown_for_tests()


def test_refused_attempts_do_not_use_up_max_calls():
    @runbound.tool(effects={"read"}, reviewed=True, max_calls=2, constraint=lambda c: c.args[0] <= 100)
    def credit(amount: int):
        return amount

    for refused in (500, 600, 700):  # three attempts the constraint refuses
        with pytest.raises(ExecutionRefused) as caught:
            credit(refused)
        assert caught.value.reason == "policy"
    assert credit(1) == 1 and credit(2) == 2  # both still inside max_calls=2
    with pytest.raises(ExecutionRefused) as caught:
        credit(3)
    assert "limit is 2" in str(caught.value)
    assert runbound.tool_calls()["credit"] == 6  # attempts are still reported as attempts


def test_the_limit_is_exactly_n_admitted_calls():
    @runbound.tool(effects={"read"}, reviewed=True, max_calls=1)
    def once():
        return "ok"

    assert once() == "ok"
    with pytest.raises(ExecutionRefused):
        once()


def test_a_refusal_at_the_limit_does_not_move_the_count():
    @runbound.tool(effects={"read"}, reviewed=True, max_calls=1)
    def once():
        return "ok"

    once()
    for _ in range(5):
        with pytest.raises(ExecutionRefused) as caught:
            once()
        assert "called 2x" in str(caught.value)  # the attempt being judged is 2, however many were refused


def test_concurrent_calls_cannot_both_take_the_last_unit():
    gate = threading.Barrier(8)
    ran = []

    # a slow gate hook keeps every call inside the chain together, after the max_calls check
    @runbound.tool(effects={"read"}, reviewed=True, max_calls=1, gate_hooks=[lambda call: time.sleep(0.2)])
    def last():
        ran.append(1)
        return "ok"

    def attempt():
        gate.wait()
        try:
            last()
        except ExecutionRefused:
            pass

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ran) == 1
