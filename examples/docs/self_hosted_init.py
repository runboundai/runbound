"""A self-hosted plane: your own URL, and token="" when it needs no auth. Shown on: Control plane."""

import runbound
from runbound import GuardrailTripped

# docs: self-hosted-init · needs token
runbound.init(control_plane_url="https://plane.internal.example.com", token="",
                service="support-bot", budget_usd=5.0, on_anomaly="raise")
# /docs

# The README's `token` row: token="" alone pins a process local; with a
# control_plane_url it is the self-hosted mode instead. plane_status() is the
# public read of which one init() chose: "local" means no plane at all.
mode = runbound.plane_status().mode
assert mode != "local", f"a control_plane_url with token='' must configure a plane, got {mode!r}"

# Guarding still works locally: every docs example runs with the network
# disabled (tests/test_docs_examples.py), so this plane never answers.
tripped = None
try:
    runbound.record_call("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)
except GuardrailTripped as exc:
    tripped = exc

assert tripped is not None, "guarding must still work locally with the self-hosted plane unreachable"
assert tripped.anomaly.detector == "budget"
