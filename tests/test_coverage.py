"""``coverage()``, ``assert_guarded()`` and the "nothing is guarded" warning.

``init()`` computes nothing. The sensors are ``wrap()`` (and ``auto_wrap``),
``@runbound.tool``, ``session()`` and ``record_call()`` — so a process that
called ``init()`` and wired up none of them is blind while reporting green.
These tests are about the SDK being honest about that: counting what it can
see, naming the provider SDKs it cannot, and saying so out loud after a minute
of silence.

The provider list is monkeypatched almost everywhere on purpose: whether
``openai`` happens to be in ``sys.modules`` depends on which other test file
ran first, and a diagnostic that only works in a particular collection order is
not a diagnostic.
"""

import logging
import sys
import threading
import time
import types

import pytest

import runbound
from runbound import _coverage, api, autowrap
from runbound.config import GuardrailConfig


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


# --- duck-typed clients, like the rest of the wrapper suite ------------------


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self):
        self.model = "gpt-4o"
        self.usage = FakeUsage(prompt_tokens=10, completion_tokens=5)


class FakeCompletions:
    def create(self, **kwargs):
        return FakeResponse()


def fake_client():
    """Something shaped like ``client.chat.completions.create`` and nothing else."""
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions()),
        base_url="http://fake.local/v1",
    )


def only_providers(monkeypatch, *names: str) -> None:
    """Make the coverage report see exactly ``names`` as the known providers."""
    monkeypatch.setattr(_coverage, "PROVIDER_MODULES", tuple(names))


