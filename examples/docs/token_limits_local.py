"""Token and step walls that work with no price table at all. Shown on: Budgets and pricing, Self-hosted."""

import runbound
from runbound import GuardrailTripped

# docs: token-limits-local
runbound.init(max_total_tokens=500_000, tokens_per_minute_limit=60_000,
                max_steps=100, on_anomaly="raise")
# /docs

# "local-model-x" has no price anywhere (built-in table or custom_prices), so
# every call here is $0.00 — and max_total_tokens still trips on the count.
tripped = None
try:
    runbound.record_call("local-model-x", tokens_in=300_000, tokens_out=250_000)
except GuardrailTripped as exc:
    tripped = exc

assert tripped is not None, "expected max_total_tokens to trip on an unpriced model"
assert tripped.anomaly.detector == "budget"

with runbound.current_session().lock:
    assert runbound.current_session().total_cost_usd == 0.0
