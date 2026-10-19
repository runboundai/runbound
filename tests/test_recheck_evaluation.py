"""AAI-1 D1 (PS-AAI-03d): the execution-entry re-check of an approved call is kept as an evaluation of its own, under ``runbound.recheck.*``.

Additive to WIRE-VPC 12e: a call that ran still carries no ``recheck_*`` keys, a refusal still carries them; the passed (or refused) re-check ALSO appears as
``runbound.recheck.evaluation_id`` / ``.outcome`` / what it read, with its own id (different from the admission's).
"""

import pytest

import runbound
from runbound import api, spans
from test_grants import pay_tool
from test_policy_rows import row, start
from test_recheck_phase import Moving, asking, refused_pay
from test_spans import Collector, attrs_of

R = "runbound.recheck."


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def last_span(collector):
    spans.exporter().flush(5.0)
    return attrs_of(collector.spans()[-1])


def test_a_passed_recheck_is_kept_as_its_own_evaluation_and_the_old_keys_stay_absent(collector):
    start(Moving([asking(2)], after_rows=[asking(3)]), otel_endpoint=collector.url)  # a promote lands during the wait; the new revision still lets the call run
    pay, ran = pay_tool()
    assert pay(amount=1.0) == "paid" and ran == [1.0]
    attrs = last_span(collector)
    assert attrs[R + "outcome"] == "passed" and int(attrs[R + "rule_revision"]) == 3 and attrs[R + "cfg_hash"] == "cfg-2" and int(attrs[R + "policy_version"]) == 9
    assert attrs[R + "evaluation_id"] != attrs["runbound.evaluation_id"], "the re-check is a second evaluation with its own id"
    assert not [name for name in attrs if "recheck_" in name], "WIRE-VPC 12e: a call that ran carries no recheck_* attributes"
    assert int(attrs["runbound.decision.rule_revision"]) == 2, "what it was approved under is still the decision record"


def test_a_refused_recheck_keeps_the_old_keys_and_also_the_evaluation_entry(collector):
    shared = Moving([asking(2)], after_rows=[row("pay", rule_revision=3, blocked=True)])  # the tool is blocked during the wait: the call is refused
    start(shared, otel_endpoint=collector.url)
    evaluation = refused_pay(shared)
    assert evaluation["recheck_cfg_hash"] == "cfg-2", "the old contract is unchanged"
    attrs = last_span(collector)
    assert attrs[R + "outcome"] == "refused" and attrs[R + "reason"] == "approval_superseded"
    assert attrs[R + "evaluation_id"] != attrs["runbound.evaluation_id"]
    assert attrs["runbound.decision.recheck_cfg_hash"] == "cfg-2"


def test_a_call_that_was_never_asked_has_no_recheck_entry(collector):
    start(Moving([]), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return 1

    look()
    assert not [name for name in last_span(collector) if name.startswith(R)]
