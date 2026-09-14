"""One policy for every tool, stated once at init() instead of per decorator. Shown on: Level 3, Tools and policy."""

from runbound import PolicyViolation

# docs: tool-policy-init
import runbound

def ask_a_human(call: runbound.ToolCall) -> bool:
    return False

runbound.init(
    on_anomaly="raise",
    tool_policy=runbound.ToolPolicy(
        allow=["issue_refund", "lookup_order"], deny=["wire_money"],
        max_calls={"issue_refund": 1}, require_approval=["wire_money"],
        approval_callback=ask_a_human, on_violation="block",
    ),
)

@runbound.tool
def issue_refund(user: str, amount: float): ...
@runbound.tool
def wire_money(account: str, amount: float): ...
@runbound.tool
def lookup_order(order_id: str): ...
@runbound.tool
def send_email(to: str): ...
# /docs

try:
    wire_money(account="acct-1", amount=5)
    raise AssertionError("expected PolicyViolation: wire_money is denied")
except PolicyViolation as exc:
    assert exc.violation.rule == "deny"

try:
    send_email(to="a@example.com")
    raise AssertionError("expected PolicyViolation: send_email is not on the allow list")
except PolicyViolation as exc:
    assert exc.violation.rule == "allow"

assert issue_refund(user="u1", amount=5) is None
assert lookup_order(order_id="42") is None
