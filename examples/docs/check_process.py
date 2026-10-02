"""One command says what is guarded in a process, with no network and no key. Shown on: Getting started."""

# docs: check-process
import runbound

runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")

@runbound.tool(effects={"financial"})
def issue_refund(user: str, amount: float) -> str:
    return "refunded"

@runbound.tool(effects={"read"})
def lookup_order(order_id: str) -> str:
    return "found"

report = runbound.check()        # prints what is guarded in THIS process, and returns it
assert report["guarded"]          # the same fact `python -m runbound check` turns into its exit code
# /docs

assert {t["name"]: t["classes"] for t in report["tools"]} == {"issue_refund": ["financial"], "lookup_order": ["read"]}
assert report["posture"] == "full" and report["plane"]["mode"] == "local"
assert report["budgets"] == {"budget_usd": 5.0, "max_steps": 50}
