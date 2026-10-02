"""The refusal you just caused is a row in events(); a connected plane shows the same row. Shown on: Getting started."""

# docs: verify-events
import runbound

runbound.init(budget_usd=0.01, on_anomaly="raise")

try:
    runbound.record_call("gpt-4o", 1_000_000, 1_000_000)   # far over a one-cent budget
except runbound.ExecutionRefused:
    pass

for event in runbound.events():
    print(event["kind"], event.get("detector"), event.get("reacted"))
# /docs

refusals = [e for e in runbound.events() if e["kind"] == "refusal"]
assert refusals, runbound.events()
assert refusals[-1]["detector"] == "budget", refusals[-1]
