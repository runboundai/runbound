"""Example 2: a deferred approval, sealed so any replica of the service may run it once approved.

    python examples/deferred_approval.py               # no plane: refused at once, approval_unavailable
    RUNBOUND_PLANE_URL=http://localhost:8000 RUNBOUND_TOKEN=rb_... \\
        RUNBOUND_APPROVAL_SECRET=<same on every replica> python examples/deferred_approval.py

Connected, the call returns {"status": "pending_approval", ...} at once and the body does not run.
Approve the request in the console (or with the admin API, see examples/README.md); the worker then
verifies the seal, runs the tool through its full chain and calls on_deferred_result.
"""

import os
import threading
import time

import runbound

done = threading.Event()

# docs: deferred-approval
def on_deferred_result(info: dict) -> None:
    """Called once a deferred request this worker executed has ended: executed, failed or tampered."""
    print("deferred", info["request_id"], info["outcome"], info.get("result", info.get("error")))
    done.set()


runbound.init(
    service="refunds",
    approval_secret=os.environ.get("RUNBOUND_APPROVAL_SECRET"),  # seals the arguments; never sent to the plane
    on_deferred_result=on_deferred_result,
)


@runbound.tool(effects={"financial"}, approval="required", mode="deferred", timeout_s=600)
def refund(order_id: str, amount: float) -> dict:
    """Refund an order. Runs only after a person approves it."""
    print("running for request", runbound.current_request())  # None outside an approved execution
    return {"order_id": order_id, "refunded": amount}
# /docs


deadline = time.monotonic() + 30  # a new approval needs a connected link: wait for the first hello
while runbound.plane_status().mode == "connecting" and time.monotonic() < deadline:
    time.sleep(0.1)

try:
    pending = refund(order_id="A-1001", amount=40.0)
except runbound.ExecutionRefused as refused:
    print("refused", refused.reason)  # no plane: approval_unavailable, and nothing ran
else:
    print("pending", pending["request_id"])  # {"status": "pending_approval", "request_id": ...}
    if not done.wait(timeout=600):
        print("no decision within 600 s")
