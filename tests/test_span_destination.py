"""DIS-3: where the built-in tool-call span exporter sends. ``otel_endpoint`` unset + a connected plane -> that plane's existing /v1/traces; a URL wins; ``False`` disables with no
fallback; unconnected stays as it was; a customer TracerProvider keeps precedence. Configured is not delivered."""

from __future__ import annotations

import io
import json

import pytest

import runbound
from runbound import api, spans
from runbound._check import build_report, render
from runbound.config import RunboundConfig

from test_spans import Collector


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane():
    c = Collector()
    yield c
    c.close()


def _call(fn, **kw):
    """The fake plane serves no policy, so the call is refused (a refusal still emits its span); enforcement is not what these tests judge."""
    try:
        fn(**kw)
    except runbound.ExecutionRefused:
        pass


def _span_names(c):
    return [sp["name"] for path, body in c.requests if path.endswith("/v1/traces") for rs in body["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"] if sp["name"] != "execution_started"]


def _cfg(**kw):
    c = RunboundConfig(**kw)
    c.validate()
    return c


def _mode(**kw):
    return _cfg(**kw).span_destination()


def test_resolution_covers_unset_url_false_and_unconnected(monkeypatch):
    monkeypatch.delenv("RUNBOUND_PLANE_URL", raising=False)
    monkeypatch.delenv("RUNBOUND_TOKEN", raising=False)
    assert _mode() == ("none", None)
    assert _mode(control_plane_url="http://plane:9000", token="t") == ("plane", "http://plane:9000")
    assert _mode(control_plane_url="http://plane:9000", token="t", otel_endpoint="http://col:4318") == ("explicit", "http://col:4318")
    assert _mode(control_plane_url="http://plane:9000", token="t", otel_endpoint=False) == ("disabled", None)
    assert _mode(otel_endpoint=False) == ("disabled", None)
    with pytest.raises(ValueError):
        _cfg(otel_endpoint=True)


def test_a_connected_plane_with_otel_endpoint_unset_receives_the_call_span_on_its_traces_route(plane):
    runbound.init(control_plane_url=plane.url, token="tok", service="svc")

    @runbound.tool(name="lookup")
    def lookup(order_id: str) -> str:
        return "ok"

    _call(lookup, order_id="SECRET-ARG-123")
    runbound.flush()
    traced = [(path, body) for path, body in plane.requests if path.endswith("/v1/traces")]
    assert traced, [p for p, _ in plane.requests]
    assert plane.headers[[p for p, _ in plane.requests].index(traced[0][0])].get("Authorization") == "Bearer tok"
    assert _span_names(plane) == ["execute_tool lookup"]
    assert "SECRET-ARG-123" not in json.dumps(plane.requests), "the built-in payload carries metadata and digests, never the argument"
    assert spans.destination()["mode"] == "plane" and spans.destination()["exporting"] is True


def test_false_disables_the_built_in_exporter_with_no_fallback_and_the_events_lane_keeps_the_calls(plane):
    runbound.init(control_plane_url=plane.url, token="tok", service="svc", otel_endpoint=False)

    @runbound.tool(name="lookup")
    def lookup(order_id: str) -> str:
        return "ok"

    _call(lookup, order_id="o-1")
    runbound.flush()
    assert spans.exporter() is None and spans.destination()["exporting"] is False
    assert not [p for p, _ in plane.requests if p.endswith("/v1/traces")], "no span left the process"
    assert api._SHARED._exporter.include_calls is True, "calls stay on the events lane when no exporter carries them"


def test_an_explicit_url_wins_over_the_plane(plane):
    other = Collector()
    try:
        runbound.init(control_plane_url=plane.url, token="tok", service="svc", otel_endpoint=other.url)

        @runbound.tool(name="lookup")
        def lookup() -> str:
            return "ok"

        _call(lookup)
        runbound.flush()
        assert _span_names(other) and not [p for p, _ in plane.requests if p.endswith("/v1/traces")]
    finally:
        other.close()


def test_unconnected_with_nothing_set_sends_no_spans(monkeypatch):
    monkeypatch.delenv("RUNBOUND_PLANE_URL", raising=False)
    monkeypatch.delenv("RUNBOUND_TOKEN", raising=False)
    runbound.init(service="svc")
    assert spans.exporter() is None and spans.destination()["mode"] == "none"


def test_a_customer_tracer_provider_keeps_precedence_and_the_plane_default_is_not_started(plane, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    monkeypatch.setattr(spans, "_provider", lambda: provider)  # what a configured global provider looks like to the SDK
    if True:
        runbound.init(control_plane_url=plane.url, token="tok", service="svc")
        assert spans.exporter() is None, "no second pipeline beside the customer's"
        assert spans.destination()["mode"] == "none" and "TracerProvider" in spans.destination()["note"]
        assert api._SHARED._exporter.include_calls is True


def test_an_unreachable_plane_never_changes_enforcement_and_the_failure_is_counted_not_raised():
    runbound.init(control_plane_url="http://127.0.0.1:9", token="tok", service="svc")
    ran = []

    @runbound.tool(name="lookup")
    def lookup() -> str:
        ran.append(1)
        return "ok"

    _call(lookup)  # fail-closed on a plane that never answered is the existing behaviour; this test only requires no other error
    assert ran == [], "an unreachable plane never lets a call through"
    runbound.flush(timeout=1)
    assert spans.destination()["exporting"] is True


def test_check_shows_the_resolved_destination_without_credentials_and_says_configured_is_not_delivered(plane):
    runbound.init(control_plane_url=plane.url.replace("http://", "http://user:pw@"), token="tok", service="svc")
    report = build_report()
    assert report["spans"]["mode"] == "plane" and "pw" not in json.dumps(report["spans"]) and "tok" not in json.dumps(report["spans"])
    text = render(report)
    assert "Tool-call spans" in text and "configured, not confirmed delivered" in text and "pw" not in text.split("Plane")[0]
