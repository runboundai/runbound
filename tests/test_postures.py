"""Capabilities and postures — reduce autonomy instead of killing the run.

A tool declares a *set* of capability classes. A posture is a capability
contract: ``Posture.allows(effects)`` is the only thing that decides whether an
action may run. Five built-in postures, tighten-only from the plane. Safe
mode is ``posture() != "full"``.
"""

import asyncio
import logging
import pathlib

import pytest

import runbound
from runbound import _coverage, api
from runbound import engine as engine_module
from runbound import shared as shared_module
from runbound.exceptions import GuardrailTripped, PolicyViolation, SafeModeViolation
from runbound.plane_types import HelloReply
from runbound.policy import CAPABILITIES
from runbound.posture import POSTURES, Posture, tighten
from spike_test_helpers import spike_controls_body
from test_shared_state import FakePlane

KEY = "user:8842"
PLANE_URL = "https://plane.example"


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
    """A posture set only by the plane's own directive -- this suite's
    own coverage of that path, distinct from a manual
    ``runbound.enter_safe_mode`` call. This fixture is what every test
    below uses: a real ``runbound.init()`` connected to a fake plane,
    exactly ``tests/test_controls_delivery.py``'s own harness."""
    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url = url
        fake.token = token
        fake.service = service
        fake.worker_id = worker_id
        fake.timeout_s = timeout_s
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    return fake


def init_connected(**kwargs) -> None:
    """``runbound.init(**kwargs)``, connected to the ``plane`` fixture."""
    fields = {
        "control_plane_url": PLANE_URL,
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
        "control_plane_poll_s": 3600.0,
        "export_events": False,
        "auto_wrap": False,
    }
    fields.update(kwargs)
    runbound.init(**fields)


def narrow(posture_name: str) -> None:
    """The process-wide narrowing a plane directive drives, via
    ``HelloReply.posture`` -- this file's own coverage of that source, as
    opposed to a manual ``runbound.enter_safe_mode`` call."""
    api._SHARED.apply_hello(HelloReply(posture=posture_name))


def widen() -> None:
    """The plane lifting its own narrowing -- what
    ``runbound.exit_safe_mode()`` used to do."""
    api._SHARED.apply_hello(HelloReply(posture=None))


def enable_capabilities(plane: FakePlane, capabilities: dict) -> None:
    """The plane's ``Controls.capabilities`` delivered on one heartbeat
    -- this suite's own coverage of the plane-delivered path, alongside
    ``init(capabilities=...)``'s local one."""
    plane.controls_body = {"version": 1, "controls": {"capabilities": capabilities}}
    api._SHARED.apply_hello(HelloReply(controls_version=1))

#: The built-in posture table. Rows are postures, columns capability classes.
TABLE = {
    "full": dict.fromkeys(CAPABILITIES, "allow"),
    "restricted": {"read": "allow", "write": "allow", "external": "deny",
                   "financial": "deny", "destructive": "deny", "privileged": "deny"},
    "read_only": {"read": "allow", "write": "deny", "external": "deny",
                  "financial": "deny", "destructive": "deny", "privileged": "deny"},
    "no_side_effects": dict.fromkeys(CAPABILITIES, "deny"),
    "stopped": dict.fromkeys(CAPABILITIES, "deny"),
}


@pytest.fixture(autouse=True)
def _pristine():
    api._teardown_for_tests()
    _coverage.reset_for_tests()
    yield
    api._teardown_for_tests()
    _coverage.reset_for_tests()


def make_tool(effects, name="act", **rules):
    ran = []

    @runbound.tool(name=name, effects=effects, **rules)
    def act():
        ran.append(True)
        return "done"

    return act, ran


# --- the table, as pure data ------------------------------------------------


def test_the_six_classes_are_the_ones_the_model_names():
    assert CAPABILITIES == ("read", "write", "external", "financial",
                            "destructive", "privileged")


