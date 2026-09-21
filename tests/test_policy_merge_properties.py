"""The merge algebra as a property: merging can only ever remove freedom.

``merge`` folds an org (remote) policy into the local one, and the safety of
fleet-wide policy rests on one sentence: **the merged policy never permits a
call that either input refuses.** ``test_policy_merge.py`` asserts that rule
by rule on hand-written examples; this file asserts the sentence itself over
randomly generated policy pairs, where the combinations nobody thought to
write by hand live.

Deterministic on purpose. Generation is the stdlib ``random`` module seeded
with ``SEED``, so a failure reproduces exactly and the SDK's test
dependencies stay what they are (pytest, and nothing else). One run generates
``PAIRS`` (500) policy pairs and probes every pair that merges with each of 7
tools at each of 4 attempt counts: 425 merged pairs, 11,900 probes, 8,626 of
them a call one input refuses. The other 75 pairs state an org approval rule
with no local callback to ask, which ``merge`` refuses to build at all.

Two conventions the generator follows. Both come from what ``merge``
documents; neither is invented here:

* **A remote policy is wire-shaped.** It arrives over the network and cannot
  carry code, so it states no ``constraints`` and no ``approval_callback``.
  Generating a remote predicate would only "discover" the documented fact
  that callables are never taken from the org side.
* **A remote approval rule is answered by the local callback**, because that
  is precisely what the merged policy does with it — so that is the callback
  the remote input is evaluated with here. Where there is no local callback
  to ask, ``merge`` refuses to build the policy at all (``ValueError``) and
  nothing is permitted; those pairs are counted and skipped.
"""

import random
from dataclasses import replace

import pytest

from runbound.policy import (
    ON_VIOLATION_MODES,
    ToolCall,
    ToolPolicy,
    coerce,
    evaluate,
    merge,
)

#: Fixed so a failure reproduces exactly.
SEED = 20260913

#: How many random policy pairs one run covers.
PAIRS = 500

#: The tools a generated policy may name. ``undeclared_tool`` is deliberately
#: never named by either side, so an allow-list's inversion is probed too.
TOOLS = (
    "lookup_order",
    "issue_refund",
    "send_email",
    "wire_money",
    "delete_account",
    "search",
)
PROBE_TOOLS = TOOLS + ("undeclared_tool",)

#: ``calls_so_far`` values each probe is made at; 1 is the first attempt.
ATTEMPTS = (1, 2, 3, 5)

#: How strict each mode is, lowest first — the same ranking ``merge`` uses.
_STRICTNESS = {"dry_run": 0, "block": 1, "block_and_latch": 2}


class _Boom(Exception):
    """What a constraint that cannot answer raises (fail-closed → refusal)."""


def _always(answer: bool):
    def predicate(_call: ToolCall) -> bool:
        return answer

    return predicate


def _raises(_call: ToolCall) -> bool:
    raise _Boom("this gate cannot answer")


def _approves(approved: frozenset):
    def callback(call: ToolCall) -> bool:
        return call.name in approved

    return callback


def _sample(rng: random.Random, population, weight: float = 0.4) -> list[str]:
    """A random subset of ``population``, each member kept with ``weight``."""
    return [item for item in population if rng.random() < weight]


def _local_policy(rng: random.Random) -> ToolPolicy | None:
    """A policy a customer could have written — callables and all.

    ``None`` sometimes, because "no local policy" is a configuration people
    actually run. Every generated policy passes :meth:`ToolPolicy.validate`
    the way one that survived ``init()`` would: ``allow`` and ``deny`` never
    name the same tool, and nothing requires an approval there is nobody to
    ask for.
    """
    if rng.random() < 0.125:
        return None
    deny = _sample(rng, TOOLS, 0.25)
    allow = None
    if rng.random() < 0.4:
        allow = [tool for tool in _sample(rng, TOOLS, 0.5) if tool not in deny]
    constraints = {}
    for tool in _sample(rng, TOOLS, 0.3):
        constraints[tool] = _raises if rng.random() < 0.2 else _always(rng.random() < 0.5)
    callback = _approves(frozenset(_sample(rng, TOOLS, 0.5))) if rng.random() < 0.85 else None
    policy = ToolPolicy(
        deny=deny,
        allow=allow,
        max_calls={tool: rng.randint(1, 4) for tool in _sample(rng, TOOLS, 0.3)},
        constraints=constraints,
        require_approval=_sample(rng, TOOLS, 0.25) if callback is not None else [],
        approval_callback=callback,
        on_violation=rng.choice(ON_VIOLATION_MODES),
    )
    policy.validate()
    return policy


