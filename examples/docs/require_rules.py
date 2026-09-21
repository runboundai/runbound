"""The CI gate: a tool with no stated rule fails loudly, at decoration time. Shown on: Tools and policy, Production checklist."""

import runbound

# docs: require-rules
runbound.init(require_rules=True)

@runbound.tool(reviewed=True)
def lookup_order(order_id: str):
    return "ok"
# /docs

assert lookup_order(order_id="42") == "ok"

decoration_error = None
try:
    @runbound.tool
    def unreviewed_tool(x: int):
        return x
except ValueError as exc:
    decoration_error = exc

assert decoration_error is not None, "a rule-less tool must fail to decorate under require_rules"
