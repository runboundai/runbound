"""Graduated pressure for a shared service, instead of slamming the door. Shown on: Runs and keys."""

import runbound

# docs: spike-ladder-limit
runbound.init(
    on_anomaly="raise",
    on_spike="limit",           # requires on_trip="latch" — the default
    spike_limit_calls=5,        # abnormal calls a limited session may still make
    spike_cooldown_seconds=300, # how long a closed session is refused
    spike_max_strikes=3,        # rollovers before the key is blocked outright
)
# /docs

with runbound.session("user:5310"):
    pass

status = runbound.session_status("user:5310")

assert status is not None
for field in ("level", "strikes", "allowance_left", "cooldown_remaining_s", "tripped_by", "generation"):
    assert field in status, (field, status)
assert status["level"] == 0
