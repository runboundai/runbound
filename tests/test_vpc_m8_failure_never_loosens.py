"""PRD section 11 row M8: a policy fetch that fails, a cache that expires, no cache at all: nothing is loosened.

A thin named aggregator, not new behaviour: it runs the existing fail-closed contract tests that prove the row, in a child pytest, so one name
fails if any of them does and a renamed or deleted one fails by collection (never by silently running zero). The real-plane leg of the row,
``integration/test_policy_unserved_plane.py``, needs the plane environment and runs in the real-plane suite.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

M8_PROOFS = [
    "contract/test_states_engine.py::test_an_expired_sealed_policy_that_restricted_the_tool_still_refuses_it",
    "contract/test_states_engine.py::test_the_reason_an_expired_policy_refuses_is_policy_expired_restricted",
    "contract/test_enforcement_floor.py::test_o_flr_5d_nothing_held_and_the_marker_arrives_holds_nothing_and_writes_no_cache",
    "test_policy_unserved.py::test_no_policy_and_no_cache_with_a_503_is_fail_closed_for_the_closed_classes_and_never_allow_everything",
    "test_approval_superseded.py::test_vpc_07_9_a_policy_gone_unavailable_during_the_wait_refuses_a_fail_closed_tool",
]


def _pytest(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=TESTS.parent, capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("proof", M8_PROOFS)
def test_vpc_m8_failure_never_loosens(proof):
    node = f"tests/{proof}"
    collected = _pytest("--collect-only", node)
    assert collected.returncode == 0 and re.search(r"\b[1-9]\d* tests? collected", collected.stdout), f"M8 proof is gone or renamed: {node}\n{collected.stdout[-600:]}"

    ran = _pytest(node)

    assert ran.returncode == 0 and " passed" in ran.stdout, f"M8 proof failed: {node}\n{ran.stdout[-1200:]}"
