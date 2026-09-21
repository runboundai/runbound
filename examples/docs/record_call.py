"""Recording inference runbound did not make itself. Shown on: Self-hosted."""

import runbound
from runbound import GuardrailTripped

runbound.init(max_total_tokens=1500, on_anomaly="raise")

# docs: record-call
runbound.record_call("llama-3.1-8b", tokens_in=812, tokens_out=210,
                       duration_s=1.9, provider="llama.cpp@local")
# /docs

# The first call recorded 1022 tokens, under the 1500 cap; an identical
# second call pushes the running total to 2044 and trips on tokens.
tripped = None
try:
    runbound.record_call("llama-3.1-8b", tokens_in=812, tokens_out=210,
                           duration_s=1.9, provider="llama.cpp@local")
except GuardrailTripped as exc:
    tripped = exc

assert tripped is not None, "expected the second record_call to trip the budget detector"
assert tripped.anomaly.detector == "budget"
