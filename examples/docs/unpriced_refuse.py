"""Refusing a call outright when its model has no known price. Shown on: Budgets and pricing, Production checklist."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion(model="mystery-model-v1")])

import runbound
from openai import OpenAI

# docs: unpriced-refuse
runbound.init(on_unpriced_model="refuse")
client = runbound.wrap(OpenAI())

refused = None
try:
    client.chat.completions.create(
        model="mystery-model-v1", messages=[{"role": "user", "content": "hi"}]
    )
except runbound.GuardrailTripped as exc:
    refused = exc
# /docs

assert refused is not None
assert refused.anomaly.details["reason"] == "unpriced_model"
assert transport.calls == 0, "an unpriced model must be refused at the door"
