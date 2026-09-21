"""A latch that lifts itself after a while, instead of staying until clear(). Shown on: Runs and keys, Production checklist."""

import time

import runbound
from runbound import GuardrailTripped

# docs: latch-ttl
runbound.init(budget_usd=0.0005, on_anomaly="raise", latch_ttl_seconds=0.1)
# /docs

key = "user:latch-demo"
with runbound.session(key):
    try:
        runbound.record_call("gpt-4o", tokens_in=1000, tokens_out=1000)
    except GuardrailTripped:
        pass

assert runbound.is_tripped(key) is not None

time.sleep(0.15)

# Re-admitted, does not reset: the session is judged fresh, and — still over
# budget on the same cumulative counters — trips again immediately.
retrip = None
with runbound.session(key):
    try:
        runbound.record_call("gpt-4o", tokens_in=1, tokens_out=1)
    except GuardrailTripped as exc:
        retrip = exc

assert retrip is not None, "the session must re-trip once re-admitted, still over budget"
assert retrip.anomaly.detector == "budget"
