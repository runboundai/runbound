"""AAI-1 FR-1 (PS-AAI-03b): the refused span and the refusal event are provably the same refusal.

The span names the anomaly it records (``runbound.anomaly_id``); the anomaly names the span back (``span_id``, ``trace_id`` in its details). Both are read on the
paired record, not just checked for presence. An allowed call has no anomaly and its span names none.
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api, spans
from test_policy_rows import FakeShared, row, start
from test_spans import Collector, attrs_of


class Seen:
    """An observer that keeps every anomaly the engine reports."""

    def __init__(self) -> None:
        self.anomalies: list = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.anomalies.append(anomaly)


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


def tool(name="act"):
    @runbound.tool(name=name, effects={"read"}, reviewed=True)
    def act(size: int = 0):
        return "done"

    return act


def exported(collector):
    spans.exporter().flush(5.0)
    return collector.spans()


def test_the_refused_span_and_the_reported_refusal_name_each_other(collector):
    start(FakeShared([row("act", blocked=True)]), otel_endpoint=collector.url)
    seen = Seen()
    api._ENGINE.observers.append(seen)
    with pytest.raises(runbound.ExecutionRefused) as refused:
        tool()(size=1)
    (span,) = exported(collector)
    attrs = attrs_of(span)
    (reported,) = seen.anomalies
    assert attrs["runbound.anomaly_id"] == refused.value.anomaly.anomaly_id == reported.anomaly_id
    assert reported.details["span_id"] == span["spanId"] and reported.details["trace_id"] == span["traceId"]
    assert refused.value.anomaly.details["span_id"] == span["spanId"]


def test_two_refusals_are_two_pairs(collector):
    start(FakeShared([row("act", blocked=True)]), otel_endpoint=collector.url)
    act = tool()
    for _ in range(2):
        with pytest.raises(runbound.ExecutionRefused):
            act(size=1)
    first, second = exported(collector)
    assert attrs_of(first)["runbound.anomaly_id"] != attrs_of(second)["runbound.anomaly_id"]
    assert first["spanId"] != second["spanId"]


def test_an_allowed_call_has_a_span_and_no_anomaly(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    seen = Seen()
    api._ENGINE.observers.append(seen)
    tool()(size=1)
    (span,) = exported(collector)
    assert "runbound.anomaly_id" not in attrs_of(span) and seen.anomalies == []
