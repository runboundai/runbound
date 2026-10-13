"""Probe one SDK source tree against the enforcement floor (TPD §4d). Not a test; it backs CONTRACT.md.

    git archive <tag-or-sha> runbound-sdk/runbound | tar -x -C /tmp/probe-<tag>
    python tests/contract/floor_probe.py /tmp/probe-<tag>/runbound-sdk

Prints one line per check: whether a decorated tool RAN or was REFUSED. It builds the SDK's own ``RemoteState`` over
a plane double that answers nothing else, so it needs no network. Works from 0.11.2 on. The halt lines use
``@runbound.tool``; 0.11.2 enforces a halt at the session door, so they run once bare and once inside a session block.
"""

import os
import sys
import tempfile

root = sys.argv[1]
sys.path.insert(0, root)
import runbound  # noqa: E402
from runbound import api  # noqa: E402
import runbound.config as config_module  # noqa: E402
from runbound.plane_types import HelloReply  # noqa: E402
from runbound.shared import RemoteState  # noqa: E402

Cfg = getattr(config_module, "RunboundConfig", None) or config_module.GuardrailConfig


class Client:
    key_state = "unknown"

    def __init__(self, policy=None):
        self._policy = policy

    def policy(self, service):
        return self._policy

    def hello(self, payload):
        return None

    def approvals(self, *args, **kwargs):
        return None, None

    def __getattr__(self, name):  # 0.11.2 asks for more routes (enter, ...): answer nothing
        return lambda *a, **k: None


def fresh(policy=None, **init):
    api._teardown_for_tests()
    os.environ["RUNBOUND_CACHE_DIR"] = tempfile.mkdtemp()  # a new cache each time: no check inherits another's
    runbound.init(**init)
    cfg = Cfg(control_plane_url="https://plane.example", token="k", service="svc", worker_id="w")
    if hasattr(cfg, "validate"):
        cfg.validate()
    shared = RemoteState(Client(policy), None, cfg)
    api._ENGINE.shared = shared
    if hasattr(api, "_SHARED"):  # 0.11.2 reads the fleet halt through this one, at the session door
        api._SHARED = shared
    return shared


def call(effects, name, session=None):
    def body():
        return "ran"

    body.__name__ = name  # the name the SDK reads at decoration, which is what a row or a deny list matches
    tool = runbound.tool(effects=set(effects) or None)(body)
    try:
        if session:
            with runbound.session(session):
                return "RAN" if tool() == "ran" else "?"
        return "RAN" if tool() == "ran" else "?"
    except Exception as exc:  # noqa: BLE001
        return f"REFUSED ({type(exc).__name__}: {getattr(exc, 'reason', '')})"


def line(label, fn):
    try:
        print(f"{label}: {fn()}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label}: PROBE-ERROR {type(exc).__name__}: {str(exc)[:80]}")


def halted(mode, session=None):
    shared = fresh(on_halt=mode)
    shared.apply_hello(HelloReply(halt=True))
    return call(["read"], "h" + mode, session)


def served(policy, effects, name):
    shared = fresh(policy)
    shared.apply_hello(HelloReply(policy_version=2))
    return call(effects, name)


rows = [{"tool": "unk", "state": "enforced", "geo_fence": "eu"}, {"tool": "bad", "state": "bogus", "blocked": True},
        {"tool": "ctl", "state": "enforced", "blocked": True}]
envelope = {"version": 2, "policy": None, "rows": rows, "rows_hash": "h"}
line("L1 halt, on_halt=raise (default)", lambda: halted("raise"))
line("L1 halt, on_halt=warn", lambda: halted("warn"))
line("L1 halt, on_halt=raise, inside runbound.session('k')", lambda: halted("raise", "k"))
line("L1 halt, on_halt=warn, inside runbound.session('k')", lambda: halted("warn", "k"))
line("L2 control: a well-formed blocked row (0.11.2 has no rows)", lambda: served(envelope, ["read"], "ctl"))
line("L2 a row with an unknown restriction key only", lambda: served(envelope, ["read"], "unk"))
line("L2 a malformed row (blocked, state=bogus)", lambda: served(envelope, ["read"], "bad"))
line("L2 control: an org document that denies the tool, every key known",
     lambda: served({"version": 2, "dry_run": False, "policy": {"deny": ["doc0"]}}, ["read"], "doc0"))
line("L2 an org document with an unknown key (deny doc)",
     lambda: served({"version": 2, "dry_run": False, "policy": {"deny": ["doc"], "frobnicate": 1}}, ["read"], "doc"))
for effects in (["read"], ["destructive"], []):
    line(f"L3 before the first hello, no cache, effects={effects}", lambda e=effects: (fresh(), call(e, "pre" + "".join(e)))[1])
