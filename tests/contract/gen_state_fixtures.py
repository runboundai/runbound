"""Writes tests/contract/fixtures/states_*.json: hand-authored expectations from TPD §5.7 and §12 items 7 and 10."""

from __future__ import annotations

import json
import pathlib

OUT = pathlib.Path(__file__).parent / "fixtures"
VALIDITY = {"fresh_s": 30, "cache_s": 86400}
SERVICE = {"read": "open", "write": "open", "external": "open", "financial": "closed", "destructive": "closed",
           "privileged": "closed", "unclassified": "closed"}


def state_cases():
    c = []

    def add(id, note, now, served_at, sealed_at, expect, validity=VALIDITY):
        c.append({"id": id, "note": note, "now": now, "served_at": served_at, "sealed_at": sealed_at, "validity": validity, "expect": expect})

    add("O-STA-1", "served 10 s ago: fresh", 1000, 990, None, "fresh")
    add("O-STA-1a", "served exactly fresh_s ago: still fresh (inclusive)", 1000, 970, None, "fresh")
    add("O-STA-2", "served long ago, a sealed cache within validity: cached", 1000, 500, 100, "cached")
    add("O-STA-2a", "never served this run, a sealed cache (a restart): cached", 1000, None, 100, "cached")
    add("O-STA-2b", "a cache exactly cache_s old is still cached (inclusive)", 87400, None, 1000, "cached")
    add("O-STA-3", "no served policy and no cache: unavailable", 1000, None, None, "unavailable")
    add("O-STA-3a", "a cache older than its validity: unavailable", 87401, None, 1000, "unavailable")
    add("O-STA-3b", "served long ago and the cache has expired too: unavailable", 90000, 100, 100, "unavailable")
    add("O-STA-4", "a cache that expires mid-run: the call at 86399 s is cached", 86399, None, 0, "cached")
    add("O-STA-4a", "...and the next call, at 86401 s, is unavailable (nothing remembers the earlier state)", 86401, None, 0, "unavailable")
    return c


