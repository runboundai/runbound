"""The rule for a tool on its own decorator, enforced before the body runs. Shown on: the landing page, How you use it."""

from runbound import PolicyViolation

import runbound

runbound.init(on_anomaly="raise")
refunds = []

# docs: tool-rule-on-decorator
@runbound.tool(effects={"financial"}, max_calls=1)
def issue_refund(user: str, amount: float):
    refunds.append(amount)
# /docs

issue_refund(user="u1", amount=10.0)
try:
    issue_refund(user="u1", amount=10.0)
    raise AssertionError("expected PolicyViolation on the second issue_refund")
except PolicyViolation as exc:
    assert exc.violation.rule == "max_calls", exc.violation.rule

assert refunds == [10.0], "the refused call must never run the body"
