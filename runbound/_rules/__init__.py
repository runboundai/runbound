"""The tool-rules evaluation contract as pure functions (TPD Draft 5.3 §4b-§5.7; R0).

Private package, not exported, not in ``API.md``, and **not imported by the call path** in R0
(``tests/contract/test_evaluator_isolated.py`` asserts it): data in, decision out, no I/O, no clock, no
globals. It exists so the contract is written down and proved before any engine change ships it.

* :mod:`.tri`        three-valued logic (true / false / unknown) for conditions and groups
* :mod:`.evaluator`  §4b steps 1-6: ``evaluate(ceiling, rules, fallback, limits) -> Outcome``
* :mod:`.approvals`  §4b step 6, §5.4: approval requirements as a set, each satisfied separately
* :mod:`.states`     §5.7: fresh / cached / unavailable and what each does to a call
"""
