"""OpenTelemetry export (``runbound[otel]``): refusals, anomalies, posture
changes and runtime changes as log records, five counters, nothing when the
packages are absent, and never a raw session key."""

import builtins

import pytest

import runbound
from runbound import api, local_events, otel
from runbound.events import Anomaly
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import key_hash

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402


@pytest.fixture
def rig():
    api._teardown_for_tests()
    logs = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    reader = InMemoryMetricReader()
    assert otel.enable(logger_provider=provider, meter_provider=MeterProvider(metric_readers=[reader]))
    yield logs, reader
    otel.disable()
    api._teardown_for_tests()


def records(logs):
    return [r.log_record for r in logs.get_finished_logs()]


def by_event(logs, name):
    return [r for r in records(logs) if r.event_name == name]


def counters(reader):
    data = reader.get_metrics_data()
    out = {}
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                out[metric.name] = [(dict(p.attributes), p.value) for p in metric.data.data_points]
    return out


def refuse(key="acct-secret-1"):
    runbound.init(on_anomaly="raise", max_actions_per_run=1)

    @runbound.tool
    def ping():
        return "pong"

    with runbound.session(key=key):
        ping()
        with pytest.raises(GuardrailTripped):
            ping()


def test_a_refusal_is_a_log_record_with_the_decision_fields_as_attributes(rig):
    logs, _ = rig
    refuse()
    [refusal] = by_event(logs, "runbound.refusal")
    attrs = dict(refusal.attributes)
    assert attrs["runbound.detector"] == "fanout"
    assert attrs["runbound.decision.verdict"] == "deny"
    assert attrs["runbound.decision.kind"] == "action"
    assert attrs["runbound.decision.boundary"]
    assert attrs["runbound.decision.level"]
    assert attrs["runbound.decision.reason"]
    assert any(k.startswith("runbound.decision.evaluation.") for k in attrs)
    assert refusal.severity_text == "ERROR"
    assert refusal.timestamp and refusal.body


def test_the_same_trip_is_also_an_anomaly_record(rig):
    logs, _ = rig
    refuse()
    [anomaly] = by_event(logs, "runbound.anomaly")
    assert dict(anomaly.attributes)["runbound.detector"] == "fanout"
    assert dict(anomaly.attributes)["runbound.reacted"] in ("raise", "door")


def test_a_raw_session_key_never_leaves_only_its_hash(rig):
    logs, _ = rig
    refuse(key="acct-secret-1")
    for record in records(logs):
        flat = repr(dict(record.attributes)) + repr(record.body)
        assert "acct-secret-1" not in flat
    assert dict(by_event(logs, "runbound.refusal")[0].attributes)["runbound.key_hash"] == key_hash("acct-secret-1")


def test_a_posture_change_is_a_log_record_and_a_counter(rig):
    logs, reader = rig
    runbound.init()
    runbound.enter_safe_mode(reason="manual test", posture="restricted")
    runbound.exit_safe_mode()
    postures = by_event(logs, "runbound.posture")
    assert [dict(p.attributes)["runbound.posture"] for p in postures] == ["restricted", "full"]
    assert dict(postures[0].attributes)["runbound.posture.source"] == "manual"
    assert dict(postures[0].attributes)["runbound.posture.from"] == "full"
    assert sum(v for _, v in counters(reader)["runbound.posture_changes"]) == 2


def test_a_runtime_change_is_a_log_record(rig):
    logs, _ = rig
    local_events.note_runtime_value("model", "gpt-4o")
    local_events.note_runtime_value("model", "gpt-4.1")
    [change] = by_event(logs, "runbound.runtime_change")
    assert dict(change.attributes) == {
        "runbound.runtime.what": "model", "runbound.runtime.from": "gpt-4o", "runbound.runtime.to": "gpt-4.1"}


def test_three_metrics_count_refusals_anomalies_and_posture_changes(rig):
    _, reader = rig
    refuse()
    runbound.enter_safe_mode(posture="restricted")
    got = counters(reader)
    assert set(got) == {"runbound.refusals", "runbound.anomalies", "runbound.posture_changes"}
    [(refusal_attrs, refusals)] = got["runbound.refusals"]
    assert refusals == 1 and refusal_attrs["runbound.detector"] == "fanout"
    assert sum(v for _, v in got["runbound.anomalies"]) == 1
    assert sum(v for _, v in got["runbound.posture_changes"]) == 1


def test_a_decision_is_not_exported_twice(rig):
    logs, _ = rig
    refuse()
    assert len(records(logs)) == 2  # the refusal and its anomaly, nothing for the decision ring


def test_disable_stops_the_export(rig):
    logs, _ = rig
    otel.disable()
    refuse()
    assert records(logs) == []


def test_enable_twice_replaces_rather_than_doubles(rig):
    logs, _ = rig
    provider = LoggerProvider()
    other = InMemoryLogRecordExporter()
    provider.add_log_record_processor(SimpleLogRecordProcessor(other))
    assert otel.enable(logger_provider=provider, meter_provider=MeterProvider())
    refuse()
    assert records(logs) == [] and len(other.get_finished_logs()) == 2


