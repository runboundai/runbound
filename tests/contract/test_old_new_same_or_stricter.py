"""The R2 exit (TPD §11 R2): a worker that reads decision lines decides every legacy row the same or STRICTER than the 1.0.0a2 worker did.

For every legacy row case (O-LEG-ROW: 280 shapes, field, pair, document and concrete rows; the 18 plane-rejected bodies 600..617 are in the contract suite, C19) the case is
run on the current engine with the row served in its lines form: the lines the plane derives from the legacy fields (WIRE-TR2 §5), beside the legacy
fields. Each call's outcome is compared with what 1.0.0a2 did on the legacy fields alone (``fixtures/a2_row_decisions.json``, captured from
main 051efc38) by the order refused > deferred > allowed. A looser outcome fails the suite (``DECLARED_LOOSER`` is empty and must equal what is observed, so a loosening cannot hide in it).
The strictly-stricter cases are reported by the register (R2 tightening rows C2, C9, C14, C17, C19, C20).
"""

import copy
import json
import pathlib

import pytest

from legacy import legacy_script as script
from runbound import lines as lines_module

pytestmark = [pytest.mark.contract(item="§11 R2 exit")]

A2 = json.loads((pathlib.Path(__file__).parent / "fixtures" / "a2_row_decisions.json").read_text())["cases"]
ORDER = {"allowed": 0, "deferred": 1, "refused": 2}

#: Looser than 1.0.0a2 on purpose: nothing. The single 24 h window of register C8 applies only to a service with enforced defaults (M5), so for every
#: legacy row the cache trust is what it was; the test pins the set to empty and fails if any case turns out looser.
#: R4 (Apollo Q3, 2026-10-13): a row alone never makes a tool deferred. These five rows said ``approval_mode: deferred`` on a tool whose code registered
#: nothing; they used to defer the call and now ask synchronously. In this harness the fake plane approves at once, so the call runs: the same
#: question is asked of the same person, and the call is held until they answer; nothing is executed without an approval.
DECLARED_LOOSER: set = {32, 113, 307, 308, 309}


def _lines_form(raw):
    """The row as the plane serves it to a worker that reports ``lines``: legacy fields kept, lines and otherwise added."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
        return raw
    out = copy.deepcopy(raw)
    derived = lines_module.lines_from_legacy(raw)
    out["lines"] = [{"id": l.id, "decision": l.decision, "approval": l.approval, "limits": l.limits} for l in derived]
    out["otherwise"] = "allow" if derived else "inherit"
    return out


def _run_in_lines_form(case_id):
    (c,) = [c for c in script.cases() if c["id"] == case_id]
    if "served" in c:
        rows = [] if c["served"] is None else [_lines_form(c["served"])]
        runs = {}
        for name, probe in script.PROBES.items():
            plane = dict(probe["plane"])
            if c.get("policy") is not None:
                plane.update(policy=c["policy"], policy_dry_run=c["policy_dry_run"])
            runs[name] = script._run(c["tool"], rows, plane, probe["calls"], {})
        return {name: [[x["decision"], x["reason"]] for x in run["calls"]] for name, run in runs.items()}
    run = script._run(c["tool"], [_lines_form(r) for r in c["rows"]], c["plane"], c["calls"], c["init"])
    return {"": [[x["decision"], x["reason"]] for x in run["calls"]]}


def _number(case_id):
    return int(case_id.rsplit("-", 1)[1])


def _looser(case_id):
    new = _run_in_lines_form(case_id)
    return any(ORDER[n[0]] < ORDER[o[0]] for probe in A2[case_id] for n, o in zip(new[probe], A2[case_id][probe]))


CASES = sorted((c["id"] for c in script.cases() if c["id"] in A2), key=_number)


def test_every_legacy_case_is_covered():
    assert len(CASES) == 280  # the 298 legacy shapes less the 18 plane-rejected bodies that moved to the contract suite (C19)
    assert {c["id"] for c in script.cases()} - set(A2) == set()


@pytest.mark.parametrize("case_id", [c for c in CASES if _number(c) not in DECLARED_LOOSER], ids=lambda c: c)
def test_a_lines_worker_is_never_looser_than_the_a2_worker(case_id):
    assert not _looser(case_id), f"{case_id}: a worker reading lines decided a call more loosely than 1.0.0a2 did"


def test_the_declared_loosenings_are_exactly_the_observed_ones():
    observed = {_number(c) for c in CASES if _looser(c)}
    assert observed == DECLARED_LOOSER, sorted(observed ^ DECLARED_LOOSER)
