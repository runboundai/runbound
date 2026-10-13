"""Writes tests/contract/fixtures/approvals_*.json (hand-authored expectations from TPD §4b step 6 and §12 item 12)."""

from __future__ import annotations

import json
import pathlib

OUT = pathlib.Path(__file__).parent / "fixtures"


def req(source, eligible=(), wait_s=None):
    return {"source": source, "eligible": sorted(eligible), "wait_s": wait_s}


def build_cases():
    ask = {"decision": "ask", "primary_reason": "rule"}
    cases = [
        {"id": "O-APR-1", "note": "code-declared approval alone: one requirement, the code's",
         "outcome": ask, "code_approval": req("code", ["sec"]), "expect": ["code"]},
        {"id": "O-APR-2", "note": "code approval plus a matching ask rule: two requirements, never merged",
         "outcome": ask, "code_approval": req("code", ["sec"]), "matching_ask": [req("rule:r1", ["lead"])], "expect": ["code", "rule:r1"]},
        {"id": "O-APR-3", "note": "an explicit tool allow drops the service default's ask but never the code's",
         "outcome": {"decision": "ask", "primary_reason": "code_approval"}, "code_approval": req("code"),
         "fallback_operative": False, "service": [req("service:destructive", ["oncall"])], "expect": ["code"]},
        {"id": "O-APR-3a", "note": "with no code approval an explicit allow leaves nothing to ask",
         "outcome": ask, "fallback_operative": False, "service": [req("service:destructive")], "expect": []},
        {"id": "O-APR-4", "note": "service-default requirements apply only when the fallback is operative",
         "outcome": {"decision": "ask", "primary_reason": "fallback"}, "fallback_operative": True,
         "service": [req("service:default", ["oncall"])], "expect": ["service:default"]},
        {"id": "O-APR-4a", "note": "two tags that each gate approval are two requirements",
         "outcome": {"decision": "ask", "primary_reason": "fallback"}, "fallback_operative": True,
         "tags": [req("tag:financial", ["cfo"]), req("tag:destructive", ["oncall"])], "expect": ["tag:financial", "tag:destructive"]},
        {"id": "O-APR-4b", "note": "tag requirements keep their own approver lists (never a union)",
         "outcome": {"decision": "ask", "primary_reason": "fallback"}, "fallback_operative": True,
         "tags": [req("tag:financial", ["cfo"]), req("tag:destructive", ["oncall"])], "expect": ["tag:financial", "tag:destructive"],
         "expect_eligible": {"tag:financial": ["cfo"], "tag:destructive": ["oncall"]}},
        {"id": "O-APR-4c", "note": "tag requirements are dropped when a tool rule matched (fallback not operative)",
         "outcome": ask, "matching_ask": [req("rule:r1", ["lead"])], "fallback_operative": False,
         "tags": [req("tag:financial", ["cfo"])], "expect": ["rule:r1"]},
        {"id": "O-APR-5", "note": "several matching ask rules: one requirement each", "outcome": ask,
         "matching_ask": [req("rule:a", ["x"]), req("rule:b", ["y"])], "expect": ["rule:a", "rule:b"]},
        {"id": "O-APR-6", "note": "a deny asks nobody", "outcome": {"decision": "deny", "primary_reason": "rule"},
         "code_approval": req("code"), "matching_ask": [req("rule:r1")], "expect": []},
        {"id": "O-APR-6a", "note": "a refuse asks nobody", "outcome": {"decision": "refuse", "primary_reason": "condition_unevaluable"},
         "code_approval": req("code"), "expect": []},
        {"id": "O-APR-6b", "note": "an allow asks nobody", "outcome": {"decision": "allow", "primary_reason": "rule"}, "expect": []},
        {"id": "O-APR-7", "note": "the same source twice is one requirement", "outcome": ask,
         "matching_ask": [req("rule:r1", ["a"]), req("rule:r1", ["a"])], "expect": ["rule:r1"]},
    ]
    waits = [
        {"id": "O-APR-W1", "note": "wait is the shortest named", "reqs": [req("a", [], 900), req("b", [], 300), req("c")], "expect_wait": 300},
        {"id": "O-APR-W2", "note": "none named: the default (null)", "reqs": [req("a"), req("b")], "expect_wait": None},
        {"id": "O-APR-W3", "note": "no requirements: null", "reqs": [], "expect_wait": None},
    ]
    a, b = req("a", ["ana", "bo"]), req("b", ["ana", "cy"])
    flow = [
        {"id": "O-APR-S1", "note": "one satisfied, one pending: not authorised", "reqs": [a, b],
         "decisions": [{"approver": "ana", "source": "a"}], "expect_states": {"a": "satisfied", "b": "pending"},
         "expect_authorised": False, "expect_after": "wait"},
        {"id": "O-APR-S2", "note": "both satisfied: authorised", "reqs": [a, b],
         "decisions": [{"approver": "ana", "source": "a"}, {"approver": "cy", "source": "b"}],
         "expect_states": {"a": "satisfied", "b": "satisfied"}, "expect_authorised": True, "expect_after": "allow"},
        {"id": "O-APR-S3", "note": "a one-time approval consumed for a does not satisfy b, though the decider is eligible under both",
         "reqs": [a, b], "decisions": [{"approver": "ana", "source": "a"}], "shared": False,
         "expect_states": {"a": "satisfied", "b": "pending"}, "expect_authorised": False, "expect_after": "wait"},
        {"id": "O-APR-S4", "note": "the service flag lets one decider eligible under both satisfy both",
         "reqs": [a, b], "decisions": [{"approver": "ana", "source": "a"}], "shared": True,
         "expect_states": {"a": "satisfied", "b": "satisfied"}, "expect_authorised": True, "expect_after": "allow"},
        {"id": "O-APR-S5", "note": "the flag does not help a decider who is not eligible under b",
         "reqs": [a, b], "decisions": [{"approver": "bo", "source": "a"}], "shared": True,
         "expect_states": {"a": "satisfied", "b": "pending"}, "expect_authorised": False, "expect_after": "wait"},
        {"id": "O-APR-S6", "note": "denial of any requirement is deny", "reqs": [a, b],
         "decisions": [{"approver": "ana", "source": "a"}, {"approver": "cy", "source": "b", "verdict": "deny"}],
         "expect_states": {"a": "satisfied", "b": "denied"}, "expect_authorised": False, "expect_after": "deny"},
        {"id": "O-APR-S7", "note": "timeout of any requirement is deny", "reqs": [a, b],
         "decisions": [{"approver": "ana", "source": "a"}, {"approver": "cy", "source": "b", "verdict": "expire"}],
         "expect_states": {"a": "satisfied", "b": "expired"}, "expect_authorised": False, "expect_after": "deny"},
        {"id": "O-APR-S8", "note": "a revoked requirement is deny", "reqs": [a, b],
         "decisions": [{"approver": "ana", "source": "a"}, {"approver": "cy", "source": "b", "verdict": "revoke"}],
         "expect_states": {"a": "satisfied", "b": "revoked"}, "expect_authorised": False, "expect_after": "deny"},
        {"id": "O-APR-S9", "note": "a denial is never shared by the flag", "reqs": [a, b],
         "decisions": [{"approver": "ana", "source": "a", "verdict": "deny"}], "shared": True,
         "expect_states": {"a": "denied", "b": "pending"}, "expect_authorised": False, "expect_after": "deny"},
        {"id": "O-APR-S10", "note": "no requirements is not authorised (an ask that names nobody is not permission)", "reqs": [],
         "decisions": [], "expect_states": {}, "expect_authorised": False, "expect_after": "deny"},
        {"id": "O-APR-S11", "note": "nothing decided: pending, wait", "reqs": [a], "decisions": [],
         "expect_states": {"a": "pending"}, "expect_authorised": False, "expect_after": "wait"},
    ]
    return cases, waits, flow


if __name__ == "__main__":
    cases, waits, flow = build_cases()
    (OUT / "approvals_requirements.json").write_text(json.dumps(cases, indent=1) + "\n")
    (OUT / "approvals_waits.json").write_text(json.dumps(waits, indent=1) + "\n")
    (OUT / "approvals_flow.json").write_text(json.dumps(flow, indent=1) + "\n")
    print(len(cases), len(waits), len(flow))
