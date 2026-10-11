"""Example 1: a sync tool, the rules its code states, and a console row that tightens them.

    python examples/tool_rules.py                      # no account: the code rules alone
    RUNBOUND_PLANE_URL=http://localhost:8000 RUNBOUND_TOKEN=rb_... python examples/tool_rules.py

Connected, the row your console holds for `issue_credit` is folded in (examples/README.md sets one
with max_calls_per_session=1). Most restrictive wins, field by field: a row can add a restriction,
never loosen a rule written here.
"""

import time

import runbound

# docs: tool-rules
# The token and plane URL come from RUNBOUND_TOKEN / RUNBOUND_PLANE_URL; with neither, it runs locally.
runbound.init(service="credits")


def under_100(call: runbound.ToolCall) -> bool:
    """A code rule is a plain predicate over the call's final arguments."""
    return call.kwargs["amount"] <= 100


@runbound.tool(effects={"write"}, max_calls=3, constraint=under_100)
def issue_credit(customer: str, amount: float) -> dict:
    """Give a customer store credit."""
    return {"customer": customer, "credited": amount}


def attempt(amount: float) -> None:
    try:
        print("ok      ", issue_credit(customer="c-42", amount=amount))
    except runbound.ExecutionRefused as refused:  # the body did not run
        print("refused ", refused.reason, "-", refused)
# /docs


# Connected? Wait for the first answered hello; the same hello fetches this service's console rows.
deadline = time.monotonic() + 30
while runbound.plane_status().mode == "connecting" and time.monotonic() < deadline:
    time.sleep(0.1)
if runbound.plane_status().mode == "connected":
    time.sleep(1.0)  # the row fetch runs right after that hello; give it a moment to land
print("plane:", runbound.plane_status().mode)

with runbound.session("run-1"):  # max_calls and the row's cap both count per session
    attempt(50)   # ok
    attempt(500)  # refused: policy (the constraint). A refused attempt does not use up max_calls.
    attempt(20)   # ok on code rules alone; connected, the row's cap of 1 refuses it: session_max_calls
    attempt(20)   # ok on code rules alone (the third admitted call)
    attempt(20)   # refused: policy (the code's own max_calls=3, checked before any row)
