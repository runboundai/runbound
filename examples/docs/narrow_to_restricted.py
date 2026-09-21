"""Narrowing a run's posture denies a whole class of actions at once. Shown on: Getting started, Wire the loop."""

from runbound import SafeModeViolation

# docs: narrow-to-restricted
import runbound

runbound.init(on_anomaly="raise")

@runbound.tool(effects={"financial"})
def issue_refund(user: str, amount: float) -> str:
    return "refunded"

runbound.enter_safe_mode(reason="spend looks abnormal", posture="restricted")

refused = None
try:
    issue_refund("u1", 20.0)
except SafeModeViolation as exc:
    refused = exc
# /docs

assert refused is not None, "expected 'restricted' to deny a 'financial' tool"
assert refused.decision.boundary == "posture"
assert runbound.posture() == "restricted"