@pytest.mark.parametrize("name", sorted(TABLE))
@pytest.mark.parametrize("klass", CAPABILITIES)
def test_every_built_in_posture_matches_the_table(name, klass):
    assert POSTURES[name].allows(frozenset({klass})) == TABLE[name][klass]


def test_a_tool_is_denied_when_any_one_of_its_classes_is():
    both = frozenset({"write", "external"})
    assert POSTURES["full"].allows(both) == "allow"
    assert POSTURES["restricted"].allows(both) == "deny"  # external is denied


def test_an_unclassified_tool_runs_only_under_full():
    for name in TABLE:
        expected = "allow" if name == "full" else "deny"
        assert POSTURES[name].allows(frozenset()) == expected


def test_no_side_effects_denies_every_tool_including_read():
    assert POSTURES["no_side_effects"].allows(frozenset({"read"})) == "deny"


# --- tighten ----------------------------------------------------------------


def test_tighten_returns_the_stricter_of_two_postures():
    order = ["full", "restricted", "read_only", "no_side_effects", "stopped"]
    for i, looser in enumerate(order):
        for stricter in order[i:]:
            assert tighten(POSTURES[looser], POSTURES[stricter]).name == stricter
            assert tighten(POSTURES[stricter], POSTURES[looser]).name == stricter


def test_tighten_is_idempotent():
    for name in TABLE:
        assert tighten(POSTURES[name], POSTURES[name]).name == name


# --- the one decider --------------------------------------------------------


def _docstring_constant_ids(tree) -> set:
    """id()s of every string constant that is some node's own docstring —
    documentation, not code that could decide anything."""
    import ast

    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _effects_kwarg_constant_ids(tree) -> set:
    """id()s of every string constant sitting inside some call's
    ``effects=`` keyword argument — a tool *declaring* the capability
    classes it carries, the same way a caller's own ``@runbound.tool``
    usage would, never a decision made by comparing against one."""
    import ast

    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "effects":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    ids.add(id(sub))
    return ids


def test_effects_kwarg_constant_ids_only_exempts_the_effects_argument():
    """Unit test for the narrowing helper itself, before it is trusted to
    guard the whole package: a capability class inside ``effects=`` is
    exempt; the identical string used to *decide* something elsewhere in
    the same file — not inside an ``effects=`` argument — is not."""
    import ast

    tree = ast.parse(
        "\n".join(
            [
                "@runbound.tool(effects={'financial'}, max_calls=1)",
                "def issue_refund(): ...",
                "",
                "def _decide(effect):",
                "    return effect == 'financial'",  # NOT inside an effects= kwarg
            ]
        )
    )
    exempt = _effects_kwarg_constant_ids(tree)
    exempt_values = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) in exempt
    }
    non_exempt_values = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in exempt
    }
    assert exempt_values == {"financial"}
    assert "financial" in non_exempt_values  # the comparison in _decide is still caught


def test_only_posture_py_decides_by_capability_class():
    """`Posture.allows` is the only call site that decides (acceptance, grep).

    Code only: a capability class named in a docstring is documentation,
    and one declared on a tool via ``effects=`` is a caller stating what a
    tool carries, not a decision made by comparing against it — the whole
    point of the decorator's own docstring is to show exactly that usage.
    Both are exempted by construction (see the two helpers above), not by
    excluding a whole file: a hardcoded comparison against a capability
    class anywhere else in the package, including in a file that also
    happens to declare one via ``effects=``, is still caught.
    """
    import ast

    package = pathlib.Path(api.__file__).parent
    watched = {"external", "financial", "destructive", "privileged"}
    offenders = {}
    for path in sorted(package.glob("*.py")):
        if path.name in ("posture.py", "policy.py"):
            continue  # posture.py holds the tables; policy.py names the classes
        tree = ast.parse(path.read_text())
        exempt = _docstring_constant_ids(tree) | _effects_kwarg_constant_ids(tree)
        hits = sorted(
            {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in watched
                and id(node) not in exempt
            }
        )
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, offenders


# --- postures through the decorator ----------------------------------------

CLASS_SETS = [frozenset({k}) for k in CAPABILITIES] + [
    frozenset({"write", "external"}),
    frozenset(),
]


