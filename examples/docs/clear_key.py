"""Checking a latch without tripping it, then forgiving it explicitly. Shown on: Runs and keys."""

import runbound
from runbound import GuardrailTripped

runbound.init(budget_usd=0.0005, on_anomaly="raise")
key = "user:99"

with runbound.session(key):
    try:
        runbound.record_call("gpt-4o", tokens_in=1000, tokens_out=1000)
    except GuardrailTripped:
        pass

# docs: clear-key
tripped_before = runbound.is_tripped(key)
runbound.clear(key)
tripped_after = runbound.is_tripped(key)
# /docs

assert tripped_before is not None and tripped_before.detector == "budget"
assert tripped_after is None, "clear() must forgive the latch"

# A cleared key starts fresh: the next block is served rather than refused.
served = None
with runbound.session(key):
    served = "ok"
assert served == "ok"
