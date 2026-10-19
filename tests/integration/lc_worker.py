"""A separate PROCESS for the LangChain restart tests (AAI-04 gap G-1): one Python interpreter that connects to a real plane, builds the harness graph over a shared sqlite checkpoint,
and either runs the first turn (prints the interrupt payload and then waits to be killed) or resumes (prints what happened). Every tool BODY that starts appends a line to a ledger FILE, so
a body that began in a process that was then killed is still counted. Not a test module: ``test_lc_process_restart_plane.py`` spawns it.

    python lc_worker.py '<json: mode, url, key, service, db, ledger, resume, hold_s>'
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # tests/: so ``integration`` imports as a package
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # the SDK checkout

import runbound  # noqa: E402
from integration import lc_harness as h  # noqa: E402
from runbound.integrations import langchain_early as early, langchain_resume as resume  # noqa: E402

EFFECTS = {"lookup_order": {"read"}, "issue_refund": {"financial"}, "send_email": {"write"}}
REFUND = h.Call("issue_refund", {"order_id": "o-1", "amount_cents": 500}, "call_1")


def governor(layout, tools):
    return tools, [early.RunboundMiddleware(effects=EFFECTS)]


def say(kind: str, value) -> None:
    print(f"{kind} {json.dumps(value)}", flush=True)


class FileList(list):
    def __init__(self, path: str, hold_s: float) -> None:
        super().__init__()
        self.path, self.hold_s = path, hold_s

    def append(self, item) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps({"pid": os.getpid(), "tool": item[0], "args": item[1]}) + "\n")
        super().append(item)
        say("STARTED", {"pid": os.getpid(), "tool": item[0]})
        if self.hold_s:
            time.sleep(self.hold_s)  # the body is "running" until this process is killed


def main() -> None:
    args = json.loads(sys.argv[1])
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    runbound.init(control_plane_url=args["url"], token=args["key"], service=args["service"], control_plane_poll_s=0.2, approval_bind=True)
    deadline = time.monotonic() + 30
    while runbound.plane_status().mode != "connected" and time.monotonic() < deadline:
        time.sleep(0.1)
    early.enable()
    resume.enable()
    ledger = h.Ledger()
    ledger.starts = FileList(args["ledger"], float(args.get("hold_s") or 0))
    script = [[REFUND]] if args["mode"] == "first" else []  # the scripted model is not checkpointed: a resuming process only needs its final plain answer, or it would ask for the refund again
    graph = h.build_create_agent(script, governor, ledger=ledger, checkpointer=SqliteSaver(sqlite3.connect(args["db"], check_same_thread=False)))
    config = {"configurable": {"thread_id": args.get("thread") or "th-restart"}}
    say("READY", {"pid": os.getpid()})
    if args.get("at"):  # start together with the other workers: sleep until a shared wall-clock instant
        time.sleep(max(0.0, float(args["at"]) - time.time()))
    if args["mode"] == "first":
        out = graph.invoke({"messages": [HumanMessage("go")]}, config)
        say("PAYLOAD", out["__interrupt__"][0].value)
        time.sleep(120)  # wait to be killed: nothing here exits cleanly
    else:
        out = graph.invoke(Command(resume=args["resume"]), config)
        tools = [m.content for m in out.get("messages", []) if getattr(m, "type", "") == "tool"]
        say("DONE", {"pid": os.getpid(), "tools": tools, "interrupted": "__interrupt__" in out, "interrupt": [i.value for i in out.get("__interrupt__", [])]})


if __name__ == "__main__":
    main()
