"""A runaway told from events(): detected, contained, explained, recovered. Shown on: A runaway, start to finish."""

import time

import runbound

# docs: runaway-arc
runbound.init(on_anomaly="raise", on_spike="limit", spike_limit_calls=2, spike_cooldown_seconds=0.5)

@runbound.tool(effects={"financial"})
def issue_refund(amount: float) -> str:
    return "refunded"

with runbound.session("user:7"):
    for _ in range(12):                      # the same refund, over and over
        try:
            issue_refund(25.0)
        except runbound.ExecutionRefused:
            pass
try:
    with runbound.session("user:7"):         # entering a closed session starts its cooldown
        pass
except runbound.ExecutionRefused:
    pass
time.sleep(0.7)
with runbound.session("user:7"):             # the cooldown is over: served again
    issue_refund(25.0)

RUNGS = {"log": "detected", "alert": "paged"}

def step(event: dict) -> str | None:
    """One line of the story, or None for a record that is not part of it."""
    if event["kind"] == "anomaly" and event["detector"] == "loop":
        rung = event["details"]["rung"]
        if rung in RUNGS:
            return RUNGS[rung]
        return "limited" if event["details"]["level"] == 2 else "closed"
    if event["kind"] == "anomaly" and event["detector"] == "safe_mode":
        return "refused"                         # a call refused because of the posture
    if event["kind"] == "posture":
        return f"posture: {event['posture']}"
    return None

story = []
for line in filter(None, map(step, runbound.events())):
    if not story or story[-1] != line:           # a repeat of the same beat is one line
        story.append(line)
        print(line)
# /docs

order = ["detected", "paged", "posture: restricted", "limited", "refused", "posture: stopped", "closed", "posture: full"]
assert story == order, story  # each beat, once, in this order
