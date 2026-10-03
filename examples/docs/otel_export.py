"""Sending refusals, anomalies and posture changes to OpenTelemetry. Shown on: OpenTelemetry."""

REQUIRES = ("opentelemetry.sdk",)

# docs: otel-export
import runbound
from runbound import otel
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

logs = InMemoryLogRecordExporter()
logger_provider = LoggerProvider()
logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
reader = InMemoryMetricReader()

runbound.init(on_anomaly="raise", max_actions_per_run=1)
otel.enable(logger_provider=logger_provider, meter_provider=MeterProvider(metric_readers=[reader]))

@runbound.tool
def ping():
    return "pong"

with runbound.session(key="acct-42"):
    ping()
    try:
        ping()
    except runbound.GuardrailTripped:
        pass

for finished in logs.get_finished_logs():
    record = finished.log_record
    print(record.event_name, record.attributes["runbound.detector"])
# /docs

names = [f.log_record.event_name for f in logs.get_finished_logs()]
assert names == ["runbound.anomaly", "runbound.refusal"], names
refusal = dict(logs.get_finished_logs()[1].log_record.attributes)
assert refusal["runbound.decision.verdict"] == "deny"
assert "acct-42" not in repr(refusal)
otel.disable()
