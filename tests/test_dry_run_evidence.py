"""A dry-run row's evidence counts every call it would have refused (register C24 / J-EVD-1).

The console's evidence is the number of ``refusal`` events with ``reacted == "dry_run"``. The SDK used to send one per
session x rule x tool (the notice was told once per key), so a rule that would have stopped 40 calls showed 1. Now each
would-refuse call is its own anomaly and Decision, up to the record cap per session x rule x tool; past it they are counted
and one summary carries ``suppressed_count``. A dry run still never pages anyone: the reaction stays ``dry_run``.
"""

import pytest

import runbound
from runbound import api, engine as engine_module
from test_policy_rows import FakeShared, row, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Observer:
    def __init__(self):
        self.anomalies = []

    def on_anomaly(self, session, anomaly, reacted):
        self.anomalies.append((anomaly, reacted))

    def on_event(self, session, event):
        return None


def watch():
    observer = Observer()
    api._ENGINE.observers.append(observer)
    return observer


def dry_run_tool(**fields):
    start(FakeShared([row("look", state="dry_run", blocked=True, **fields)]))
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    return look, seen


def test_every_would_refuse_call_is_its_own_dry_run_anomaly_and_decision():
    look, seen = dry_run_tool()
    for _ in range(5):
        assert look() == "ok"  # nothing is refused
    mine = [(a, r) for a, r in seen.anomalies if a.details.get("tool") == "look"]
    assert [r for _a, r in mine] == ["dry_run"] * 5  # one per call, never a page: the reaction is dry_run
    assert all(a.details.get("rule") == "tool_blocked" for a, _r in mine)
    assert len([t for t in runbound.tool_results() if "would_refuse:tool_blocked" in t["tags"]]) == 5  # unchanged
    assert len(runbound.decisions()) >= 5


def test_past_the_record_cap_they_are_counted_and_one_summary_carries_the_count_when_the_session_exits():
    cap = engine_module.REFUSAL_RECORD_CAP
    look, seen = dry_run_tool()
    with runbound.session("run-1"):
        for _ in range(cap + 20):
            look()
    singles = [a for a, _r in seen.anomalies if a.details.get("tool") == "look" and "suppressed_count" not in a.details]
    summaries = [a for a, _r in seen.anomalies if a.details.get("suppressed_count")]
    assert len(singles) == cap
    assert [s.details["suppressed_count"] for s in summaries] == [20]
    assert {r for _a, r in seen.anomalies} == {"dry_run"}


def test_two_sessions_have_separate_record_budgets():
    look, seen = dry_run_tool()
    for key in ("a", "b"):
        with runbound.session(key):
            for _ in range(3):
                look()
    assert len([a for a, _r in seen.anomalies if a.details.get("tool") == "look"]) == 6


def test_a_dry_run_quota_that_would_refuse_is_counted_on_every_call():
    quota = {"limit": 1, "window": "minute", "per": "service"}
    start(FakeShared([row("look", state="dry_run", quota=quota)], take=lambda body: (200, {"granted": False, "retry_after_s": 9})))
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    for _ in range(4):
        assert look() == "ok"
    assert len([a for a, r in seen.anomalies if a.details.get("rule") == "quota_exceeded" and r == "dry_run"]) == 4


def test_an_enforced_row_still_records_every_refusal_as_before():
    start(FakeShared([row("look", blocked=True)]))
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    for _ in range(3):
        with pytest.raises(runbound.ExecutionRefused):
            look()
    assert len([a for a, r in seen.anomalies if a.details.get("tool") == "look" and r == "blocked"]) == 3


# --- the conditions that keep a dry run a dry run -----------------------------------------------------------------------


def test_a_dry_run_record_is_dry_run_everywhere_and_moves_no_enforcement_counter():
    cap = engine_module.REFUSAL_RECORD_CAP
    look, seen = dry_run_tool()
    with runbound.session("run-dry") as state:
        for _ in range(cap + 5):
            assert look() == "ok"
        refused, executed = state.refused_actions, state.executed_actions
    assert {r for _a, r in seen.anomalies if _a.details.get("tool") == "look"} == {"dry_run"}  # every record AND the summary
    assert refused == 0 and executed == cap + 5  # a dry run changes nothing enforcement reads
    assert not [e for e in runbound.events() if e.get("kind") == "refusal"]  # no refusal event locally
    assert {t["outcome"] for t in runbound.tool_results() if t["tool"] == "look"} == {"ok"}


def test_the_summary_says_it_is_a_dry_run_summary():
    look, seen = dry_run_tool()
    with runbound.session("run-sum"):
        for _ in range(engine_module.REFUSAL_RECORD_CAP + 3):
            look()
    (summary,) = [(a, r) for a, r in seen.anomalies if a.details.get("suppressed_count")]
    anomaly, reacted = summary
    assert reacted == "dry_run" and anomaly.details["dry_run"] is True and anomaly.details["suppressed_count"] == 3
    assert "dry-run" in anomaly.message and "would-refuse" in anomaly.message and "ran" in anomaly.message


def test_the_cap_is_separate_per_session_rule_and_tool():
    cap = engine_module.REFUSAL_RECORD_CAP
    start(FakeShared([row("one", state="dry_run", blocked=True), row("two", state="dry_run", blocked=True)]))
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    def one():
        return "ok"

    @runbound.tool(effects={"read"}, reviewed=True)
    def two():
        return "ok"

    with runbound.session("s1"):
        for _ in range(cap + 2):
            one()
        for _ in range(cap + 1):
            two()
    summaries = {a.details["tool"]: a.details["suppressed_count"] for a, _r in seen.anomalies if a.details.get("suppressed_count")}
    assert summaries == {"one": 2, "two": 1}  # each tool has its own budget
    singles = [a for a, _r in seen.anomalies if "suppressed_count" not in a.details]
    assert len([a for a in singles if a.details["tool"] == "one"]) == cap and len([a for a in singles if a.details["tool"] == "two"]) == cap


def test_the_span_verdict_of_a_dry_run_call_stays_would_refuse_never_refuse():
    from test_spans import Collector, attrs_of
    from runbound import otel_names as names, spans

    collector = Collector()
    try:
        start(FakeShared([row("look", state="dry_run", blocked=True)]), otel_endpoint=collector.url)

        @runbound.tool(effects={"read"}, reviewed=True)
        def look():
            return "ok"

        for _ in range(3):
            look()
        spans.exporter().flush(5.0)
        verdicts = [(attrs_of(s)[names.DECISION_VERDICT], attrs_of(s)[names.OUTCOME]) for s in collector.spans()]
        assert verdicts == [("would_refuse", "ok")] * 3
    finally:
        collector.close()


def test_a_row_promoted_to_enforced_mid_session_does_not_borrow_the_dry_run_budget():
    from runbound.refusal_log import RefusalLog
    from runbound.events import Anomaly

    log = RefusalLog()
    def a():
        return Anomaly("policy", "warn", "x", {"tool": "t", "rule": "tool_blocked"})
    assert all(log.admit("s", a(), "dry_run") for _ in range(engine_module.REFUSAL_RECORD_CAP))
    assert log.admit("s", a(), "blocked")  # the enforced refusals start their own count