def outage_cases():
    c = []

    def add(id, note, state, effects, expect, code=None, cached=None, service=SERVICE):
        c.append({"id": id, "note": note, "state": state, "effects": effects, "fail_mode_by_tag": service,
                  "code": code or {}, "cached": cached, "expect": expect})

    add("O-STA-5", "fresh: the served policy decides", "fresh", ["destructive"], {"decision": "evaluate", "reason": "served_policy"})
    add("O-STA-6", "cached: the sealed policy's ask is enforced in full", "cached", ["destructive"],
        {"decision": "ask", "reason": "cached_policy", "fail_mode_applied": False}, cached="ask")
    add("O-STA-6a", "cached: its deny is enforced", "cached", ["read"], {"decision": "deny", "fail_mode_applied": False}, cached="deny")
    add("O-STA-6b", "cached: fail mode does not apply even to a destructive tool: the sealed allow stands", "cached", ["destructive"],
        {"decision": "allow", "fail_mode_applied": False}, cached="allow")
    add("O-STA-6c", "cached: a code-declared approval is a floor under a sealed allow", "cached", ["read"],
        {"decision": "ask"}, code={"approval": True}, cached="allow")
    add("O-STA-7", "unavailable: a destructive tool with no rule refuses", "unavailable", ["destructive"],
        {"decision": "refuse", "reason": "plane_unavailable", "fail_mode_applied": True})
    add("O-STA-7a", "unavailable: a read tool runs per the service's fail mode (open)", "unavailable", ["read"],
        {"decision": "allow", "reason": "fail_open", "fail_mode_applied": True})
    add("O-STA-7b", "unavailable: a read tool refuses when the service set its fail mode closed", "unavailable", ["read"],
        {"decision": "refuse", "reason": "plane_unavailable"}, service={**SERVICE, "read": "closed"})
    add("O-STA-7c", "unavailable: a tool with no declared effect is unclassified, and unclassified is closed", "unavailable", [],
        {"decision": "refuse", "reason": "plane_unavailable"})
    add("O-STA-7d", "unavailable: any closed tag closes it (read + destructive)", "unavailable", ["read", "destructive"],
        {"decision": "refuse"})
    add("O-STA-7e", "unavailable: a tag with no setting is closed, never open", "unavailable", ["external"],
        {"decision": "refuse"}, service={"read": "open"})
    # K8: a bare write is unclassified for fail mode
    add("O-STA-7f", "K8: a bare write has no tag, so it is unclassified, and unclassified is closed", "unavailable", ["write"],
        {"decision": "refuse", "reason": "plane_unavailable"})
    add("O-STA-7g", "K8: read + write is still a bare write: unclassified, closed", "unavailable", ["read", "write"],
        {"decision": "refuse", "reason": "plane_unavailable"})
    add("O-STA-7h", "K8: write + external has a tag; external stays per its setting (open)", "unavailable", ["write", "external"],
        {"decision": "allow", "reason": "fail_open"})
    add("O-STA-7i", "K8: ...and closed when the service closes external", "unavailable", ["write", "external"],
        {"decision": "refuse"}, service={**SERVICE, "external": "closed"})
    add("O-STA-7j", "K8: 'unless the service default says otherwise': an open unclassified default lets a bare write run", "unavailable", ["write"],
        {"decision": "allow", "reason": "fail_open"}, service={**SERVICE, "unclassified": "open"})
    add("O-STA-7k", "a read alone is still a read: open", "unavailable", ["read"], {"decision": "allow", "reason": "fail_open"})
    # K7: the last sealed policy, even expired, bounds fail-open
    add("O-STA-10", "K7: expired cache that denied the tool: refused whatever fail mode says (read, open)", "unavailable", ["read"],
        {"decision": "refuse", "reason": "policy_expired_restricted", "fail_mode_applied": False}, cached="deny")
    add("O-STA-10a", "K7: expired cache that required a human: refused (external, open)", "unavailable", ["external"],
        {"decision": "refuse", "reason": "policy_expired_restricted"}, cached="ask")
    add("O-STA-10b", "K7: expired cache that allowed a read tool: fail mode as written (open -> allow)", "unavailable", ["read"],
        {"decision": "allow", "reason": "fail_open", "fail_mode_applied": True}, cached="allow")
    add("O-STA-10c", "K7: expired cache that allowed a destructive tool: fail mode as written (closed -> refuse)", "unavailable", ["destructive"],
        {"decision": "refuse", "reason": "plane_unavailable", "fail_mode_applied": True}, cached="allow")
    add("O-STA-10d", "K7: no cache at all: fail mode as written (read open)", "unavailable", ["read"],
        {"decision": "allow", "reason": "fail_open"}, cached=None)
    add("O-STA-10e", "K7: an expired deny still refuses when every tag is open", "unavailable", ["read"],
        {"decision": "refuse", "reason": "policy_expired_restricted"}, cached="deny", service={t: "open" for t in SERVICE})
    add("O-STA-10f", "K7: a code predicate's refusal stands before the expired policy is consulted", "unavailable", ["read"],
        {"decision": "deny", "reason": "code_constraint"}, code={"constraint_refused": True}, cached="deny")
    add("O-STA-8", "unavailable: a code-declared approval refuses whatever fail mode says", "unavailable", ["read"],
        {"decision": "refuse", "reason": "approval_unverifiable", "fail_mode_applied": False}, code={"approval": True})
    add("O-STA-8a", "...even when every tag is open", "unavailable", ["read"],
        {"decision": "refuse", "reason": "approval_unverifiable"}, code={"approval": True}, service={t: "open" for t in SERVICE})
    add("O-STA-9", "a code predicate's refusal stands when fresh", "fresh", ["read"], {"decision": "deny", "reason": "code_constraint"}, code={"constraint_refused": True})
    add("O-STA-9a", "...when cached, whatever the sealed policy allows", "cached", ["read"],
        {"decision": "deny", "reason": "code_constraint"}, code={"constraint_refused": True}, cached="allow")
    add("O-STA-9b", "...when unavailable, even for a read tool failing open", "unavailable", ["read"],
        {"decision": "deny", "reason": "code_constraint"}, code={"constraint_refused": True})
    add("O-STA-9c", "a code block stands in every state", "cached", ["read"], {"decision": "deny", "reason": "code_blocked"},
        code={"blocked": True}, cached="allow")
    return c


if __name__ == "__main__":
    (OUT / "states_state.json").write_text(json.dumps(state_cases(), indent=1) + "\n")
    (OUT / "states_outage.json").write_text(json.dumps(outage_cases(), indent=1) + "\n")
    print(len(state_cases()), len(outage_cases()))
