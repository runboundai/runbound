"""Two lines guard every model call in the process. Shown on: Install, Level 1."""

from runbound import GuardrailTripped

# docs: quickstart-init
import runbound
runbound.init(budget_usd=5.0, on_anomaly="raise")   # everything below is now guarded
# /docs

# A call runbound did not make through a client still goes through the same
# budget wall — record_call() takes the same path a wrapped client's usage
# does, so this is a small, dependency-free way to push spend over $5.0.
tripped = None
try:
    runbound.record_call("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)
except GuardrailTripped as exc:
    tripped = exc

assert tripped is not None, "expected the over-budget call to raise GuardrailTripped"
assert tripped.anomaly.detector == "budget"