@pytest.mark.parametrize("posture_name", sorted(TABLE))
@pytest.mark.parametrize("effects", CLASS_SETS, ids=lambda e: "+".join(sorted(e)) or "unclassified")
def test_a_decorated_tool_is_refused_when_the_posture_denies_one_of_its_classes(plane, posture_name, effects):
    init_connected()
    act, ran = make_tool(effects, reviewed=True)
    if posture_name != "full":
        narrow(posture_name)
    verdict = POSTURES[posture_name].allows(effects)
    if verdict == "allow":
        assert act() == "done" and ran == [True]
    else:
        with pytest.raises(SafeModeViolation):
            act()
        assert ran == []


@pytest.mark.parametrize("posture_name", sorted(TABLE))
def test_an_undecorated_tool_is_never_refused_by_a_posture(plane, posture_name):
    init_connected()
    if posture_name != "full":
        narrow(posture_name)
    # _enforce_policy is folded into _admit_action (one _active()
    # resolution and one ToolCall build for posture, the class rule, the
    # action cap and the policy together); decorated=False is the exact
    # thing this test is about — an undecorated tool declares no effects, so
    # posture must never be the thing that refuses it here.
    api._admit_action("framework_search", ("query",), {}, frozenset(), False)  # does not raise


def test_the_refusal_names_the_posture_and_the_class_that_was_denied():
    runbound.init(refusals={"safe_mode": {"status": 409, "message": "Read-only right now."}})
    act, ran = make_tool({"financial", "write"}, name="issue_refund", max_calls=5)

    with runbound.session(KEY) as state:
        state.enter_safe_mode("spend is hot", posture="restricted")
        api._record_llm_call("gpt-4o", 10, 100, duration_s=1.0)  # model calls go on
        with pytest.raises(SafeModeViolation) as caught:
            act()

    exc = caught.value
    assert isinstance(exc, PolicyViolation) and isinstance(exc, GuardrailTripped)
    assert exc.refusal.status == 409 and exc.refusal.message == "Read-only right now."
    assert exc.anomaly.detector == "safe_mode"
    details = exc.anomaly.details
    assert details["posture"] == "restricted"
    assert details["denied_class"] == "financial"
    assert sorted(details["effects"]) == ["financial", "write"]
    assert (details["reason"], details["source"]) == ("spend is hot", "manual")
    assert ran == []
    assert runbound.session_status(KEY)["posture"]["name"] == "restricted"


def test_an_unclassified_refusal_says_unclassified(plane):
    init_connected()
    act, _ = make_tool(frozenset(), name="mystery", reviewed=True)
    narrow("restricted")
    with pytest.raises(SafeModeViolation) as caught:
        act()
    assert caught.value.anomaly.details["denied_class"] == "unclassified"
    assert "unclassified" in caught.value.anomaly.message


def test_a_posture_refusal_latches_nothing():
    runbound.init(on_anomaly="raise")
    act, _ = make_tool({"write"}, reviewed=True, polling=True)
    with runbound.session(KEY) as state:
        state.enter_safe_mode("hot", posture="read_only")
        for _ in range(3):
            with pytest.raises(SafeModeViolation):
                act()
        api._record_llm_call("gpt-4o", 10, 100, duration_s=1.0)
    assert runbound.is_tripped(KEY) is None


def test_the_posture_is_judged_before_the_policy_so_no_approver_is_asked(plane):
    asked = []
    init_connected()
    act, ran = make_tool({"financial"}, require_approval=lambda call: asked.append(call) or True)
    narrow("restricted")
    with pytest.raises(SafeModeViolation):
        act()
    assert asked == [] and ran == []


def test_an_async_tool_is_refused_before_its_body_is_awaited(plane):
    init_connected()
    ran = []

    @runbound.tool(effects={"external"}, reviewed=True)
    async def send_email():
        ran.append(True)

    narrow("restricted")
    with pytest.raises(SafeModeViolation):
        asyncio.run(send_email())
    assert ran == []


