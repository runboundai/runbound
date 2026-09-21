"""Graduated pressure for a shared service, instead of slamming the door. Shown on: Runs and keys."""

import runbound

# docs: spike-ladder-limit
runbound.init(on_anomaly="raise", on_spike="limit")  # requires on_trip="latch" — the default
# The ladder itself -- its mode ("limit"), the abnormal-call allowance, the
# cooldown and the strikes before a permanent block -- is free and local,
# the same as spike detection itself (spike_detection defaults True).
# /docs

with runbound.session("user:5310"):
    pass

status = runbound.session_status("user:5310")

assert status is not None
for field in ("level", "strikes", "allowance_left", "cooldown_remaining_s", "tripped_by", "generation"):
    assert field in status, (field, status)
assert status["level"] == 0
