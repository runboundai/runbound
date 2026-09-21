"""Stopping a run once it crosses its own action cap. Shown on: Getting started, Wire the loop."""

from runbound import GuardrailTripped

# docs: action-cap-stop
import runbound

runbound.init(on_anomaly="raise", max_actions_per_run=2)

@runbound.tool
def lookup_order(order_id: str) -> dict:
    return {"order": order_id}

with runbound.session("run:1"):
    lookup_order("A1")
    lookup_order("A2")

    stopped = None
    try:
        lookup_order("A3")
    except GuardrailTripped as exc:
        stopped = exc
# /docs

assert stopped is not None, "expected the third action to cross max_actions_per_run"
assert stopped.decision.boundary == "blast_radius"