# --- entering, leaving, precedence -----------------------------------------


def test_posture_and_safe_mode_report_the_effective_state(plane):
    init_connected()
    assert runbound.posture() == "full"
    assert runbound.safe_mode() is False
    narrow("read_only")
    assert runbound.posture() == "read_only"
    assert runbound.safe_mode() is True
    widen()
    assert runbound.posture() == "full"
    assert runbound.safe_mode() is False


def test_enter_safe_mode_is_on_the_public_surface_again():
    """A manual local posture is free forever: enter_safe_mode/exit_safe_mode
    are on runbound.__init__'s public surface, on the process and on the
    session."""
    assert hasattr(runbound, "enter_safe_mode")
    assert hasattr(runbound, "exit_safe_mode")
    assert "enter_safe_mode" in runbound.__all__
    assert "exit_safe_mode" in runbound.__all__


def test_a_session_posture_does_not_leak_into_another_session():
    runbound.init()
    act, _ = make_tool({"write"}, reviewed=True)
    with runbound.session(KEY) as state:
        state.enter_safe_mode("hot", posture="read_only")
        assert runbound.posture() == "read_only"
    with runbound.session("user:0001"):
        assert runbound.posture() == "full"
        assert act() == "done"


def test_clear_takes_a_session_back_to_full():
    runbound.init()
    act, _ = make_tool({"write"}, reviewed=True)
    with runbound.session(KEY) as state:
        state.enter_safe_mode("hot", posture="read_only")
    runbound.clear(KEY)
    with runbound.session(KEY):
        assert act() == "done"
    assert runbound.session_status(KEY)["posture"] is None


def test_an_automatic_posture_never_replaces_or_lifts_a_manual_one():
    runbound.init()
    with runbound.session(KEY) as state:
        state.enter_safe_mode("the operator said so", posture="read_only")
        state._enter_posture("restricted", "limited", source="ladder")
        assert state.posture.name == "read_only" and state.posture.source == "manual"
        assert state._exit_posture(source="ladder") is False
        assert state.posture.source == "manual"


def test_a_manual_posture_replaces_an_automatic_one():
    runbound.init()
    with runbound.session(KEY) as state:
        state._enter_posture("restricted", "limited", source="ladder")
        state.enter_safe_mode("operator", posture="stopped")
        assert (state.posture.name, state.posture.source) == ("stopped", "manual")


# An unknown posture name from the plane's own directive is not treated
# as a caller mistake to raise loudly on -- it is untrusted wire data,
# held to the same fail-open rule as everything else a plane says
# (Engine.effective_posture / posture() already fail open to "full" on
# any exception, unknown name included).


# --- the ladder's rungs ----------------------------------------------------

NORMAL_SECONDS = 2.0
SPIKE_SECONDS = 400.0


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:  # pragma: no cover
        pass


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


def ladder(plane: FakePlane, **overrides):
    """This suite's own coverage of the plane-delivered path for the
    ladder's tuning: even though every ``spike_*``/``on_spike`` knob is a
    real, local ``init()`` keyword too, this helper delivers them all
    through the fake plane's Controls instead, ``init_connected``'s
    harness plus one extra heartbeat."""
    on_spike = overrides.pop("on_spike", "limit")
    spike_kwargs = dict(
        spike_warmup_calls=4, spike_confirm=2, spike_limit_calls=3,
        spike_cooldown_seconds=100.0, spike_max_strikes=2, spike_min_duration_s=1.0,
    )
    for name in list(overrides):
        if name in spike_kwargs:
            spike_kwargs[name] = overrides.pop(name)
    settings = dict(on_anomaly="raise")
    settings.update(overrides)
    init_connected(**settings)
    plane.controls_body = {"version": 1, "controls": spike_controls_body(on_spike, **spike_kwargs)}
    api._SHARED.apply_hello(HelloReply(controls_version=1))


def turn(duration: float = NORMAL_SECONDS, then=None):
    with runbound.session(KEY):
        api._record_llm_call("gpt-4o", 10, 100, duration_s=duration)
        if then is not None:
            return then()


