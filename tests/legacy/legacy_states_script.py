"""Legacy capture of today's plane-loss states, per capability class (R0, TR0-O deliverable C, AC11).

Not a pytest test module; ``test_legacy_states.py`` reads ``fixtures/legacy_states.json``. The cases are defined
once here so the capture and the check cannot drift apart (the ``tests/golden_script.py`` pattern).

A case is one class of tool (read, write, external, financial, destructive, privileged, or unclassified: no declared
effect) called in one state of the link and the on-disk cache, through the **real engine**, the real
:mod:`runbound.cache` and a real :class:`runbound.shared.RemoteState` over a plane double that answers nothing. Only
the clocks are fake (the link's monotonic clock is injected; the cache age is set on the loaded cache). Ids are
``O-LEG-STA-<n>``, never reused. What each case records: whether the body ran, and the refusal reason if not.

States (today's wording, TPD section 5.7 is the contract these are compared with):

* ``cache_0min``/``cache_16min``/``cache_25h``: a worker that starts with a cache of that age and has not yet heard
  a hello (``connecting``);
* ``no_cache``: no cache, no hello, still ``connecting``;
* ``no_hello_61s``: no hello and no cache 61 s after start (``plane_unavailable``);
* ``cache_0min_no_hello_61s``: a fresh cache but still no hello 61 s after start;
* ``hello_then_30s_silence`` / ``hello_then_61s_silence``: one answered hello, then the plane goes quiet.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

import runbound
from runbound import api, cache as cache_module
from runbound.config import RunboundConfig
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import HelloReply
from runbound.shared import RemoteState

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES_DIR / "legacy_states.json"

URL, TOKEN, SERVICE = "https://plane.example", "legacy-states-token", "svc"

CLASSES = {
    "read": ["read"], "write": ["write"], "external": ["external"], "financial": ["financial"],
    "destructive": ["destructive"], "privileged": ["privileged"], "unclassified": [],
}

#: Classes added after the first capture (K8: a bare write has no tag). Their ids continue after the original block, so no id moves.
EXTRA_CLASSES = {"read_write": ["read", "write"], "write_external": ["write", "external"]}

#: K7: the last sealed policy (here a 25 h old cache, so expired) held a row for the tool. kind -> the row's fields.
SEALED = {
    "blocked": {"blocked": True},
    "approval": {"approval_required": True},
    "allowed": {"max_calls_per_session": 1000},
}
SEALED_CLASSES = ("read", "write", "external", "destructive")

#: (state id, cache age in seconds or None, hello first?, seconds of silence after the start or the hello)
STATES = (
    ("cache_0min", 0, False, 0),
    ("cache_16min", 16 * 60, False, 0),
    ("cache_25h", 25 * 3600, False, 0),
    ("no_cache", None, False, 0),
    ("no_hello_61s", None, False, 61),
    ("cache_0min_no_hello_61s", 0, False, 61),
    ("hello_then_30s_silence", None, True, 30),
    ("hello_then_61s_silence", None, True, 61),
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Plane:
    key_state = "unknown"

    def __init__(self, served=None):
        self.served = served

    def policy(self, service):
        return self.served

    def hello(self, payload):
        return None

    def approvals(self, *args, **kwargs):
        return None, None


def cases() -> list:
    """``[(id, state id, class name)]`` in a fixed order; ids 1-56 the original block, 57-72 the extra classes,
    101+ the sealed-policy cases. An id is never reused or moved."""
    out, n = [], 0
    for classes in (CLASSES, EXTRA_CLASSES):
        for state in STATES:
            for name in classes:
                n += 1
                out.append((f"O-LEG-STA-{n}", state[0], name))
    n = 100
    for kind in SEALED:
        for name in SEALED_CLASSES:
            n += 1
            out.append((f"O-LEG-STA-{n}", f"sealed_{kind}", name))
    return out


def case_ids() -> list:
    return [case_id for case_id, _state, _name in cases()]


def all_classes() -> dict:
    return {**CLASSES, **EXTRA_CLASSES}


#: Service defaults a service has adopted and enforced, as WIRE-TR2b serves them: every class allowed (so the sealed policy allows the tool, which is what
#: the state contract's ``Cache`` assumes) and fail mode as TPD §5.5 starts a service with: closed for financial, destructive, privileged and unclassified.
ENFORCED_DEFAULTS = {"revision": 1, "state": "enforced", "defaults": {
    "decision": {k: "allow" for k in ("read", "write", "external", "financial", "destructive", "privileged", "unclassified")},
    "fail_mode": {"read": "open", "write": "open", "external": "open", "financial": "closed", "destructive": "closed", "privileged": "closed", "unclassified": "closed"},
    "approval": {}}}


def run_case(state_id: str, class_name: str, defaults: "dict | None" = None) -> dict:
    """One scenario; with ``defaults`` the service has adopted them (a sealed cache carries them, and a hello serves them)."""
    rows = None
    if state_id.startswith("sealed_"):  # an expired cache that held a rule for this tool, 61 s into an outage
        age_s, hello, silence_s = 25 * 3600, False, 61
        rows = [{"tool": f"probe_{class_name}", "state": "enforced", **SEALED[state_id[len("sealed_"):]]}]
    else:
        spec = next(s for s in STATES if s[0] == state_id)
        _id, age_s, hello, silence_s = spec
    cache_dir = pathlib.Path(tempfile.mkdtemp(prefix="rb-legacy-states-"))
    api._teardown_for_tests()
    try:
        runbound.init(cache_dir=str(cache_dir))
        config = RunboundConfig(control_plane_url=URL, token=TOKEN, service=SERVICE, worker_id="w", cache_dir=str(cache_dir))
        config.validate()
        if age_s is not None:  # a cache a previous worker left behind
            path = cache_module.resolve(str(cache_dir), URL, SERVICE)
            cache_module.write(path, TOKEN, cache_module.body_for(SERVICE, 1, None, False, "h", rows or [], service_defaults=defaults))
        clock = _Clock()
        # A plane with nothing to serve a service answers the policy read with an EMPTY policy (the first answered hello reads the policy whatever version it announced);
        # a read that fails would leave the worker holding none, which is a different state and not what these cases capture.
        served = {"version": 0, "policy": None, "rows": [], "rows_hash": "h"} if defaults is None else {"version": 1, "policy": None, "rows": [], "rows_hash": "h", "service_defaults": defaults}
        shared = RemoteState(_Plane(served), None, config, now=clock)
        if age_s is not None:
            shared._cache_written_at = time.time() - age_s
        api._ENGINE.shared = shared
        if hello:
            shared.apply_hello(HelloReply(policy_version=1) if defaults is not None else HelloReply())
        clock.t += silence_s
        effects = all_classes()[class_name]

        def body():
            return "ran"

        body.__name__ = f"probe_{class_name}"
        tool = runbound.tool(effects=set(effects) or None)(body)
        try:
            tool()
            result = {"ran": True, "reason": None}
        except GuardrailTripped as refusal:
            result = {"ran": False, "reason": getattr(refusal, "reason", None)}
        result["link"] = shared.link_state()
        return result
    finally:
        api._teardown_for_tests()


def run_all() -> dict:
    return {"cases": {cid: {"state": state, "class": name, **run_case(state, name)} for cid, state, name in cases()}}


def main(argv: "list | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.legacy.legacy_states_script")
    parser.add_argument("--write", action="store_true", help="Capture today's behaviour into the fixture.")
    parser.add_argument("--print", dest="show", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(run_all(), indent=2, sort_keys=True) + "\n")
        print(f"wrote {FIXTURE}")
        return 0
    if args.show:
        print(json.dumps(run_all(), sort_keys=True))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
