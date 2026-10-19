"""PS-AAI-03j: a span says where its capability declaration came from, beside ``runbound.capability``: ``runbound.capability_source`` (``decorator``, ``mcp_annotation`` or
``undeclared``), ``runbound.capability_effective`` (only when a console row RAISED the classes), and ``runbound.service_version`` (``RUNBOUND_SERVICE_VERSION``, a comparison label).
Additive: no decision changes.
"""

import pytest

import runbound
from runbound import api, otel_names as names, spans
from test_policy_rows import FakeShared, row
from test_spans import Collector, attrs_of


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RUNBOUND_SERVICE_VERSION", raising=False)
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def span_of(collector):
    spans.exporter().flush(5.0)
    return attrs_of(collector.spans()[-1])


def test_a_decorator_declaration_is_the_source_and_nothing_else_is_added(collector):
    runbound.init(otel_endpoint=collector.url, token="k", service="acme")

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return 1

    look()
    a = span_of(collector)
    assert a[names.CAPABILITY] == "read" and a[names.CAPABILITY_SOURCE] == "decorator"
    assert names.CAPABILITY_EFFECTIVE not in a and names.SERVICE_VERSION not in a


def test_a_tool_that_declares_nothing_says_undeclared(collector):
    runbound.init(otel_endpoint=collector.url, token="k", service="acme")

    @runbound.tool(reviewed=True)
    def vague():
        return 1

    try:
        vague()
    except runbound.ExecutionRefused:
        pass
    a = span_of(collector)
    assert a[names.CAPABILITY_SOURCE] == "undeclared" and names.CAPABILITY not in a


def test_an_integration_can_say_the_declaration_is_an_mcp_annotation(collector):
    runbound.init(otel_endpoint=collector.url, token="k", service="acme")

    @runbound.tool(effects={"destructive"}, reviewed=True, effects_source="mcp_annotation")
    def remote_drop():
        return 1

    remote_drop()
    a = span_of(collector)
    assert a[names.CAPABILITY] == "destructive" and a[names.CAPABILITY_SOURCE] == "mcp_annotation"


def test_effects_source_is_validated():
    for bad in ("row", "annotation", "", 3):
        with pytest.raises(ValueError, match="effects_source"):
            runbound.tool(effects={"read"}, effects_source=bad)(lambda: 1)


def test_a_row_that_raises_the_classes_is_recorded_beside_the_declaration(collector):
    runbound.init(otel_endpoint=collector.url, token="k", service="acme")
    shared = FakeShared([row("export", risk_class="financial", lines=[{"id": "L1", "decision": "allow", "approval": None, "limits": None}], otherwise="allow")])
    api._ENGINE.shared = shared
    api._SHARED = shared

    @runbound.tool(effects={"read"}, reviewed=True)
    def export():
        return 1

    export()
    a = span_of(collector)
    assert a[names.CAPABILITY] == "read" and a[names.CAPABILITY_SOURCE] == "decorator", "the declaration is still the code's"
    assert a[names.CAPABILITY_EFFECTIVE] == "financial,read"


def test_the_deployments_version_rides_the_span_as_a_label(collector, monkeypatch):
    monkeypatch.setenv("RUNBOUND_SERVICE_VERSION", "2026.10.18-rc1")
    runbound.init(otel_endpoint=collector.url, token="k", service="acme")

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return 1

    look()
    assert span_of(collector)[names.SERVICE_VERSION] == "2026.10.18-rc1"
