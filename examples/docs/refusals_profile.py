"""Your own status and sentence for a refusal, not runbound's. Shown on: Alerts and callbacks."""

import runbound
from runbound import GuardrailTripped

answer = None

# docs: refusals-profile
runbound.init(
    budget_usd=0.0005,
    on_anomaly="raise",
    refusals={
        "default": {"status": 429, "message": "This assistant can't continue this conversation right now. Please try again later."},
        "budget":  {"status": 402, "message": "This conversation is over today's spending limit."},
        "policy":  {"status": 403, "message": "That action isn't allowed."},
    },
)

try:
    runbound.record_call("gpt-4o", tokens_in=1000, tokens_out=1000)
except GuardrailTripped as exc:
    r = exc.refusal
    answer = (r.status, r.headers, {**r.body(), "reply": r.message})
# /docs

assert answer is not None, "the over-budget call must raise and be caught"
status, headers, body = answer
assert status == 402
assert body["reply"] == "This conversation is over today's spending limit."
assert body["refused"] is True
