"""Legacy fixtures (R0, AC15/AC16): today's SDK decision for every per-tool policy row shape and every concrete row.

The cases and the harness live in ``tests/legacy/legacy_script.py`` (not a test module itself, see its docstring),
defined once so this file can never drift from what produced ``fixtures/legacy_rows.json``. This file only re-runs
each case on today's engine and compares. A red case here is a regression unless the R0 register
(docs/product/tool-rules-r0-register.md §2) lists the change with its test; regenerating the fixture is never the fix.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

from . import legacy_script as legacy

pytestmark = pytest.mark.legacy

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "legacy_rows.json"
with open(FIXTURE) as _fh:
    CASES: dict = json.load(_fh)["cases"]

ROW_FIELDS = ("blocked", "max_calls_per_session", "quota", "approval_required", "approval_mode", "approval_timeout_s",
              "risk_class", "fail_mode", "state", "approvers", "headline", "identifies")
FOLD_FIELDS = ("blocked", "max_calls_per_session", "approval_required", "approval_mode", "approval_timeout_s",
               "risk_class", "fail_mode", "quota")
CALL_KEYS = {"decision", "exception", "reason", "detector", "rule", "boundary", "retryable", "retry_after_ceil_s", "ran",
             "would_refuse", "approval_raised", "quota_takes", "lease_spent"}


def _number(case_id: str) -> int:
    return int(case_id.rsplit("-", 1)[1])


def _runs(result: dict) -> list:
    """Every engine run of a case: one for a field/pair/document case, one per probe for a concrete row."""
    outcome = result["outcome"]
    return [outcome] if "calls" in outcome else [outcome[name] for name in sorted(outcome)]


@pytest.mark.parametrize("case_id", sorted(CASES, key=_number))
def test_todays_decision_is_unchanged(case_id):
    assert legacy.run_case(case_id) == CASES[case_id]


def test_the_script_and_the_fixture_hold_the_same_cases():
    ids = [c["id"] for c in legacy.cases()]
    assert sorted(ids, key=_number) == sorted(CASES, key=_number)
    assert all(re.fullmatch(r"O-LEG-ROW-[1-9][0-9]*", i) for i in ids)


def test_every_call_records_decision_reason_tags_approval_and_quota():
    for case_id, result in CASES.items():
        for run in _runs(result):
            assert run["calls"], case_id
            for call in run["calls"]:
                assert set(call) == CALL_KEYS, case_id
                assert call["decision"] in ("allowed", "refused", "deferred"), case_id


def test_a_field_the_sdk_ignores_changes_nothing():
    """``same_as``: the named case's calls are decided exactly alike (approvers, headline, identifies, unknown keys...)."""
    paired = [(i, r["input"]["same_as"]) for i, r in CASES.items() if "same_as" in r["input"]]
    assert len(paired) >= 10
    for case_id, other in paired:
        assert CASES[case_id]["outcome"]["calls"] == CASES[other]["outcome"]["calls"], (case_id, other)


def test_every_row_field_is_captured_alone():
    alone = [r["input"] for r in CASES.values() if r["input"]["group"] == "field"]
    seen = {field for c in alone for row in c["rows"] for field in row if field != "tool"}
    assert set(ROW_FIELDS) <= seen
    states = {row.get("state") for c in alone for row in c["rows"]}
    assert {"dry_run", "enforced"} <= states
    quotas = {(row["quota"].get("per"), row["quota"].get("window")) for c in alone for row in c["rows"]
              if isinstance(row.get("quota"), dict)}
    assert {(p, w) for p in ("service", "key") for w in ("minute", "hour", "day")} <= quotas
    ranks = {row["risk_class"] for c in alone for row in c["rows"] if "risk_class" in row}
    assert set(legacy.CAPABILITY_RANKS) <= ranks
    approval_modes = {row.get("approval_mode") for c in alone for row in c["rows"] if row.get("approval_required")}
    assert {"sync", "deferred"} <= approval_modes
    links = {(row.get("fail_mode"), c["plane"]["link"]) for c in alone for row in c["rows"] if "fail_mode" in row}
    assert {(m, link) for m in ("open", "closed") for link in ("connected", "unavailable")} <= links


def test_every_fold_field_is_paired_with_a_code_rule():
    pairs = [r["input"] for r in CASES.values() if r["input"]["group"] == "pair"]
    code_keys = ("blocked", "max_calls", "approval", "mode", "timeout_s", "constraint", "require_approval")
    for field in FOLD_FIELDS:
        assert any(field in row and (any(k in c["tool"] for k in code_keys) or c["init"].get("capabilities")
                                     or c["tool"].get("effects") != ["read"])
                   for c in pairs for row in c["rows"]), field
    for code in ("constraint", "require_approval", "approval"):
        assert any(code in c["tool"] for c in pairs), code
    assert any(c["init"].get("capabilities") for c in pairs)


def test_a_pair_never_resolves_looser_than_the_code_rule_alone():
    """Where the code refuses on its own, the pair refuses too (the row can add, never loosen)."""
    for case_id, result in CASES.items():
        c = result["input"]
        if c["group"] != "pair" or not (c["tool"].get("blocked") or c["tool"].get("require_approval") == "never"):
            continue
        assert all(call["decision"] == "refused" for call in result["outcome"]["calls"]), case_id


def test_document_policy_cases_and_only_they_are_tagged_retires_with_h6():
    for case_id, result in CASES.items():
        c = result["input"]
        reads_document = c.get("policy") is not None or (c.get("plane") or {}).get("policy") is not None
        assert (legacy.H6 in c["tags"]) == reads_document == (c["group"] == "document"), case_id


def test_every_concrete_row_names_where_it_came_from():
    concrete = [r["input"] for r in CASES.values() if r["input"].get("sources")]
    assert len(concrete) >= 50
    for c in concrete:
        assert c["sources"] and all(re.fullmatch(r"(?:[a-z0-9 -]+: )?[\w./-]+\.(py|ts|md):\d+", s) for s in c["sources"]), c["sources"]


def test_the_capture_is_the_same_in_a_fresh_process():
    root = pathlib.Path(legacy.__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-m", "tests.legacy.legacy_script", "--print"], cwd=str(root), timeout=300,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == CASES