def _remote_policy(rng: random.Random) -> "ToolPolicy | dict | None":
    """An org policy as it comes off the wire: rules, never code.

    Half the time as a dict — the wire form, in which a missing
    ``on_violation`` key means the org has no opinion about the mode.
    """
    if rng.random() < 0.125:
        return None
    deny = _sample(rng, TOOLS, 0.25)
    stated: dict = {
        "deny": deny,
        "max_calls": {tool: rng.randint(1, 4) for tool in _sample(rng, TOOLS, 0.3)},
        "require_approval": _sample(rng, TOOLS, 0.15),
    }
    if rng.random() < 0.4:
        stated["allow"] = [
            tool for tool in _sample(rng, TOOLS, 0.5) if tool not in deny
        ]
    if rng.random() < 0.7:
        stated["on_violation"] = rng.choice(ON_VIOLATION_MODES)
    return stated if rng.random() < 0.5 else ToolPolicy(**stated)


def _refuses(policy: ToolPolicy | None, call: ToolCall, attempts: int) -> bool:
    """Would ``policy`` refuse this call? A policy that is not there never does."""
    return policy is not None and evaluate(policy, call, attempts) is not None


def _as_enforced(remote: ToolPolicy | None, local: ToolPolicy | None) -> ToolPolicy | None:
    """The remote policy with the callback its approval rule would be asked of.

    A remote policy carries no code, so on its own every tool it requires
    approval for reads as "nobody to ask" — which is not what the *merged*
    policy does with that rule: it asks the local callback. Evaluating the
    remote input with that same callback is what makes the comparison below
    about the algebra rather than about the wire format.
    """
    if remote is None:
        return None
    callback = None if local is None else local.approval_callback
    return replace(remote, approval_callback=callback)


def _strictness(mode: str) -> int:
    return _STRICTNESS[mode]


def test_the_merge_never_permits_a_call_either_input_refuses():
    """500 random policy pairs, 11,900 probes over the 425 that merge."""
    rng = random.Random(SEED)
    refusals = 0
    unbuildable = 0
    probes = 0

    for pair in range(PAIRS):
        local = _local_policy(rng)
        remote_written = _remote_policy(rng)
        remote_dry_run = rng.random() < 0.3
        remote = coerce(remote_written)

        try:
            merged = merge(local, remote_written, remote_dry_run=remote_dry_run)
        except ValueError:
            # An org rule the merged policy could not enforce — an approval
            # with nobody to ask. Nothing is permitted at all, so the
            # property holds trivially; counted so it cannot quietly become
            # the only thing this test sees.
            unbuildable += 1
            continue

        enforced_remote = _as_enforced(remote, local)
        for tool in PROBE_TOOLS:
            for attempts in ATTEMPTS:
                call = ToolCall(tool, (), {}, "user:1", {})
                refused_by_input = _refuses(local, call, attempts) or _refuses(
                    enforced_remote, call, attempts
                )
                probes += 1
                if not refused_by_input:
                    continue
                refusals += 1
                assert _refuses(merged, call, attempts), (
                    f"pair {pair}: merge permitted {tool!r} at attempt "
                    f"{attempts}, which an input refuses\n"
                    f"local={local!r}\nremote={remote!r}\nmerged={merged!r}"
                )

    assert probes == (PAIRS - unbuildable) * len(PROBE_TOOLS) * len(ATTEMPTS)
    # Not a vacuous pass: the generator really does produce refusals to check,
    # and really does produce policies that can be merged.
    assert refusals > 1000
    assert unbuildable < PAIRS // 2


def test_the_merged_mode_is_never_looser_than_either_input():
    """The `mode = stricter wins` row of the algebra, over the same pairs."""
    rng = random.Random(SEED)
    checked = 0

    for _ in range(PAIRS):
        local = _local_policy(rng)
        remote_written = _remote_policy(rng)
        remote_dry_run = rng.random() < 0.3
        remote = coerce(remote_written)
        try:
            merged = merge(local, remote_written, remote_dry_run=remote_dry_run)
        except ValueError:
            continue
        if merged is None:
            continue
        checked += 1
        if local is not None:
            assert _strictness(merged.on_violation) >= _strictness(local.on_violation)
        states_mode = not isinstance(remote_written, dict) or "on_violation" in remote_written
        if remote is not None and states_mode and not remote_dry_run:
            assert _strictness(merged.on_violation) >= _strictness(remote.on_violation)

    assert checked > PAIRS // 2


@pytest.mark.parametrize("attempts", ATTEMPTS)
def test_a_denied_tool_is_refused_however_the_other_side_permits_it(attempts):
    """The generator's premise, pinned: deny beats a permissive allow-list."""
    merged = merge(
        ToolPolicy(allow=["wire_money"]),
        ToolPolicy(deny=["wire_money"]),
    )

    assert evaluate(merged, ToolCall("wire_money", (), {}, None, {}), attempts) is not None
