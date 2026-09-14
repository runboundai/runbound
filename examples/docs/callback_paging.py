"""Your own kill switch, called directly with no token and no plane. Shown on: Alerts and callbacks, Production checklist."""

import runbound

paged: list = []

def page_on_call(anomaly):
    paged.append(anomaly)

# docs: callback-paging
runbound.init(budget_usd=0.0005, on_anomaly="callback", callback=page_on_call)
# /docs

runbound.record_call("gpt-4o", tokens_in=1000, tokens_out=1000)

assert len(paged) == 1, paged
assert paged[0].detector == "budget"
