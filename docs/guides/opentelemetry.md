# OpenTelemetry

[← Docs](../README.md)

Send what runbound already records in this process to your own
OpenTelemetry pipeline. Nothing is sent unless you call `otel.enable()`.

```bash
pip install "runbound[otel]"   # opentelemetry-api and opentelemetry-sdk
```

```python
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
```

That prints `runbound.anomaly fanout` and then `runbound.refusal fanout`.
In your own service you pass nothing: `otel.enable()` uses the global
OpenTelemetry providers, and your usual exporter (OTLP, console) takes it
from there.

## What is exported

One **log record** for each of:

| `event_name` | when | notable attributes |
|---|---|---|
| `runbound.anomaly` | a detector fired | `runbound.detector`, `runbound.severity`, `runbound.reacted` |
| `runbound.refusal` | a call was refused | `runbound.detector`, `runbound.reacted`, the Decision |
| `runbound.posture` | a posture was narrowed or lifted | `runbound.posture`, `.from`, `.scope`, `.source`, `.level` |
| `runbound.runtime_change` | the model, provider or a version moved | `runbound.runtime.what`, `.from`, `.to` |

When the anomaly or refusal carries a [Decision](handling-refusals.md), its
fields are attributes: `runbound.decision.verdict`, `.kind`, `.boundary`,
`.level`, `.reason`, `.detector`, `.policy_version`, and the numbers behind
it as `runbound.decision.evaluation.<name>` (`limit`, `used`, `estimate`,
whichever apply). Severity is `ERROR` for a critical anomaly and a refusal,
`WARN` for a warning, `INFO` for the other two.

Five **counters**: `runbound.refusals` (by detector, boundary and level),
`runbound.anomalies` (by detector and severity), `runbound.posture_changes` (by posture and source),
`runbound.guarded_calls` (one per model call runbound guarded, by model, provider and outcome `ok` or `error`) and
`runbound.estimated_usd` (the dollars runbound estimated for those calls, by model and provider; `runbound.priced` is
`estimated` when the price was a fallback). A call with no price adds a call and no dollars. Neither carries a session key.

## What is never exported

The same promise as everywhere else in runbound: no prompt, no reply, no
call argument. A session key leaves only as `runbound.key_hash`, the same
sha256 the control plane receives, and is redacted out of any sentence that
names it.

## Without the package, and when it fails

`otel.enable()` returns `False` and does nothing when the OpenTelemetry
packages are not installed; `import runbound` never needs them. Like every
observer in runbound it is fail-open: an exporter that raises is logged and
never reaches the call that produced the record. `otel.disable()` stops it.

---