def test_the_limited_rung_sets_restricted_and_healing_returns_to_full(clock, plane):
    ladder(plane)
    write, write_ran = make_tool({"external"}, name="send_email", max_calls=99)
    read, _ = make_tool({"read"}, name="lookup_order", reviewed=True)
    for _ in range(15):
        turn()
    turn(SPIKE_SECONDS)
    assert runbound.session_status(KEY)["posture"] is None  # watching is not limited
    turn(SPIKE_SECONDS)

    status = runbound.session_status(KEY)
    assert status["level"] == 2
    assert (status["posture"]["name"], status["posture"]["source"]) == ("restricted", "ladder")
    assert status["allowance_left"] == 3

    assert turn(then=read) == "done"
    with pytest.raises(SafeModeViolation):
        turn(then=write)
    assert write_ran == []

    for _ in range(6):
        turn()
    healed = runbound.session_status(KEY)
    assert healed["level"] == 1 and healed["posture"] is None
    assert turn(then=write) == "done"


def test_the_closed_rung_sets_stopped(clock, plane):
    ladder(plane)
    for _ in range(15):
        turn()
    for _ in range(2):
        turn(SPIKE_SECONDS)
    seen = None
    for _ in range(6):
        try:
            turn(SPIKE_SECONDS)
        except GuardrailTripped:
            seen = runbound.session_status(KEY)
            break
    assert seen is not None, "the ladder never closed the session"
    assert seen["posture"]["name"] == "stopped"


# --- require_rules and the consequential classes ---------------------------


@pytest.mark.parametrize("klass", ["financial", "destructive"])
def test_require_rules_refuses_an_unruled_tool_of_a_consequential_class(klass):
    runbound.init(require_rules=True)
    with pytest.raises(ValueError) as caught:

        @runbound.tool(effects={klass}, reviewed=True)
        def wire_money():
            pass

    message = str(caught.value)
    assert "wire_money" in message and klass in message and "max_calls=" in message


def test_require_rules_accepts_a_consequential_tool_with_a_real_rule():
    runbound.init(require_rules=True)

    @runbound.tool(effects={"financial", "destructive"}, max_calls=1)
    def wire_money():
        pass


def test_require_rules_still_accepts_a_reviewed_write_tool():
    runbound.init(require_rules=True)

    @runbound.tool(effects={"write", "external"}, reviewed=True)
    def send_email():
        pass


def test_require_rules_names_a_consequential_tool_already_imported_at_init():
    @runbound.tool(effects={"destructive"}, reviewed=True)
    def delete_account():
        pass

    with pytest.raises(ValueError) as caught:
        runbound.init(require_rules=True)
    assert "delete_account" in str(caught.value)


@pytest.mark.parametrize("value", ["read", {"delete"}, {"read", "nope"}, 7, {1}])
def test_an_unknown_or_malformed_effects_set_fails_at_decoration(value):
    with pytest.raises(ValueError) as caught:

        @runbound.tool(effects=value)
        def drop_table():
            pass

    assert "effects" in str(caught.value)


def test_effects_accepts_any_iterable_of_known_classes():
    @runbound.tool(effects=["read", "write"], name="a", reviewed=True)
    def a():
        pass

    @runbound.tool(effects=frozenset({"read"}), name="b", reviewed=True)
    def b():
        pass

    by_name = {e["name"]: e for e in runbound.tools()}
    assert by_name["a"]["effects"] == ["read", "write"]
    assert by_name["b"]["effects"] == ["read"]


def test_the_old_effect_keyword_is_gone():
    with pytest.raises(TypeError):

        @runbound.tool(effect="write")
        def old():
            pass


# --- class rules, delivered here through the plane's Controls --------------
#
# This suite's own coverage of the plane-delivered path: class rules also
# reach a worker through ``init(capabilities=...)`` and a custom posture
# table via ``init(postures=...)``, both real, free, local keywords -- see
# tests/test_plane_only_controls.py for that side and its tighten-only
# proof. Only the plane's own five built-in postures plus class rules are
# stated over the wire, so a custom posture table has no plane
# counterpart to test here.


