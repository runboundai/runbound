"""The golden fixture that a behaviour change must keep reproducing.

The scenario and the normaliser live in ``tests/golden_script.py`` (not a
test module itself — see its docstring), defined exactly once so this file
can never drift from what produced the fixtures. This file only runs the
scenario and compares.

Nothing under ``runbound/`` was touched to build this fixture: it is
captured against unmodified product code, and the two fixtures
(``tests/fixtures/behaviour_golden.json``,
``tests/fixtures/behaviour_golden_no_admission.json``) are the baseline
captured before the execution envelope existed, which every later commit
must keep matching under ``envelope=False`` (today, silently — see
``golden_script``'s "envelope=False problem" section).
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


def test_default_configuration_matches_the_golden():
    result = golden.run_scenario(dict(golden.BASE_INIT_KWARGS))
    assert result == _load("behaviour_golden.json")


def test_budget_admission_false_matches_its_own_golden():
    result = golden.run_scenario(golden._no_admission_kwargs())
    assert result == _load("behaviour_golden_no_admission.json")


def test_the_two_fixtures_differ_on_the_reservation_and_nothing_else():
    """The reservation wall is the one call in the scenario that states an
    output cap, so it is the one place ``budget_admission`` can make a
    difference — every other call is uncapped and takes the same path
    either way (``test_with_no_stated_cap_capped_is_byte_identical_to_admission_off``,
    ``tests/test_budget_soft_and_reservation.py``). This names exactly which
    fields move, and by how much, rather than asserting general inequality —
    see ``golden_script``'s "WHAT DIFFERS" section for the reasoning.
    """
    capped = _load("behaviour_golden.json")
    uncapped = _load("behaviour_golden_no_admission.json")

    assert capped != uncapped

    # "capped" refuses the reservation before it reaches the fake; "False"
    # lets it through and it actually costs $1.00.
    assert len(capped["refusals"]) == len(uncapped["refusals"]) + 1
    assert len(capped["anomalies"]) == len(uncapped["anomalies"]) + 1
    assert capped["client_calls"] == uncapped["client_calls"] - 1

    reservation_refusals = [r for r in capped["refusals"] if r["rule"] == "reservation"]
    assert len(reservation_refusals) == 1
    assert reservation_refusals[0]["detector"] == "budget"
    assert not any(r["rule"] == "reservation" for r in uncapped["refusals"])

    assert capped["budget"]["golden:wall-reservation"] == {
        "limit": 3.0,
        "spent": 2.0,
        "remaining": 1.0,
        "soft_at": None,
        "window": None,
        "resets_at": None,
        "scope": "session",
        "reserved": 0.0,
    }
    assert uncapped["budget"]["golden:wall-reservation"] == {
        "limit": 3.0,
        "spent": 3.0,
        "remaining": 0.0,
        "soft_at": None,
        "window": None,
        "resets_at": None,
        "scope": "session",
        "reserved": 0.0,
    }

    # Every other session's budget, and the latching phase, are untouched.
    for key in ("default", "golden:wall-budget", "golden:wall-steps", "golden:wall-tokens"):
        assert capped["budget"][key] == uncapped["budget"][key]
    assert capped["latching"] == uncapped["latching"]
    assert capped["is_tripped"] == uncapped["is_tripped"] is None


# --- determinism -------------------------------------------------------


def _run_in_a_subprocess(repo_root: str, which: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "tests.golden_script", "--print", which],
        cwd=repo_root,
        timeout=60,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def test_the_scenario_is_deterministic_in_process_and_across_processes():
    """Capture it twice in this process and twice in fresh ones; all four
    must agree. A golden that varies between runs is worse than no golden at
    all — this is what proves it does not.
    """
    repo_root = str(pathlib.Path(golden.__file__).resolve().parent.parent)

    in_process_first = golden.run_scenario(dict(golden.BASE_INIT_KWARGS))
    in_process_second = golden.run_scenario(dict(golden.BASE_INIT_KWARGS))
    cross_process_first = _run_in_a_subprocess(repo_root, "default")
    cross_process_second = _run_in_a_subprocess(repo_root, "default")

    assert in_process_first == in_process_second
    assert cross_process_first == cross_process_second
    assert in_process_first == cross_process_first
