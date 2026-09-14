"""A tool that is supposed to run over and over is not a loop. Shown on: Tools and policy."""

import runbound

runbound.init(on_anomaly="raise")

# docs: repeatable-tool
@runbound.tool(repeatable=True)
def poll_job_status(job_id: str) -> str:
    return "running"
# /docs

# loop_threshold defaults to 3; five identical calls would trip an ordinary
# tool. Nothing here is wrapped in try/except, so a loop trip would crash
# this script and fail the test — the passing case *is* the proof.
for _ in range(5):
    poll_job_status(job_id="job-1")

assert runbound.tool_calls()["poll_job_status"] == 5
