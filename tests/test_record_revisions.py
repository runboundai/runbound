"""VPC-B6 (VPC-05-3, M12 SDK half; R1; R-C): a decision record names what it was decided under and by what.

* a row's explicit ``otherwise`` is the contributor ``otherwise:<decision>``; ``default:service`` is said only when the service defaults decide;
* a limit that refused the call is a contributor (``limit:quota``, ``limit:session_cap``, ``limit:repetition``);
* the record carries the ``rule_revision`` the row was served under and the ``cfg_hash`` the worker held, on the refusal and on the span, and only when known;
* every call a revision being tried was evaluated on carries ``candidate_outcome`` (allow / ask / deny / unknown) and the candidate's revision.

Nothing here names a tool or a value of the product: the names are this file's own."""

from __future__ import annotations

import pytest

import runbound
from runbound import api, lines, spans
from test_policy_rows import FakeShared, row, start
from test_service_defaults import Shared as DefaultsShared
from test_service_defaults import served as served_defaults
from test_spans import Collector, attrs_of

ASK_BIG = {"id": "L1", "decision": "ask", "when": {"all": [{"arg": "size", "op": ">", "value": 1000}]}}
DENY_BIG = {"id": "L1", "decision": "deny", "when": {"all": [{"arg": "size", "op": ">", "value": 1000}]}}
ATTR = {"rule_revision": "runbound.decision.rule_revision", "cfg_hash": "runbound.decision.cfg_hash", "outcome": "runbound.decision.candidate_outcome",
        "candidate_revision": "runbound.decision.candidate_revision", "candidate_of": "runbound.decision.candidate_of", "contributors": "runbound.decision.contributors"}


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


class Shared(DefaultsShared):
    cfg = None

    def boot_id(self):
        return "boot-test"

    def served_cfg(self):
        return self.cfg


def tool(effects=("read",)):
    @runbound.tool(effects=set(effects), reviewed=True)
    def act(size: int = 0):
        return "done"

    return act


def span_attrs(collector):
    spans.exporter().flush(5.0)
    return attrs_of(collector.spans()[-1])


def contributors_of(act, collector, **kwargs):
    """The contributors recorded for one call, whether it ran (its span) or was refused (the refusal's record)."""
    try:
        act(**kwargs)
    except runbound.ExecutionRefused as refused:
        return refused.decision.evaluation["contributors"]
    return span_attrs(collector)[ATTR["contributors"]]


def refused_with(act, **kwargs):
    with pytest.raises(runbound.ExecutionRefused) as refused:
        act(**kwargs)
    return refused.value


# --- the otherwise mislabel (VPC-05-3, M12 SDK half) ------------------------------------------------------------------------------------


@pytest.mark.parametrize("otherwise", ["allow", "ask", "deny"])
def test_vpc_05_3_an_explicit_otherwise_is_the_tools_own_not_the_service_default(otherwise, collector):
    start(Shared([row("act", lines=[DENY_BIG], otherwise=otherwise)], defaults=served_defaults()), otel_endpoint=collector.url)
    act = tool(("financial",))  # the service default for a financial tool would be ask: the row's otherwise must still be what is named
    assert contributors_of(act, collector, size=5) == [f"otherwise:{otherwise}"]  # no line matches, so the otherwise decides


def test_m12_an_inherited_default_is_named_as_the_service_default_and_never_as_an_otherwise(collector):
    start(Shared([row("act", lines=[DENY_BIG], otherwise="inherit")], defaults=served_defaults()), otel_endpoint=collector.url)
    contributors = contributors_of(tool(("financial",)), collector, size=5)  # no line matches; inherit hands it to the service defaults (financial: ask)
    assert "default:service" in contributors and not any(c.startswith("otherwise:") for c in contributors)


def test_m12_a_matching_line_is_a_row_contributor_and_the_otherwise_is_not_named(collector):
    start(Shared([row("act", lines=[DENY_BIG], otherwise="allow")]), otel_endpoint=collector.url)
    refused = refused_with(tool(), size=5000)
    assert refused.decision.evaluation["contributors"] == ["row:L1"]


# --- limits that refused ----------------------------------------------------------------------------------------------------------------


def test_a_session_cap_that_refused_is_a_limit_contributor():
    start(Shared([row("act", max_calls_per_session=1)]))
    act = tool()
    act()
    refused = refused_with(act)
    assert refused.reason == "session_max_calls" and "limit:session_cap" in refused.decision.evaluation["contributors"]
    assert refused.anomaly.details["contributors"].count("limit:session_cap") == 1, "the plane stores the record from the anomaly"


def test_a_repetition_control_that_refused_is_a_limit_contributor():
    start(Shared([row("act", lines=[{"id": "L1", "decision": "allow", "repetition": {"max": 1, "scope": "session"}}])]))
    act = tool()
    act(size=1)
    refused = refused_with(act, size=1)
    assert refused.reason == "repetition_exceeded" and "limit:repetition" in refused.decision.evaluation["contributors"]


def test_a_quota_that_refused_is_a_limit_contributor():
    start(Shared([row("act", quota={"limit": 1, "window": "minute", "per": "service"})],
                 take=lambda body: (200, {"granted": False, "retry_after_s": 5})))
    refused = refused_with(tool())
    assert refused.reason == "quota_exceeded" and "limit:quota" in refused.decision.evaluation["contributors"]


def test_a_refusal_for_another_reason_adds_no_limit_contributor():
    start(Shared([row("act", blocked=True)]))
    assert not any(c.startswith("limit:") for c in refused_with(tool()).decision.evaluation["contributors"])


