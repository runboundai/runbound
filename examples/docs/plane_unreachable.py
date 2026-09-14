"""An unreachable plane changes nothing about the guarding. Shown on: Control plane, Production checklist."""

import time

import runbound
from runbound import GuardrailTripped

# docs: plane-unreachable · needs token
# an unreachable plane, on purpose
runbound.init(control_plane_url="http://127.0.0.1:9", token="ag_live_x",
                budget_usd=0.01, on_anomaly="raise")

tripped = None
with runbound.session("user:1"):
    try:
        runbound.record_call("gpt-4o", tokens_in=5000, tokens_out=5000)
    except GuardrailTripped as exc:
        tripped = exc
# /docs

assert tripped is not None, "the budget wall must hold even with no plane to ask"
assert tripped.anomaly.detector == "budget"

# The link itself: bounded, and honest about being down. Poll briefly rather
# than sleep a fixed, longer window — every call to the plane is bounded by
# control_plane_timeout_s, so a few more session() entries are what it takes
# to run through the 3 consecutive failures that mark the link degraded.
deadline = time.monotonic() + 0.5
mode = runbound.plane_status().mode
while mode != "degraded" and time.monotonic() < deadline:
    with runbound.session("user:1"):
        pass
    mode = runbound.plane_status().mode

assert mode == "degraded", f"plane_status().mode was {mode!r} after polling"
