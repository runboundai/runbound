"""A loop is answered in rungs, and the session comes back after its cooldown. Shown on: Getting started, Loops and spikes."""

# docs: graded-loop
import time

import runbound

runbound.init(
    on_anomaly="raise",
    on_spike="limit",               # without this, containment is off: the ninth call would just run
    spike_limit_calls=2,            # how many more repeats the limited session gets before it is closed
    spike_cooldown_seconds=0.5,     # the default is 300; short here so this page can run
)

ran = []

@runbound.tool(effects={"financial"})
def issue_refund(amount: float) -> str:
    ran.append(amount)
    return "refunded"

refused = []
with runbound.session("user:7"):
    for call in range(1, 13):               # the same refund, again and again
        try:
            issue_refund(25.0)
        except runbound.ExecutionRefused as exc:
            refused.append((call, exc.reason))

# Eight calls ran. The ninth was refused before its body ran, and so was every one after it.
print(len(ran), "ran; first refused:", refused[0])

# The session was closed for its cooldown. Entering it now is refused; once the cooldown is
# over, the same key is served again.
try:
    with runbound.session("user:7"):
        pass
except runbound.ExecutionRefused:
    pass
time.sleep(0.7)
with runbound.session("user:7"):
    print(issue_refund(25.0))
# /docs

assert len(ran) == 9 and ran == [25.0] * 9, ran  # eight before the loop was contained, one after the return
assert refused[0][0] == 9, refused               # the ninth identical call is the first refusal
assert [call for call, _ in refused] == list(range(9, 13)), refused
assert runbound.session_status("user:7")["tripped_by"] is None
