"""A bare token, with nowhere to send it yet: the process stays local. Shown on: Control plane."""

import logging

log_records: list = []


class _ListHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log_records.append(record.getMessage())


logging.getLogger("runbound").addHandler(_ListHandler())
logging.getLogger("runbound").setLevel(logging.WARNING)

import os

os.environ["RUNBOUND_TOKEN"] = "rb_live_dummy_docs_token"

# docs: fleet-init · needs token
import os

import runbound

runbound.init(token=os.environ["RUNBOUND_TOKEN"], service="support-bot",
                budget_usd=5.0, on_anomaly="raise")
# /docs

assert runbound.plane_status().mode == "local"
warnings = [m for m in log_records if "nowhere to send it yet" in m]
assert len(warnings) == 1, log_records

tripped = None
try:
    runbound.record_call("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)
except runbound.GuardrailTripped as exc:
    tripped = exc

assert tripped is not None, "detection must still work with no reachable plane"
assert tripped.anomaly.detector == "budget"
