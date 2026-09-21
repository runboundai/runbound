"""Pricing a model the built-in table has never heard of. Shown on: Budgets and pricing, Production checklist."""

import runbound

# docs: custom-prices
runbound.init(custom_prices={"my-gateway/llama-3.3-70b": (0.60, 0.60)})
runbound.record_call("my-gateway/llama-3.3-70b", tokens_in=1000, tokens_out=1000,
                       duration_s=0.1, provider="my-gateway")
# /docs

session = runbound.current_session()
with session.lock:
    cost = session.total_cost_usd

expected = (1000 / 1e6) * 0.60 + (1000 / 1e6) * 0.60
assert abs(cost - expected) < 1e-9, (cost, expected)