def test_a_raising_exporter_never_reaches_the_call(rig):
    logs, _ = rig

    class Boom:
        def emit(self, *a, **k):
            raise RuntimeError("collector down")

    otel._ACTIVE._logger = Boom()
    refuse()  # the refusal still raises GuardrailTripped; nothing else escapes


def test_without_the_packages_enable_is_a_no_op(monkeypatch):
    api._teardown_for_tests()
    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert otel.enable() is False
    monkeypatch.undo()
    runbound.init(on_anomaly="raise", max_actions_per_run=1)
    assert local_events._SINKS == [] and local_events._CALL_SINKS == []
    runbound.record_call("gpt-4o", tokens_in=1, tokens_out=1)  # counts nowhere, raises nothing


def test_a_key_named_in_a_sentence_is_redacted_out_of_the_body_and_the_reason(rig):
    logs, _ = rig
    anomaly = Anomaly(
        detector="budget", severity="critical", message="key acct-secret-9 spent 5.01",
        details={"decision": {"verdict": "deny", "kind": "model_call", "reason": "acct-secret-9 is over",
                              "evaluation": {"limit": 5.0, "used": 5.01, "note": "acct-secret-9"}}})
    local_events.record_anomaly("s1", anomaly, "raise", "acct-secret-9")
    for record in records(logs):
        assert "acct-secret-9" not in repr(dict(record.attributes)) + repr(record.body)
    refusal = dict(by_event(logs, "runbound.refusal")[0].attributes)
    assert refusal["runbound.decision.evaluation.limit"] == 5.0
    assert refusal["runbound.decision.evaluation.used"] == 5.01


def test_the_key_reaches_sinks_only_never_the_ring_or_on_event():
    api._teardown_for_tests()
    seen, heard = [], []
    local_events.configure(heard.append)
    local_events.add_sink(seen.append)
    local_events.record_anomaly("s1", Anomaly("loop", "warn", "m", {}), "warn", "k-1")
    assert seen[0]["key"] == "k-1"
    assert "key" not in heard[0] and "key" not in runbound.events()[0]
    api._teardown_for_tests()


# --- per guarded call: runbound.guarded_calls and runbound.estimated_usd (SDK-4b) ----------------------------------------------


def test_each_guarded_call_ticks_the_call_counter_and_adds_its_estimated_cost(rig):
    _, reader = rig
    runbound.init()
    runbound.record_call("gpt-4o", tokens_in=1000, tokens_out=500)
    runbound.record_call("gpt-4o", tokens_in=1000, tokens_out=500)
    data = counters(reader)
    [(attrs, calls)] = data["runbound.guarded_calls"]
    assert calls == 2 and attrs["runbound.model"] == "gpt-4o" and attrs["runbound.outcome"] == "ok"
    [(usd_attrs, usd)] = data["runbound.estimated_usd"]
    assert usd == pytest.approx(2 * (1000 * 2.5 + 500 * 10.0) / 1_000_000) and usd_attrs["runbound.model"] == "gpt-4o"


def test_a_call_with_no_price_counts_as_a_call_and_adds_no_cost(rig):
    _, reader = rig
    runbound.init()
    runbound.record_call("a-model-nobody-priced", tokens_in=10, tokens_out=10)
    data = counters(reader)
    assert data["runbound.guarded_calls"][0][1] == 1
    assert sum(value for _, value in data.get("runbound.estimated_usd", [])) == 0 or "runbound.estimated_usd" not in data


def test_a_failed_call_is_counted_with_outcome_error_and_costs_nothing(rig):
    _, reader = rig
    runbound.init()
    api._Hooks().error("gpt-4o", RuntimeError("boom"), 0.1, "openai@api.openai.com")
    data = counters(reader)
    [(attrs, calls)] = data["runbound.guarded_calls"]
    assert calls == 1 and attrs["runbound.outcome"] == "error" and attrs["runbound.provider"] == "openai@api.openai.com"
    assert "runbound.estimated_usd" not in data


def test_call_records_reach_sinks_only_never_the_ring_or_on_event(rig):
    seen = []
    runbound.init(on_event=seen.append)
    runbound.record_call("gpt-4o", tokens_in=10, tokens_out=10)
    assert seen == [] and runbound.events() == []


def test_a_plain_sink_never_hears_a_call_record():
    api._teardown_for_tests()
    heard = []
    local_events.add_sink(heard.append)
    runbound.init()
    runbound.record_call("gpt-4o", tokens_in=10, tokens_out=10)
    assert all(r["kind"] != "call" for r in heard)
    api._teardown_for_tests()


def test_the_call_counters_carry_no_session_key_or_content(rig):
    _, reader = rig
    runbound.init()
    with runbound.session(key="acct-secret-7"):
        runbound.record_call("gpt-4o", tokens_in=10, tokens_out=10)
    assert "acct-secret-7" not in repr(counters(reader))


def test_a_call_with_no_sink_registered_costs_nothing_and_does_nothing():
    api._teardown_for_tests()
    runbound.init()
    runbound.record_call("gpt-4o", tokens_in=10, tokens_out=10)  # no exporter: returns at once, no error
    assert local_events._CALL_SINKS == []
    api._teardown_for_tests()
