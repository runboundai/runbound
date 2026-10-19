"""AAI-04 gap G-1 (Gate B AT-06): a REAL process kill and restart of a suspended LangChain call, against a real plane and a real sqlite checkpoint.

Each phase is a separate Python process (``lc_worker.py``); the kill is SIGKILL. A tool body that starts appends to a ledger FILE, so "no extra dispatch" is counted across processes.
(a) killed while suspended, approved while nobody runs, resumed in a NEW process: the body runs exactly once. (b) killed INSIDE the body after the bind, resumed in a THIRD process: the
request is bound, so the body is not run again and the outcome is ``prior_outcome_unknown``, not a blind retry.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from .test_langchain_resume_plane import ASK, private_plane_resume, rig  # noqa: F401  (fixtures)
from .test_real_plane import KEY, wait_scale

pytestmark = pytest.mark.plane

WORKER = Path(__file__).with_name("lc_worker.py")


class Worker:
    def __init__(self, rig, mode, *, resume=None, hold_s=0.0, thread=None, at=None):
        args = {"thread": thread, "at": at, "mode": mode, "url": rig.plane.url, "key": KEY, "service": rig.service, "db": str(rig.db), "ledger": str(rig.tmp_path / "ledger.jsonl"), "resume": resume, "hold_s": hold_s}
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RUNBOUND_", "OTEL_"))}
        self.proc = subprocess.Popen([sys.executable, str(WORKER), json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(WORKER.parent.parent.parent))
        self.lines: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.err: list = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.err.append(self.proc.stderr.read()), daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            kind, _, rest = line.partition(" ")
            try:
                self.lines.put((kind, json.loads(rest)))
            except ValueError:
                pass

    def wait_for(self, kind, seconds=90):
        end = time.monotonic() + seconds * wait_scale()
        while time.monotonic() < end:
            try:
                got = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None and self.lines.empty():
                    raise AssertionError(f"the worker exited ({self.proc.returncode}) before {kind}:\n{''.join(self.err)[-3000:]}")
                continue
            if got[0] == kind:
                return got[1]
        raise AssertionError(f"no {kind} from the worker in {seconds} s:\n{''.join(self.err)[-3000:]}")

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(10)
        assert self.proc.returncode == -signal.SIGKILL, "the process was killed, it did not exit by itself"

    def finish(self):
        self.proc.wait(60)


def ledger(rig):
    path = rig.tmp_path / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def suspended_then_killed(rig, thread=None):
    rig.set_row(ASK)
    first = Worker(rig, "first", thread=thread)
    payload = first.wait_for("PAYLOAD")
    first.kill()
    assert ledger(rig) == [], "the first process was interrupted outside the body: nothing ran"
    (waiting,) = rig.pending()
    assert waiting["id"] == payload["approval_id"]
    return payload, first


def test_killed_while_suspended_then_approved_and_resumed_in_a_new_process_runs_the_body_exactly_once(rig):
    payload, first = suspended_then_killed(rig)
    rig.decide(payload["approval_id"])
    second = Worker(rig, "resume", resume={"request_id": payload["request_id"], "approval_id": payload["approval_id"]})
    done = second.wait_for("DONE")
    second.finish()
    assert done["interrupted"] is False, done
    assert done["pid"] != first.proc.pid
    lines = ledger(rig)
    assert len(lines) == 1 and lines[0]["pid"] == done["pid"] and lines[0]["tool"] == "issue_refund", "exactly one dispatch, by the new process"
    row = rig.row(payload["approval_id"])
    assert row["status"] in ("executed", "executing") and row.get("bound_attempt_id") == payload["attempt_id"]


def test_a_second_resume_in_another_new_process_after_the_first_finished_dispatches_nothing_more(rig):
    payload, _ = suspended_then_killed(rig)
    rig.decide(payload["approval_id"])
    resume = {"request_id": payload["request_id"], "approval_id": payload["approval_id"]}
    Worker(rig, "resume", resume=resume).wait_for("DONE")
    third = Worker(rig, "resume", resume=resume)
    done = third.wait_for("DONE")
    third.finish()
    assert len(ledger(rig)) == 1, "a duplicate resume, from a third process, did not dispatch again"
    assert not done["interrupted"]


def test_a_process_that_starts_after_the_first_finished_reports_the_stored_result_and_dispatches_nothing(rig):
    """The shape that the race test above showed under load: the late process's report carries the stored 'refunded ...' message but the ledger has one dispatch, the first process's."""
    payload, _ = suspended_then_killed(rig, thread="th-late")
    rig.decide(payload["approval_id"])
    resume = {"request_id": payload["request_id"], "approval_id": payload["approval_id"]}
    first = Worker(rig, "resume", resume=resume, thread="th-late")
    done_first = first.wait_for("DONE")
    first.finish()
    late = Worker(rig, "resume", resume=resume, thread="th-late")
    done_late = late.wait_for("DONE")
    late.finish()
    lines = ledger(rig)
    assert len(lines) == 1 and lines[0]["pid"] == done_first["pid"] != done_late["pid"], "one dispatch, by the first process"
    assert done_late["tools"] == done_first["tools"] and not done_late["interrupted"], "the late process only reports what the checkpoint already holds"


def test_killed_inside_the_body_after_the_bind_a_new_process_does_not_run_it_again_and_says_prior_outcome_unknown(rig):
    payload, _ = suspended_then_killed(rig)
    rig.decide(payload["approval_id"])
    resume = {"request_id": payload["request_id"], "approval_id": payload["approval_id"]}
    second = Worker(rig, "resume", resume=resume, hold_s=120)
    started = second.wait_for("STARTED")  # the bind succeeded and the body began
    second.kill()
    assert len(ledger(rig)) == 1 and ledger(rig)[0]["pid"] == started["pid"]
    row = rig.row(payload["approval_id"])
    assert row.get("bound_attempt_id") == payload["attempt_id"], "the plane holds the bind made before the process died"
    third = Worker(rig, "resume", resume=resume)
    done = third.wait_for("DONE")
    third.finish()
    assert any(t.startswith("refused prior_outcome_unknown") for t in done["tools"]), done
    assert len(ledger(rig)) == 1, "no blind retry: the body that may have run is not run again"


# --- G-2: concurrent resumes -----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("round_", [1, 2, 3])
def test_two_processes_resuming_the_same_approved_request_at_the_same_instant_dispatch_exactly_once(rig, round_):
    payload, _ = suspended_then_killed(rig, thread=f"th-race-{round_}")
    rig.decide(payload["approval_id"])
    resume = {"request_id": payload["request_id"], "approval_id": payload["approval_id"]}
    at = time.time() + 6.0 * wait_scale()  # both interpreters import and connect first, then fire together
    a = Worker(rig, "resume", resume=resume, thread=f"th-race-{round_}", at=at)
    b = Worker(rig, "resume", resume=resume, thread=f"th-race-{round_}", at=at)
    done = [a.wait_for("DONE"), b.wait_for("DONE")]
    a.finish()
    b.finish()
    lines = ledger(rig)
    assert len(lines) == 1, f"exactly one dispatch across both processes, not {len(lines)}"
    # WHO dispatched is the ledger's word (every body start appends to it), never a process's report: a process reports the ToolMessages of the final state of the SHARED checkpoint, so one that
    # starts after the other finished (a loaded host misses the rendezvous) reports the stored 'refunded ...' without having run anything (found 19 Oct, gate run on a host at load 25-78).
    winner = [d for d in done if d["pid"] == lines[0]["pid"]]
    others = [d for d in done if d["pid"] != lines[0]["pid"]]
    assert len(winner) == 1 and len(others) == 1, done
    assert any(t.startswith("refunded") for t in winner[0]["tools"]), "the process that dispatched reports its result"
    loser = others[0]["tools"]
    assert loser[-1].startswith("refused prior_outcome_unknown") or loser == winner[0]["tools"], f"the other process was told the outcome is unknown, or (it started after the winner finished) only reports the stored result: {others[0]}"
    assert rig.row(payload["approval_id"]).get("bound_attempt_id") == payload["attempt_id"], "one bind, by the attempt that asked"
