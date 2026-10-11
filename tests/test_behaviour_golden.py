"""The golden fixture that a behaviour change at the tool boundary must keep reproducing.

The scenario and the normaliser live in ``tests/golden_script.py`` (not a test
module itself — see its docstring), defined exactly once so this file can never
drift from what produced the fixture. This file only runs the scenario and
compares. It covers decorated-tool rules, capability class rules, the latch and
the action cap; every feature that was removed from the SDK is gone from it.
"""

import json
import pathlib
import subprocess
import sys

import golden_script as golden

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES / name) as fh:
        return json.load(fh)


def test_the_scenario_matches_the_golden():
    assert golden.run_scenario() == _load("behaviour_golden.json")


def test_policy_rules_are_judged_before_the_capability_class():
    """The invariant the fixture pins by name: slot 3 (policy) runs before slot 4 (capability)."""
    rules = _load("behaviour_golden.json")["rules"]
    both = [a for a in rules["anomalies"] if a[3].get("tool") == "golden_both"]

    assert [a[0] for a in both] == ["policy"]  # it also breaks the financial class rule


def test_a_latched_session_refuses_every_tool_and_another_key_is_untouched():
    latching = _load("behaviour_golden.json")["latching"]

    assert latching["sessions"]["golden:latch"]["latched_by"] == "policy"
    assert latching["sessions"]["golden:other"]["latched_by"] is None
    assert [r["detector"] for r in latching["refusals"]] == ["policy"] * 3
    assert len(latching["anomalies"]) == 1  # the on-call is paged once, when it latched


def test_a_refused_attempt_is_not_counted_as_executed():
    cap = _load("behaviour_golden.json")["action_cap"]["sessions"]["default"]

    assert (cap["executed"], cap["tool_calls"]["golden_ping"]) == (2, 4)


# --- determinism -------------------------------------------------------


def _run_in_a_subprocess(repo_root: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "tests.golden_script", "--print"],
        cwd=repo_root,
        timeout=60,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_scenario_is_deterministic_in_process_and_across_processes():
    """Capture it twice in this process and twice in fresh ones; all four
    must agree. A golden that varies between runs is worse than no golden at
    all — this is what proves it does not.
    """
    repo_root = str(pathlib.Path(golden.__file__).resolve().parent.parent)

    in_process_first = golden.run_scenario()
    in_process_second = golden.run_scenario()
    cross_process_first = _run_in_a_subprocess(repo_root)
    cross_process_second = _run_in_a_subprocess(repo_root)

    assert in_process_first == in_process_second
    assert cross_process_first == cross_process_second
    assert in_process_first == cross_process_first
