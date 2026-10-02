# A runaway, start to finish

[← Docs](../README.md)

A runaway is an agent repeating one action until something stops it. This page
runs one and reads the whole story back out of `runbound.events()`: it is
detected, contained, explained, and then the session comes back. The same
records, in the same order, are what a connected console shows as one incident.

```python
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
```

It prints:

```text
detected
paged
posture: restricted
limited
refused
posture: stopped
closed
posture: full
```

Read it top to bottom:

| Line | What happened | The record in `events()` |
|---|---|---|
| `detected` | The third identical call: a line in your log, nothing stopped. | `anomaly`, detector `loop`, `details["rung"] == "log"` |
| `paged` | The sixth: critical, pages your alert routes, still stops nothing. | `anomaly`, `loop`, rung `alert` |
| `posture: restricted` | The ninth hands the loop to the spike ladder, which narrows the session. | `posture`, `source == "ladder"` |
| `limited` | The loop's own record of that step. | `anomaly`, `loop`, rung `contain`, `level` 2 |
| `refused` | Each further refund is refused before its body runs. | `anomaly`, detector `safe_mode`, reacted `blocked` |
| `posture: stopped` | The limited session spent its allowance. | `posture`, `posture == "stopped"` |
| `closed` | The close, with its cooldown. | `anomaly`, `loop`, rung `contain`, `level` 3 |
| `posture: full` | The cooldown ended and the key is served again. | `posture`, reason `cooldown served` |

Two things to know. Containment only happens with `on_spike="limit"`, a keyed
session and `on_anomaly="raise"`; without them you get the first two lines and
a notice at the ninth that says why it was not contained. And `decisions()`
holds only refusals, so a run that was merely logged has events and no
decisions.

See also: [Getting started](../getting-started.md), [Spike
detection](spike-detection.md), [What changed](what-changed.md).
