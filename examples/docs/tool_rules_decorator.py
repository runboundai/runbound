"""The rule for a tool lives on its own decorator, in the same diff. Shown on: Level 3, Tools and policy."""

from runbound import PolicyViolation

# docs: tool-rules-decorator
import runbound

runbound.init(on_anomaly="raise")

def under_500(call: runbound.ToolCall) -> bool:
    return call.kwargs.get("amount", 0) < 500

def ask_a_human(call: runbound.ToolCall) -> bool:
    return False

refund_runs = []

@runbound.tool(max_calls=1, constraint=under_500)
def issue_refund(user: str, amount: float):
    refund_runs.append(amount)

@runbound.tool(blocked=True)
def send_email(to: str): ...

@runbound.tool(require_approval=ask_a_human)
def wire_money(account: str, amount: float): ...

@runbound.tool
def lookup_order(order_id: str): ...
# /docs

issue_refund(user="u1", amount=10)
try:
    issue_refund(user="u1", amount=10)
    raise AssertionError("expected PolicyViolation on the second issue_refund")
except PolicyViolation as exc:
    assert exc.violation.rule == "max_calls"
assert refund_runs == [10], "the second, refused call must never run the body"

try:
    send_email(to="a@example.com")
    raise AssertionError("expected PolicyViolation on send_email")
except PolicyViolation as exc:
    assert exc.violation.rule == "deny"

try:
    wire_money(account="acct-1", amount=100)
    raise AssertionError("expected PolicyViolation on wire_money")
except PolicyViolation as exc:
    assert exc.violation.rule == "approval"

assert lookup_order(order_id="42") is None
