"""A run budget and a daily key budget, checked side by side. Shown on: Getting started, Wire the loop."""

from runbound import GuardrailTripped

# docs: run-and-key-budget
import runbound

runbound.init(run_budget_usd=1.0, budget_usd=20.0, budget_window="day", on_anomaly="raise")

with runbound.session("user:8842"):
    runbound.record_call("gpt-4o", 1_000, 1_000)   # a normal call, well under either budget

    tripped = None
    try:
        runbound.record_call("gpt-4o", 1_000_000, 1_000_000)   # ~$12.50: over the $1 run budget
    except GuardrailTripped as exc:
        tripped = exc
# /docs

assert tripped is not None, "expected this run's own $1 budget to refuse the second call"
assert tripped.decision.level == "run", tripped.decision.level

view = runbound.budget("user:8842")
assert view.limit == 20.0, "the key's own daily budget is unaffected by the run's own cap"
assert view.window == "day"
