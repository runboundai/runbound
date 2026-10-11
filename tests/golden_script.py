"""The golden fixture: what runbound observably does at the tool boundary.

This is not a pytest test module (no ``test_`` prefix; ``tests/test_behaviour_golden.py``
is the test that reads it). It is the one place the scenario is defined, so the
capture that produced the fixture and the check that guards it can never drift
apart. Both call :func:`run_scenario`.

THE SCENARIO HAS THREE PHASES
=============================
**Phase one** walks every way a decorated tool can be refused or allowed, in
the default (unkeyed) session and two keyed ones, under
``init(capabilities={"financial": "deny"})``:

* ``limited_tool`` (``max_calls=1``): the first call is allowed, the second is
  refused by the policy alone (``detector="policy"``, ``rule="max_calls"``).
* ``financial_tool`` (``effects={"financial"}``): refused by the class rule
  alone (``detector="capability"``).
* ``both_tool`` (``effects={"financial"}`` and ``blocked=True``): a tool that
  breaks *both* rules. The hook chain judges the policy rules (slot 3) before
  the capability class (slot 4), so the call is refused by the policy. This is
  the invariant this fixture pins: swap slots 3 and 4 and it reports the
  capability. (``max_calls`` cannot pin it: only calls that clear the chain
  count, and this tool never clears it.)
* ``blocked_tool`` (``blocked=True``), ``constrained_tool`` (a ``constraint``
  that refuses amounts over 100) and ``approval_tool`` (a ``require_approval``
  that says no) each refuse with their own rule; ``constrained_tool`` is also
  called inside the limit and allowed.
* ``limited_tool`` once more inside a second keyed session, to pin that
  ``max_calls`` is counted per session.

**Phase two** re-``init()``s with a ``block_and_latch`` tool policy and walks
one fresh keyed session (``golden:latch``): the denied tool latches it, and the
next, otherwise allowed, tool is refused with the stored anomaly. Another key is
untouched.

**Phase three** re-``init()``s with ``max_actions_per_run=2``: the third call is
refused at the door (``detector="fanout"``) and a refused attempt is not
counted as executed.

Every phase tears down completely before and after, so no registry entry,
latch or engine state leaks from one phase into the next.

WHAT IS COMPARED, AND WHAT IS DELIBERATELY EXCLUDED
====================================================
:func:`run_scenario` returns a plain, JSON-able dict:

* ``refusals`` — for each call that raised, in order: the exception class
  name, ``detector``, ``rule`` (``details["rule"]``), ``reason`` (``exc.reason``),
  ``boundary`` and ``level`` (from ``exc.decision``). **Not** the message text:
  it is prose, expected to be reworded over time.
* ``anomalies`` — every anomaly a recording observer actually saw, as
  ``(detector, severity, reacted, details)``, with ``session_id``, ``anomaly_id``,
  ``key_hash`` and ``decision`` stripped from ``details`` (the first three are
  per-run or hashed, ``decision`` is covered by ``refusals``).
* ``sessions`` — each session's counters (``tool_calls``, ``admitted``,
  ``executed``, ``refused``) and its latched detector, or ``None``.

Excluded on purpose: wall-clock values, the per-process salted ``args_hash``
(only *events* carry one, and events are not compared), and set ordering
(``effects`` is stored sorted).

REGENERATING
============
Regenerating the fixture is a decision to be justified in a commit message,
never a way to make a red test green::

    python -m tests.golden_script --write

If ``test_behaviour_golden.py`` is red, the right fix is almost always to the
code the scenario is exercising, not to the fixture.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import runbound
from runbound import _coverage, api

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES_DIR / "behaviour_golden.json"

KEY_SECOND = "golden:second"
KEY_LATCH = "golden:latch"
KEY_OTHER = "golden:other"

PHASE_ONE_KWARGS: dict[str, Any] = {"capabilities": {"financial": "deny"}}
PHASE_TWO_KWARGS: dict[str, Any] = {
    "tool_policy": {"deny": ["golden_wire"], "on_violation": "block_and_latch"},
}
PHASE_THREE_KWARGS: dict[str, Any] = {"max_actions_per_run": 2}


class _Recorder:
    """Collects every anomaly a recording observer sees, verbatim."""

    def __init__(self) -> None:
        self.anomalies: list = []

    def on_event(self, session: Any, event: Any) -> None:  # pragma: no cover - unused
        pass

    def on_anomaly(self, session: Any, anomaly: Any, reacted: str) -> None:
        self.anomalies.append((anomaly, reacted))


def _refusal(exc: BaseException) -> dict:
    anomaly = getattr(exc, "anomaly", None)
    details = getattr(anomaly, "details", None) or {}
    decision = getattr(exc, "decision", None)
    return {
        "exception": type(exc).__name__,
        "detector": getattr(anomaly, "detector", None),
        "rule": details.get("rule"),
        "reason": getattr(exc, "reason", None),
        "boundary": getattr(decision, "boundary", None),
        "level": getattr(decision, "level", None),
    }


def _attempt(refusals: list, fn: Any, *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        refusals.append(_refusal(exc))


def _session_summary(state: Any) -> dict:
    return {
        "tool_calls": dict(state.tool_calls),
        "admitted": state.admitted_actions,
        "executed": state.executed_actions,
        "refused": state.refused_actions,
        "latched_by": None if state.tripped_by is None else state.tripped_by.detector,
    }


def _anomalies(recorder: _Recorder) -> list:
    hidden = ("session_id", "anomaly_id", "key_hash", "decision")
    return [
        [
            anomaly.detector,
            anomaly.severity,
            reacted,
            {k: v for k, v in anomaly.details.items() if k not in hidden},
        ]
        for anomaly, reacted in recorder.anomalies
    ]


# --- phase one: every way a tool is allowed or refused -------------------------


def _run_phase_one() -> dict:
    runbound.init(**PHASE_ONE_KWARGS)
    recorder = _Recorder()
    api._ENGINE.observers.append(recorder)
    refusals: list = []

    @runbound.tool(name="golden_limited", effects={"read"}, max_calls=1)
    def limited_tool() -> str:
        return "done"

    @runbound.tool(name="golden_financial", effects={"financial"})
    def financial_tool() -> str:
        return "done"

    @runbound.tool(name="golden_both", effects={"financial"}, blocked=True)
    def both_tool() -> str:
        return "done"

    @runbound.tool(name="golden_blocked", effects={"read"}, blocked=True)
    def blocked_tool() -> str:
        return "done"

    @runbound.tool(
        name="golden_constrained", effects={"read"}, constraint=lambda call: call.kwargs.get("amount", 0) <= 100
    )
    def constrained_tool(amount: int) -> str:
        return "done"

    @runbound.tool(name="golden_approval", effects={"read"}, require_approval=lambda call: False)
    def approval_tool() -> str:
        return "done"

    sessions: dict = {}

    _attempt(refusals, limited_tool)  # 1st call: allowed
    _attempt(refusals, limited_tool)  # 2nd call: refused by the policy's max_calls alone
    _attempt(refusals, financial_tool)  # refused by the class rule alone
    _attempt(refusals, both_tool)  # breaks both rules: the policy (slot 3) is judged before the capability
    _attempt(refusals, blocked_tool)
    _attempt(refusals, constrained_tool, amount=50)  # allowed
    _attempt(refusals, constrained_tool, amount=500)
    _attempt(refusals, approval_tool)
    sessions["default"] = _session_summary(runbound.current_session())

    with runbound.session(KEY_SECOND) as second:
        _attempt(refusals, limited_tool)  # max_calls is per session: allowed again
    sessions[KEY_SECOND] = _session_summary(second)

    return {"refusals": refusals, "anomalies": _anomalies(recorder), "sessions": sessions}


# --- phase two: a latch, on purpose ----------------------------------------------


def _run_phase_two() -> dict:
    runbound.init(**PHASE_TWO_KWARGS)
    recorder = _Recorder()
    api._ENGINE.observers.append(recorder)
    refusals: list = []

    @runbound.tool(name="golden_wire")
    def wire_tool() -> str:
        return "done"

    @runbound.tool(name="golden_lookup")
    def lookup_tool() -> str:
        return "done"

    sessions: dict = {}
    with runbound.session(KEY_LATCH) as latched:
        _attempt(refusals, wire_tool)  # denied: latches the session
        _attempt(refusals, lookup_tool)  # allowed on its own, refused by the latch
        _attempt(refusals, lookup_tool)
    with runbound.session(KEY_OTHER) as other:
        _attempt(refusals, lookup_tool)  # another key is untouched
    sessions[KEY_LATCH] = _session_summary(latched)
    sessions[KEY_OTHER] = _session_summary(other)

    return {"refusals": refusals, "anomalies": _anomalies(recorder), "sessions": sessions}


# --- phase three: the action cap -------------------------------------------------


def _run_phase_three() -> dict:
    runbound.init(**PHASE_THREE_KWARGS)
    recorder = _Recorder()
    api._ENGINE.observers.append(recorder)
    refusals: list = []

    @runbound.tool(name="golden_ping")
    def ping_tool() -> str:
        return "done"

    for _ in range(4):  # the 3rd and 4th attempts cross max_actions_per_run=2
        _attempt(refusals, ping_tool)

    return {
        "refusals": refusals,
        "anomalies": _anomalies(recorder),
        "sessions": {"default": _session_summary(runbound.current_session())},
    }


def run_scenario() -> dict:
    """Run the three phases of the fixed scenario and return the semantic outputs.

    Tears down completely before, between and after the phases, exactly like
    every other test in this suite, so nothing leaks in or out.
    """
    result: dict = {}
    for name, phase in (("rules", _run_phase_one), ("latching", _run_phase_two), ("action_cap", _run_phase_three)):
        api._teardown_for_tests()
        _coverage.reset_for_tests()
        try:
            result[name] = phase()
        finally:
            api._teardown_for_tests()
            _coverage.reset_for_tests()
    return result


# --- regeneration entry point ------------------------------------------------------


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.golden_script",
        description=(
            "Golden fixture regeneration. See the module docstring: regenerating is a "
            "decision to justify in a commit message, never a way to make a red test green."
        ),
    )
    parser.add_argument("--write", action="store_true", help="Rewrite the fixture from the scenario as it exists now.")
    parser.add_argument("--print", dest="show", action="store_true", help="Print the scenario's JSON to stdout.")
    args = parser.parse_args(argv)

    if args.write:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        with open(FIXTURE, "w") as fh:
            json.dump(run_scenario(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"wrote {FIXTURE}")
        return 0
    if args.show:
        print(json.dumps(run_scenario(), sort_keys=True))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
