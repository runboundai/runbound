"""Example 3: LangChain tools guarded by the callback handler, one OpenTelemetry span per call.

    pip install --pre "runbound[langchain,otel]"
    python examples/langchain_otel.py

No model and no API key are needed: the tools are invoked directly, the way an agent would.
"""

import sys
import time

try:
    from langchain_core.tools import tool
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError as missing:
    sys.exit(f"this example needs the optional extras ({missing.name} is missing): "
             "pip install --pre 'runbound[langchain,otel]'")

import runbound
from runbound.integrations.langchain import RunboundCallbackHandler

# docs: langchain-otel
# Your app's own OpenTelemetry provider: runbound finds it and never installs one. In production
# this is your OTLP exporter; with no provider at all, init(otel_endpoint="http://collector:4318").
spans = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(spans))
trace.set_tracer_provider(provider)

runbound.init(service="orders", tool_policy={"deny": ["delete_order"]})
# A LangChain tool carries no capability class, and an unclassified tool fails closed while a configured
# plane has not answered yet (plane_unavailable): with RUNBOUND_PLANE_URL set, wait for the first hello.
# A connected plane's defaults for a new service then ASK a person before an unclassified tool runs (default:unclassified):
# approve lookup_order in the console, or this script waits for the request to expire.
deadline = time.monotonic() + 30
while runbound.plane_status().mode == "connecting" and time.monotonic() < deadline:
    time.sleep(0.1)


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


@tool
def delete_order(order_id: str) -> str:
    """Delete an order."""
    return f"order {order_id}: deleted"


config = {"callbacks": [RunboundCallbackHandler()]}  # the handler vets each tool before it runs
print(lookup_order.invoke({"order_id": "A-7"}, config=config))
try:
    delete_order.invoke({"order_id": "A-7"}, config=config)
except runbound.ExecutionRefused as refused:  # the handler stops the chain; the tool never ran
    print("refused", refused.reason)
# /docs

for span in spans.get_finished_spans():
    print("span", span.name, span.attributes["runbound.outcome"])
