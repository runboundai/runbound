"""Approval requirements as a set (TPD §4b step 6, §5.4, §12 item 12; R0 closes §11a item 1).

``requirements(outcome, code_approval, matching_ask_rules, fallback_operative, service_default_reqs, tag_reqs)``
builds the requirements a call needs, only from sources applicable to the decision:

* the **code-declared approval** is always present when the tool has one, and nothing overrides it;
* one requirement per **matching tool-level ``ask`` rule**;
* the **service-default** and **tag** requirements only when the fallback is operative (no tool rule matched),
  so an explicit tool ``allow`` drops a service default's ask but never the code's;
* two tags that each gate approval are two requirements, never a union of approver lists;
* nothing at all unless the decision is ``ask`` (a ``deny`` or ``refuse`` asks nobody).

A requirement is satisfied by a decision made for *that* requirement. ``states`` gives each one of
``satisfied | pending | expired | revoked | denied`` (``denied`` is the decision a person took against it; the
TPD's "timeout or denial of any requirement is deny" needs both). The call is authorised **only when every
requirement is satisfied**; partial satisfaction never authorises; a one-time grant is consumed for the one
requirement it was given for.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .evaluator import Outcome

SATISFIED, PENDING, EXPIRED, REVOKED, DENIED = "satisfied", "pending", "expired", "revoked", "denied"
STATES = (SATISFIED, PENDING, EXPIRED, REVOKED, DENIED)

CODE = "code"


@dataclass(frozen=True)
class Requirement:
    """One thing that must be approved: where it came from, who may decide it, how long to wait for it.

    ``eligible`` is the set of approver ids; an empty set means any admin (the plane's rule). ``wait_s`` is how
    long this requirement waits; ``None`` means the default.
    """

    source: str
    eligible: frozenset = frozenset()
    wait_s: "float | None" = None


@dataclass(frozen=True)
class Decision:
    """A person's decision on the request raised for one requirement (``source``).

    ``verdict`` is ``approve``, ``deny``, ``expire`` (nobody answered in time) or ``revoke`` (a grant withdrawn
    after it was given). One decision is for exactly one requirement.
    """

    approver: str
    source: str
    verdict: str = "approve"


def requirements(
    outcome: Outcome,
    code_approval: "Requirement | None" = None,
    matching_ask_rules: Iterable[Requirement] = (),
    fallback_operative: bool = False,
    service_default_reqs: Iterable[Requirement] = (),
    tag_reqs: Iterable[Requirement] = (),
) -> list:
    """The requirements this call needs, in a fixed order: code, matching ask rules, service defaults, tags."""
    if outcome.decision != "ask":
        return []
    found: list = []
    if code_approval is not None:
        found.append(code_approval)
    found.extend(matching_ask_rules)
    if fallback_operative:
        found.extend(service_default_reqs)
        found.extend(tag_reqs)
    seen: set = set()
    unique = []
    for requirement in found:  # the same source twice is one requirement
        if requirement.source not in seen:
            seen.add(requirement.source)
            unique.append(requirement)
    return unique


def states(
    reqs: Sequence[Requirement],
    decisions: Iterable[Decision],
    *,
    one_decider_satisfies_all: bool = False,
) -> dict:
    """``{source: state}`` for each requirement.

    A decision counts for its own requirement. With ``one_decider_satisfies_all`` (the service says so) an
    ``approve`` also satisfies every other requirement that approver is eligible under; otherwise a decider
    eligible under two requirements must decide each. A ``deny``, ``expire`` or ``revoke`` is never shared.
    """
    decisions = list(decisions)
    by_source = {req.source: req for req in reqs}
    out: dict = {}
    for req in reqs:
        verdicts = [d.verdict for d in decisions if d.source == req.source]
        if "deny" in verdicts:
            out[req.source] = DENIED
        elif "revoke" in verdicts:
            out[req.source] = REVOKED
        elif "expire" in verdicts:
            out[req.source] = EXPIRED
        elif "approve" in verdicts:
            out[req.source] = SATISFIED
        elif one_decider_satisfies_all and any(
            d.verdict == "approve" and d.approver in req.eligible and d.source in by_source for d in decisions
        ):
            out[req.source] = SATISFIED
        else:
            out[req.source] = PENDING
    return out


def authorised(reqs: Sequence[Requirement], state_by_source: dict) -> bool:
    """True only when there is at least one requirement and every one is satisfied.

    No requirements is **not** authorised: an ``ask`` that names nobody to ask must not read as permission.
    """
    return bool(reqs) and all(state_by_source.get(req.source) == SATISFIED for req in reqs)


def after_decisions(reqs: Sequence[Requirement], state_by_source: dict) -> str:
    """``allow`` (every requirement satisfied), ``deny`` (any denied, expired or revoked, or none to ask) or ``wait``."""
    if not reqs:
        return "deny"
    values = [state_by_source.get(req.source, PENDING) for req in reqs]
    if any(value in (DENIED, EXPIRED, REVOKED) for value in values):
        return "deny"
    return "allow" if all(value == SATISFIED for value in values) else "wait"


def wait_s(reqs: Sequence[Requirement]) -> "float | None":
    """How long to wait: the shortest of the requirements that name one, else ``None`` (the default)."""
    named = [req.wait_s for req in reqs if req.wait_s is not None]
    return min(named) if named else None
