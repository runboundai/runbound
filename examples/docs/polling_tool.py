"""A tool that is supposed to run over and over is not a loop. Shown on: Tools and policy.

The "# docs: polling-tool" marker below is consumed by id outside this
package: the documentation site renders this snippet by that exact id.
Renaming this file or its marker means updating that consumer too, and
nothing in this package's own test suite can see it.
"""

import runbound

runbound.init(on_anomaly="raise")

# docs: polling-tool
@runbound.tool(polling=True)
def poll_job_status(job_id: str) -> str:
    return "running"
# /docs

# loop_threshold defaults to 3; five identical calls would trip an ordinary
# tool. Nothing here is wrapped in try/except, so a loop trip would crash
# this script and fail the test — the passing case *is* the proof.
for _ in range(5):
    poll_job_status(job_id="job-1")

assert runbound.tool_calls()["poll_job_status"] == 5
