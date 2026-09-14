"""A tool policy written as a plain dict, coerced into a ToolPolicy for you. Shown on: Level 3."""

from runbound import PolicyViolation

# docs: tool-policy-dict
import runbound

runbound.init(tool_policy={"deny": ["wire_money"], "max_calls": {"framework_search": 5}})

@runbound.tool
def wire_money(account: str, amount: float): ...

@runbound.tool
def framework_search(query: str): ...
# /docs

try:
    wire_money(account="acct-1", amount=5)
    raise AssertionError("expected PolicyViolation: wire_money is denied")
except PolicyViolation as exc:
    assert exc.violation.rule == "deny"

init_error = None
try:
    runbound.init(tool_policy={"deny": ["wire_money"], "not_a_real_field": True})
except ValueError as exc:
    init_error = exc

assert init_error is not None, "an unknown tool_policy dict key must raise at init()"
