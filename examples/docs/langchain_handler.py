"""Guarding a framework that owns its own dispatch, with a callback handler. Shown on: LangChain and LangGraph."""

REQUIRES = ("langchain_core",)

# docs: langchain-handler
import runbound
from runbound.integrations.langchain import GuardrailCallbackHandler
from langchain_core.tools import tool

runbound.init(budget_usd=5.00, loop_threshold=3, on_anomaly="raise")

orders_looked_up = []

@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    orders_looked_up.append(order_id)
    return "shipped"

config = {"callbacks": [GuardrailCallbackHandler()]}

lookup_order.invoke({"order_id": "42"}, config=config)
lookup_order.invoke({"order_id": "42"}, config=config)
# /docs

tripped = None
try:
    lookup_order.invoke({"order_id": "42"}, config=config)
except runbound.GuardrailTripped as exc:
    tripped = exc

assert tripped is not None, "a third identical tool call should trip the loop detector"
assert tripped.anomaly.detector == "loop"
assert orders_looked_up == ["42", "42"], "the third, refused call must never run the tool body"
