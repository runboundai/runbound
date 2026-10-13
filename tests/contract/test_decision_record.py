"""What a decision records (TPD §4c.6, §12 item 6). Ids O-REC-*. Passing since TR2-O2 (register C6, FIXED in R2).

The record is the primary reason code, ``matched_rule_ids`` (possibly empty), the policy version, the service-default revision, the policy
state, the contributing restrictions and ``sdk_version``, on the span and in the refusal (WIRE-TR2 §7 and §10g). The refusal carries them in
``exc.decision.evaluation`` (and in the anomaly's details, which is what the plane stores); the span as the attributes named below.
"""

import pytest

import runbound
from runbound import api, spans
from test_policy_rows import FakeShared, row, start
from test_spans import Collector, attrs_of

pytestmark = [pytest.mark.contract(item="§12.6")]

KEYS = ("reason_code", "policy_state", "policy_version", "matched_rule_ids", "service_default_revision", "contributors", "sdk_version")
SPAN = {
    "reason_code": "runbound.decision.reason_code", "policy_state": "runbound.decision.policy_state",
    "policy_version": "runbound.decision.policy_version", "matched_rule_ids": "runbound.decision.matched_rule_ids",
    "service_default_revision": "runbound.decision.service_default_revision", "contributors": "runbound.decision.contributors",
    "sdk_version": "runbound.sdk.version",
}


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


def _blocked_tool():
    @runbound.tool(effects={"read"}, reviewed=True)
    def wire():
        return "sent"

    return wire


def test_o_rec_1_a_refusal_carries_the_full_record():
    start(FakeShared([row("wire", blocked=True)]))
    with pytest.raises(runbound.ExecutionRefused) as refused:
        _blocked_tool()()
    evaluation = refused.value.decision.evaluation
    assert all(key in evaluation for key in KEYS), sorted(evaluation)
    assert evaluation["sdk_version"] == runbound.__version__
    assert evaluation["reason_code"] == "tool_blocked" == refused.value.reason  # an existing code, verbatim
    assert evaluation["matched_rule_ids"] == ["wire#L1"] and evaluation["contributors"] == ["row:L1"]
    details = refused.value.anomaly.details  # the plane stores the record from the anomaly
    assert all(key in details for key in KEYS), sorted(details)


def test_o_rec_2_the_span_of_a_refused_call_carries_the_full_record(collector):
    start(FakeShared([row("wire", blocked=True)]), otel_endpoint=collector.url)
    with pytest.raises(runbound.ExecutionRefused):
        _blocked_tool()()
    spans.exporter().flush(5.0)
    (span,) = collector.spans()
    attrs = attrs_of(span)
    # service_default_revision is null until service defaults exist (TR2-A2/O3), and a null is not an attribute
    missing = [name for key, name in SPAN.items() if key != "service_default_revision" and name not in attrs]
    assert not missing, (missing, attrs)
    assert attrs[SPAN["reason_code"]] == "tool_blocked" and attrs[SPAN["matched_rule_ids"]] == ["wire#L1"]
    assert attrs[SPAN["sdk_version"]] == runbound.__version__


def test_o_rec_3_a_fallback_decision_has_an_empty_matched_list_and_a_refusal_may_have_several_contributors(collector):
    start(FakeShared([row("wire", blocked=True)]), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    def plain():
        return "ok"

    @runbound.tool(effects={"read"}, blocked=True)
    def wire():  # blocked by the code AND by the row: two contributors
        return "sent"

    plain()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        wire()
    spans.exporter().flush(5.0)
    by_tool = {attrs_of(s)["gen_ai.tool.name"]: attrs_of(s) for s in collector.spans()}
    assert by_tool["plain"].get(SPAN["matched_rule_ids"]) == []  # a fallback decision matched no rule, and says so
    assert len(refused.value.decision.evaluation.get("contributors", ())) >= 2
    assert refused.value.decision.evaluation["contributors"] == ["code:blocked", "row:L1"]
    assert by_tool["plain"][SPAN["reason_code"]] == "fallback" and by_tool["plain"][SPAN["contributors"]] == ["default:service"]