def dummy_module(monkeypatch, name: str) -> None:
    """Make ``name`` look imported, without displacing it if it really is.

    The report only ever asks whether the name is in ``sys.modules``, and
    replacing a genuine ``openai`` with a stub would change what the rest of
    the SDK can do while the test runs.
    """
    if name not in sys.modules:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def wait_for(predicate, timeout: float = 3.0) -> bool:
    """Poll until ``predicate`` holds or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- the report itself ------------------------------------------------------


def test_coverage_before_init_reports_zeros(monkeypatch):
    only_providers(monkeypatch)

    report = runbound.coverage()

    assert report == {
        "auto_wrapped": [],
        "wrapped_clients": 0,
        "decorated_tools": 0,
        "decorated_tool_names": [],
        "guarded_calls": 0,
        "tool_calls_seen": 0,
        "keyed_sessions_seen": 0,
        "providers_imported": [],
        "providers_unguarded": [],
        "last_guarded_call_age_s": None,
        "warnings": [],
        "refusals": "default",  # Wave 23: no plane/local profile before init()
    }


def test_coverage_reports_zeros_rather_than_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("the report is broken")

    monkeypatch.setattr(_coverage, "snapshot", boom)

    assert runbound.coverage()["guarded_calls"] == 0


def test_auto_wrapped_mirrors_what_is_patched_right_now():
    runbound.init()

    assert runbound.coverage()["auto_wrapped"] == autowrap.patched()

    runbound.unpatch()
    assert runbound.coverage()["auto_wrapped"] == []


# --- counting the sensors ---------------------------------------------------


def test_wrapping_a_client_is_counted_once_however_often_it_is_wrapped():
    runbound.init(auto_wrap=False)
    client = fake_client()

    runbound.wrap(client)
    assert runbound.coverage()["wrapped_clients"] == 1

    runbound.wrap(client)  # already guarded: nothing was patched
    assert runbound.coverage()["wrapped_clients"] == 1


def test_every_tool_decoration_is_counted():
    runbound.init(auto_wrap=False)

    @runbound.tool
    def search(query):
        return query

    @runbound.tool(name="renamed")
    def other():
        return None

    assert runbound.coverage()["decorated_tools"] == 2


def test_every_attempted_tool_call_is_counted():
    runbound.init(auto_wrap=False)

    @runbound.tool
    def search(query):
        return query

    search("a")
    search("b")

    assert runbound.coverage()["tool_calls_seen"] == 2


def test_keyed_sessions_are_counted_per_key_not_per_block():
    runbound.init(auto_wrap=False)

    with runbound.session("alice"):
        pass
    with runbound.session("alice"):
        pass
    with runbound.session("bob"):
        pass

    assert runbound.coverage()["keyed_sessions_seen"] == 2


def test_a_guarded_call_through_a_wrapped_client_is_counted():
    runbound.init(auto_wrap=False)
    client = runbound.wrap(fake_client())

    client.chat.completions.create(model="gpt-4o", messages=[])

    report = runbound.coverage()
    assert report["guarded_calls"] == 1
    assert report["last_guarded_call_age_s"] == pytest.approx(0.0, abs=1.0)


def test_record_call_and_the_llm_decorator_count_as_guarded_calls():
    runbound.init(auto_wrap=False)

    runbound.record_call("llama-3.1-8b", 10, 5, provider="llama.cpp@local")

    @runbound.llm(model="llama-3.1-8b", provider="llama.cpp@local")
    def generate(prompt):
        return prompt

    generate("hi")

    assert runbound.coverage()["guarded_calls"] == 2


def test_a_failed_call_is_a_guarded_call_too():
    runbound.init(auto_wrap=False)

    runbound.record_call(
        "gpt-4o", 0, 0, provider="openai@x.local", error=RuntimeError("down")
    )

    assert runbound.coverage()["guarded_calls"] == 1


def test_the_age_of_the_last_guarded_call_starts_unknown():
    runbound.init(auto_wrap=False)

    assert runbound.coverage()["last_guarded_call_age_s"] is None

    runbound.record_call("gpt-4o", 1, 1, provider="openai@x.local")
    age = runbound.coverage()["last_guarded_call_age_s"]

    assert isinstance(age, float) and age >= 0.0


# --- which SDKs are imported, and which of them nothing covers ---------------


def test_imported_provider_sdks_are_listed(monkeypatch):
    only_providers(monkeypatch, "google.genai", "boto3")
    dummy_module(monkeypatch, "google.genai")
    dummy_module(monkeypatch, "boto3")
    runbound.init(auto_wrap=False)

    assert runbound.coverage()["providers_imported"] == ["google.genai", "boto3"]


def test_a_provider_with_no_wrapper_is_unguarded_however_much_else_is_guarded(monkeypatch):
    only_providers(monkeypatch, "google.genai", "boto3")
    dummy_module(monkeypatch, "google.genai")
    dummy_module(monkeypatch, "boto3")
    runbound.init(auto_wrap=False)

    runbound.record_call("gpt-4o", 1, 1, provider="openai@x.local")

    assert runbound.coverage()["providers_unguarded"] == ["google.genai", "boto3"]


def test_openai_leaves_the_unguarded_list_once_one_call_is_seen(monkeypatch):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")
    runbound.init(auto_wrap=False)

    assert runbound.coverage()["providers_unguarded"] == ["openai"]

    runbound.record_call("gpt-4o", 1, 1, provider="openai@api.openai.com")

    assert runbound.coverage()["providers_unguarded"] == []


# --- the silent-zero warning ------------------------------------------------


def test_the_warning_names_the_imported_sdk_and_the_state_of_auto_wrap(monkeypatch):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")
    runbound.init(auto_wrap=False)

    (warning,) = runbound.coverage()["warnings"]

    assert warning == (
        "runbound sees no LLM traffic after 60s although 'openai' is imported — "
        "nothing is guarded. Did you call runbound.wrap(client)? (auto_wrap: off)"
    )


def test_the_warning_says_what_auto_wrap_did_patch(monkeypatch):
    """The parenthetical is the diagnosis; all three of its readings matter."""
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")

    warning = _coverage.silence_warning(["openai:chat", "openai:responses"])

    assert warning.endswith("(auto_wrap: on, patched openai:chat, openai:responses)")


def test_the_warning_says_when_auto_wrap_was_on_and_found_nothing(monkeypatch):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")

    warning = _coverage.silence_warning([], auto_wrap=True)

    assert warning.endswith("(auto_wrap: on, but no provider SDK was patched)")


def test_the_warning_names_several_imported_sdks(monkeypatch):
    only_providers(monkeypatch, "openai", "boto3")
    dummy_module(monkeypatch, "openai")
    dummy_module(monkeypatch, "boto3")

    warning = _coverage.silence_warning([], auto_wrap=False)

    assert "although 'openai', 'boto3' are imported" in warning


def test_there_is_no_warning_once_traffic_has_been_seen(monkeypatch):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")
    runbound.init(auto_wrap=False)

    runbound.record_call("gpt-4o", 1, 1, provider="openai@api.openai.com")

    assert runbound.coverage()["warnings"] == []


def test_there_is_no_warning_when_no_provider_sdk_is_imported(monkeypatch):
    only_providers(monkeypatch)
    runbound.init(auto_wrap=False)

    assert runbound.coverage()["warnings"] == []


# --- assert_guarded ---------------------------------------------------------


def test_assert_guarded_raises_when_a_provider_is_imported_and_nothing_is_seen(monkeypatch):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")
    runbound.init(auto_wrap=False)

    with pytest.raises(RuntimeError) as excinfo:
        runbound.assert_guarded()

    assert "nothing is guarded" in str(excinfo.value)


def test_assert_guarded_passes_after_one_guarded_call(monkeypatch):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")
    runbound.init(auto_wrap=False)

    runbound.record_call("gpt-4o", 1, 1, provider="openai@api.openai.com")

    runbound.assert_guarded()  # does not raise


def test_assert_guarded_passes_when_there_is_no_provider_to_be_blind_to(monkeypatch):
    only_providers(monkeypatch)
    runbound.init(auto_wrap=False)

    runbound.assert_guarded()


# --- the timer --------------------------------------------------------------


def test_the_coverage_check_fires_once_and_warns(monkeypatch, caplog):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")

    with caplog.at_level(logging.WARNING, logger="runbound"):
        runbound.init(auto_wrap=False, coverage_check_seconds=0.05)
        assert wait_for(
            lambda: any("no LLM traffic" in r.getMessage() for r in caplog.records)
        )

    warnings = [r.getMessage() for r in caplog.records if "no LLM traffic" in r.getMessage()]
    assert len(warnings) == 1


def test_the_coverage_check_stays_quiet_when_a_call_was_seen(monkeypatch, caplog):
    only_providers(monkeypatch, "openai")
    dummy_module(monkeypatch, "openai")

    with caplog.at_level(logging.WARNING, logger="runbound"):
        runbound.init(auto_wrap=False, coverage_check_seconds=0.05)
        runbound.record_call("gpt-4o", 1, 1, provider="openai@api.openai.com")
        assert not wait_for(
            lambda: any("no LLM traffic" in r.getMessage() for r in caplog.records),
            timeout=0.4,
        )


def test_no_check_is_armed_when_it_is_switched_off():
    runbound.init(auto_wrap=False, coverage_check_seconds=None)

    assert _coverage._TIMER is None


def test_re_initializing_does_not_leak_timer_threads():
    def timers() -> int:
        return len(
            [t for t in threading.enumerate() if t.name == _coverage.TIMER_NAME and t.is_alive()]
        )

    for _ in range(3):
        runbound.init(auto_wrap=False, coverage_check_seconds=30.0)
        assert timers() <= 1

    api._teardown_for_tests()

    assert wait_for(lambda: timers() == 0)
    assert _coverage._TIMER is None


# --- configuration ----------------------------------------------------------


def test_auto_wrap_is_on_and_the_check_is_a_minute_by_default():
    config = GuardrailConfig()

    assert config.auto_wrap is True
    assert config.coverage_check_seconds == 60.0


@pytest.mark.parametrize("value", [0, -1.0])
def test_a_non_positive_coverage_check_is_rejected(value):
    with pytest.raises(ValueError, match="coverage_check_seconds"):
        GuardrailConfig(coverage_check_seconds=value).validate()


def test_the_coverage_check_can_be_switched_off():
    GuardrailConfig(coverage_check_seconds=None).validate()
