"""Refusing a call before it goes out, on an estimate rather than a fact. Shown on: Budgets and pricing."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion()])

import runbound
from openai import OpenAI

# docs: budget-admission
runbound.init(budget_usd=0.005, budget_admission=True, on_anomaly="raise")
client = runbound.wrap(OpenAI())

refused = None
try:
    client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
except runbound.GuardrailTripped as exc:
    refused = exc
# /docs

# With no max_tokens cap the estimate assumes admission_output_tokens (1024)
# of output, which alone prices past a $0.005 budget at gpt-4o's rate.
assert refused is not None, "the estimate should have crossed the budget"
assert refused.anomaly.detector == "budget"
assert refused.anomaly.details["rule"] == "admission"
assert transport.calls == 0, "the request must never have gone out"
