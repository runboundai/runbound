"""The typed refusal every stop raises: reason, retryable, provider_called. Shown on: Getting started, Wire the loop."""

# docs: handle-execution-refused
import runbound
from runbound import ExecutionRefused

runbound.init(budget_usd=1.0, on_anomaly="raise")

caught = None
try:
    runbound.record_call("gpt-4o", 1_000_000, 1_000_000)
except ExecutionRefused as exc:
    caught = (exc.reason, exc.retryable, exc.provider_called)
# /docs

assert caught is not None, "expected the over-budget call to raise ExecutionRefused"
assert caught == ("budget", False, True), caught
