"""DEF-1b: a parameter explicitly marked ``runbound.Injected`` whose class is imported only under ``TYPE_CHECKING`` (so its hint cannot be resolved at run time) no longer
makes the whole deferred tool ineligible. The marker is the only opt-in: an unmarked unresolvable hint still refuses registration, naming the parameter and the fix.

``Ghost`` below is a name nothing defines, which is what a ``TYPE_CHECKING``-only import looks like to ``typing.get_type_hints``.
"""

from typing import Annotated

import pytest

import runbound
from runbound import api, approvals
from test_def1_injected import Plane, approved, run, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def deferred(func):
    return runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)(func)


def marked(query: str, db: "Annotated[Ghost, runbound.Injected]"): ...  # noqa: F821


def test_a_marked_parameter_with_an_unresolvable_hint_registers_is_not_stored_and_is_found_by_name():
    plane = Plane()
    results = start(plane, provide={"db": lambda: "session-by-name"})
    got = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: "Annotated[Ghost, runbound.Injected]"):  # noqa: F821
        got.append((query, db))

    out = find("x", db=object())
    (create,) = plane.created()
    assert create["args"] == {"query": "x"} and "db" not in create["args"]
    assert approvals.bound_arguments("find", ("x",), {"db": object()}) == {"query": "x"}  # the placeholder never reaches a rule
    assert run(plane, approved(plane, out)) == "executed" and got == [("x", "session-by-name")]
    assert results[-1]["outcome"] == "executed"


def test_with_no_provider_by_name_and_no_default_the_request_fails_injected_unavailable():
    plane = Plane()
    results = start(plane, provide={object: lambda: "a type key cannot match a placeholder"})
    got = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: "Annotated[Ghost, runbound.Injected]"):  # noqa: F821
        got.append(db)

    out = find("x", db=object())
    assert run(plane, approved(plane, out)) == "failed" and got == []
    assert results[-1]["error"] == "injected_unavailable:db"


def test_a_default_is_used_when_the_class_is_unknown():
    plane = Plane()
    start(plane)
    got = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, tenant: "Annotated[Ghost, runbound.Injected]" = "acme"):  # noqa: F821
        got.append(tenant)

    out = find("x")
    assert run(plane, approved(plane, out)) == "executed" and got == ["acme"]


def test_an_unmarked_unresolvable_hint_still_refuses_and_says_how_to_fix_it():
    def bare(query: str, db: "Ghost"): ...  # noqa: F821

    with pytest.raises(ValueError) as caught:
        deferred(bare)
    message = str(caught.value)
    assert "type hints cannot be resolved" in message and "'db'" in message
    assert "if the app supplies 'db', annotate it Annotated[Ghost, runbound.Injected]" in message


def test_every_unmarked_parameter_still_resolves_strictly_beside_a_marked_one():
    def mixed(query: "Ghost2", db: "Annotated[Ghost, runbound.Injected]"): ...  # noqa: F821

    with pytest.raises(ValueError) as caught:
        deferred(mixed)
    assert "'query'" in str(caught.value) and "'db'" not in str(caught.value).split("annotate it")[0].replace("'query'", "")


def test_a_forward_reference_inside_annotated_is_accepted_when_marked():
    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: Annotated["Ghost", runbound.Injected]): ...  # noqa: F821

    assert approvals.tool_meta("find")[1] is True, "the console's `may be deferred` flag agrees with registration"


def test_a_resolvable_tool_is_unchanged_and_the_return_hint_is_ignored_in_the_fallback():
    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: "Annotated[Ghost, runbound.Injected]") -> "AlsoGhost":  # noqa: F821
        ...

    assert approvals.resolve_hints(lambda n: n) == {}
