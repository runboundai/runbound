"""Reading the tally max_calls is measured against, without provoking one. Shown on: Tools and policy."""

import runbound
from runbound import PolicyViolation

runbound.init(tool_policy={"max_calls": {"issue_refund": 1}})

@runbound.tool
def issue_refund(user: str, amount: float):
    return "refunded"

with runbound.session("run:8842"):
    issue_refund(user="u1", amount=5)
    for _ in range(2):
        try:
            issue_refund(user="u1", amount=5)
        except PolicyViolation:
            pass

# docs: tool-calls-tally
tally = runbound.tool_calls("run:8842")
# /docs

assert tally == {"issue_refund": 3}, tally