# --- rule revision and cfg hash ------------------------------------------------------------------------------------------------------


def test_the_record_names_the_rule_revision_and_the_configuration_on_the_refusal_and_the_span(collector):
    shared = Shared([row("act", lines=[DENY_BIG], otherwise="allow", rule_revision=3)])
    shared.cfg = "cfg-served-1"
    start(shared, otel_endpoint=collector.url)
    refused = refused_with(tool(), size=5000)
    evaluation = refused.decision.evaluation
    assert (evaluation["rule_revision"], evaluation["cfg_hash"]) == (3, "cfg-served-1") and refused.anomaly.details["rule_revision"] == 3
    attrs = span_attrs(collector)
    assert (int(attrs[ATTR["rule_revision"]]), attrs[ATTR["cfg_hash"]]) == (3, "cfg-served-1")  # OTLP carries an integer attribute as a string
    assert evaluation["policy_version"] == 7, "the generation is the existing policy_version"
    assert evaluation["boot_id"] == "boot-test" and attrs["runbound.decision.boot_id"] == "boot-test", "which process decided it"


def test_a_worker_served_neither_records_exactly_what_it_always_did(collector):
    start(Shared([row("act", blocked=True)]), otel_endpoint=collector.url)
    evaluation = refused_with(tool()).decision.evaluation
    assert "rule_revision" not in evaluation and "cfg_hash" not in evaluation
    assert ATTR["rule_revision"] not in span_attrs(collector) and ATTR["cfg_hash"] not in span_attrs(collector)


# --- candidate_outcome (R-C) ---------------------------------------------------------------------------------------------------------


def candidate(decision_lines, revision=4, otherwise="allow"):
    return {"state": "dry_run", "version": 9, "rule_revision": revision, "lines": decision_lines, "otherwise": otherwise}


@pytest.mark.parametrize("size,outcome", [(5, "allow"), (2000, "ask")])
def test_every_call_a_candidate_was_evaluated_on_carries_its_outcome_and_revision_not_only_the_refused_ones(size, outcome, collector):
    start(Shared([row("act", rule_revision=2, lines=[{"id": "L1", "decision": "allow"}], candidate=candidate([ASK_BIG]))]), otel_endpoint=collector.url)
    assert tool()(size=size) == "done"  # the candidate never decides: the call ran
    attrs = span_attrs(collector)
    assert (attrs[ATTR["outcome"]], int(attrs[ATTR["candidate_revision"]]), attrs[ATTR["candidate_of"]]) == (outcome, 4, "rule")


def test_a_candidate_that_would_deny_says_deny_and_the_call_still_runs(collector):
    start(Shared([row("act", rule_revision=2, lines=[{"id": "L1", "decision": "allow"}], candidate=candidate([DENY_BIG]))]), otel_endpoint=collector.url)
    assert tool()(size=5000) == "done"
    assert span_attrs(collector)[ATTR["outcome"]] == "deny"


def test_a_candidate_whose_condition_cannot_be_judged_says_unknown(collector, monkeypatch):
    monkeypatch.delenv("RUNBOUND_ENVIRONMENT", raising=False)  # the context value the condition reads is not stated by this deployment
    asks_in_prod = {"id": "L1", "decision": "ask", "when": {"all": [{"ctx": "environment", "op": "equals", "value": "prod"}]}}
    start(Shared([row("act", rule_revision=2, lines=[{"id": "L1", "decision": "allow"}], candidate=candidate([asks_in_prod]))]), otel_endpoint=collector.url)
    assert tool()(size=1) == "done"  # the candidate never decides, whatever it could not judge
    assert span_attrs(collector)[ATTR["outcome"]] == "unknown"


def test_a_row_served_in_dry_run_is_the_revision_being_tried(collector):
    start(Shared([row("act", state="dry_run", rule_revision=6, lines=[ASK_BIG], otherwise="allow")]), otel_endpoint=collector.url)
    assert tool()(size=2000) == "done"
    attrs = span_attrs(collector)
    assert (attrs[ATTR["outcome"]], int(attrs[ATTR["candidate_revision"]])) == ("ask", 6)


def test_a_service_defaults_candidate_is_recorded_as_such(collector):
    defaults = served_defaults(revision=4)
    defaults["candidate"] = served_defaults(state="dry_run", revision=5, decision={"financial": "deny"})
    start(Shared([], defaults=defaults), otel_endpoint=collector.url)
    assert tool(("financial",))() == "done"  # the enforced defaults ask, the fake approver approves, so it runs; the candidate defaults would have denied it
    attrs = span_attrs(collector)
    assert (attrs[ATTR["outcome"]], int(attrs[ATTR["candidate_revision"]]), attrs[ATTR["candidate_of"]]) == ("deny", 5, "defaults")


def test_a_call_with_no_revision_being_tried_carries_no_candidate_facts(collector):
    start(Shared([row("act", lines=[{"id": "L1", "decision": "allow"}], rule_revision=2)]), otel_endpoint=collector.url)
    assert tool()(size=1) == "done"
    attrs = span_attrs(collector)
    assert ATTR["outcome"] not in attrs and ATTR["candidate_revision"] not in attrs


def test_the_candidate_never_decides_even_when_it_would_deny():
    start(Shared([row("act", rule_revision=2, lines=[{"id": "L1", "decision": "allow"}], candidate=candidate([DENY_BIG]))]))
    assert tool()(size=5000) == "done"