def test_a_class_rule_denies_a_tool_even_under_full(plane):
    init_connected()
    enable_capabilities(plane, {"financial": "deny"})
    act, ran = make_tool({"financial"}, max_calls=1)
    assert runbound.posture() == "full"
    with pytest.raises(SafeModeViolation) as caught:
        act()
    assert caught.value.anomaly.details["denied_class"] == "financial"
    assert caught.value.anomaly.details["source"] == "class_rule"
    assert ran == []


def test_a_class_rule_of_approve_refuses_until_approvals_exist(plane):
    init_connected()
    enable_capabilities(plane, {"destructive": "approve"})
    act, ran = make_tool({"destructive"}, max_calls=1)
    with pytest.raises(SafeModeViolation) as caught:
        act()
    assert "approval" in caught.value.anomaly.message.lower()
    assert ran == []


def test_a_class_rule_leaves_other_classes_alone(plane):
    init_connected()
    enable_capabilities(plane, {"financial": "deny"})
    act, _ = make_tool({"write"}, reviewed=True)
    assert act() == "done"


# --- what coverage and the tool report say ---------------------------------


def test_coverage_shows_the_posture_and_counts_tools_under_every_class(plane):
    init_connected()
    make_tool({"read"}, name="a", reviewed=True)
    make_tool({"read", "write"}, name="b", reviewed=True)
    make_tool({"financial", "destructive"}, name="c", max_calls=1)
    make_tool(frozenset(), name="d")

    report = runbound.coverage()
    assert report["tools_by_class"] == {
        "read": 2, "write": 1, "external": 0,
        "financial": 1, "destructive": 1, "privileged": 0, "unclassified": 1,
    }
    assert report["posture"] == "full"
    assert report["safe_mode"]["plane"] is False

    narrow("read_only")
    with runbound.session(KEY) as state:
        state.enter_safe_mode("hot", posture="stopped")
        inside = runbound.coverage()
    outside = runbound.coverage()
    # posture() answers for the context it is called in: the session's inside
    # the block, the process's outside it.
    assert inside["posture"] == "stopped"
    assert outside["posture"] == "read_only"
    assert outside["safe_mode"]["plane"] is True
    assert outside["safe_mode"]["sessions"] == 1


def test_the_tool_report_carries_the_effects_as_a_sorted_list():
    make_tool({"write", "financial"}, name="issue_refund", max_calls=1)
    make_tool(frozenset(), name="lookup")
    by_name = {e["name"]: e for e in runbound.tools()}
    assert by_name["issue_refund"]["effects"] == ["financial", "write"]
    assert by_name["lookup"]["effects"] == []


# --- fail-open --------------------------------------------------------------


def test_a_bug_reading_the_posture_lets_the_call_through(plane, monkeypatch, caplog):
    init_connected()
    act, ran = make_tool({"financial"}, max_calls=1)
    narrow("stopped")

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api._ENGINE, "effective_posture", broken)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert act() == "done"
    assert ran == [True]
    assert any("posture" in record.getMessage() for record in caplog.records)


# --- the session's posture and the process's tighten, they do not shadow ----


@pytest.mark.parametrize(
    "process_posture, session_posture",
    [("read_only", "restricted"), ("restricted", "read_only")],
)
def test_effective_posture_is_the_stricter_of_session_and_process(plane, process_posture, session_posture):
    """Invariant: the strictest posture wins. A ladder that narrows one
    session to ``restricted`` must not loosen a process the plane narrowed
    further, and the reverse -- regression coverage for a bug where the
    session's posture shadowed the process's instead of tightening with it."""
    init_connected()
    narrow(process_posture)
    act, ran = make_tool({"write"})
    with runbound.session(KEY) as state:
        state._enter_posture(session_posture, "limited", source="ladder")
        assert runbound.posture() == "read_only"
        with pytest.raises(SafeModeViolation):
            act()
    assert ran == []
