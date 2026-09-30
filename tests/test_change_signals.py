"""The runtime says what changed, and how posture moved.

Two local record kinds join ``runbound.events()``:

- ``"runtime_change"``: a value this process runs on — the model a call
  used, the provider it went to, the policy or Controls version the plane
  delivered — differs from the last one seen. Never on first sight (there
  is nothing to change *from*), never again while it stays the same.
- ``"posture"``, now carrying where it came ``from``, its ``scope``
  (``"session"`` or ``"process"``), the ladder ``level`` when the ladder
  moved it, and the session ``key`` (raw here, in this process; the wire
  carries only its hash).

Every model call's event also carries its ``provider`` label. None of it
holds a prompt, a reply or a tool argument.
"""

import asyncio

import pytest

import runbound
from runbound import api, ladder, local_events
from runbound.config import GuardrailConfig
from runbound.shared import RemoteState

from test_coverage_warnings import FakeAnthropic, FakeOpenAI, FakeStreamingCompletions


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class _Observer:
    """An engine observer that keeps every event it is shown."""

    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, session, event) -> None:
        self.events.append(event)

    def on_anomaly(self, session, anomaly, reacted) -> None:
        pass


def _observe() -> _Observer:
    observer = _Observer()
    api._ENGINE.observers.append(observer)
    return observer


def _changes(what: str | None = None) -> list:
    return [
        e
        for e in runbound.events(n=-1)
        if e["kind"] == "runtime_change" and (what is None or e["what"] == what)
    ]


def _postures() -> list:
    return [e for e in runbound.events(n=-1) if e["kind"] == "posture"]


# --- provider on every model call -------------------------------------------


def test_record_call_carries_its_provider_onto_the_event():
    runbound.init()
    seen = _observe()

    runbound.record_call("llama-3.1-8b", 10, 20, provider="llama.cpp@local")

    assert [e.provider for e in seen.events] == ["llama.cpp@local"]


def test_a_wrapped_openai_call_carries_the_wrappers_provider_label():
    runbound.init(auto_wrap=False)
    seen = _observe()
    client = runbound.wrap(FakeOpenAI(responses=False))

    client.chat.completions.create(model="gpt-4o", messages=[])

    assert [e.provider for e in seen.events] == ["openai@default"]


def test_a_wrapped_anthropic_call_carries_the_wrappers_provider_label():
    runbound.init(auto_wrap=False)
    seen = _observe()
    client = runbound.wrap(FakeAnthropic())

    client.messages.create(model="claude-sonnet-4-5", messages=[])

    assert [e.provider for e in seen.events] == ["anthropic@default"]


def test_a_streamed_call_carries_the_provider_label_too():
    runbound.init(auto_wrap=False)
    seen = _observe()
    fake = type("Client", (), {})()
    fake.chat = type("Chat", (), {})()
    fake.chat.completions = FakeStreamingCompletions()
    client = runbound.wrap(fake)

    for _ in client.chat.completions.create(model="gpt-4o", messages=[], stream=True):
        pass

    assert [e.provider for e in seen.events] == ["openai@default"]


def test_an_abandoned_stream_is_recorded_with_its_provider():
    runbound.init()
    seen = _observe()

    api._HOOKS.abandoned("gpt-4o", 10, 5, 0.3, "openai@default", True)

    assert [(e.provider, e.partial) for e in seen.events] == [("openai@default", True)]


def test_a_report_that_does_not_know_provider_still_works():
    """A ``report`` callable written before provider existed (a customer's
    own, or an older integration) is still handed the call, just without it."""
    from runbound.wrappers import _call_report

    got = []

    def old_report(model, tokens_in, tokens_out, duration_s, reasoning, cached, cache_write):
        got.append((model, tokens_in, tokens_out))

    _call_report(old_report, "gpt-4o", 1, 2, 0.1, 0, 0, 0, provider="openai@default")

    assert got == [("gpt-4o", 1, 2)]


# --- runtime_change: model and provider --------------------------------------


def test_the_first_model_seen_is_not_a_change():
    runbound.init()

    runbound.record_call("gpt-4o", 10, 20, provider="openai@default")

    assert _changes() == []


def test_the_same_model_again_and_again_is_not_a_change():
    runbound.init()

    for _ in range(5):
        runbound.record_call("gpt-4o", 10, 20, provider="openai@default")

    assert _changes() == []


def test_a_changed_model_is_exactly_one_runtime_change():
    runbound.init()

    runbound.record_call("gpt-4o", 10, 20, provider="openai@default")
    runbound.record_call("gpt-4o", 10, 20, provider="openai@default")
    for _ in range(3):
        runbound.record_call("gpt-4.1", 10, 20, provider="openai@default")

    changes = _changes("model")
    assert len(changes) == 1
    change = changes[0]
    assert (change["from"], change["to"]) == ("gpt-4o", "gpt-4.1")
    assert isinstance(change["at"], float)
    assert set(change) == {"kind", "at", "what", "from", "to"}


def test_a_changed_provider_is_a_runtime_change_of_its_own():
    runbound.init()

    runbound.record_call("gpt-4o", 10, 20, provider="openai@api.openai.com")
    runbound.record_call("gpt-4o", 10, 20, provider="openai@eu.example")

    assert [(c["from"], c["to"]) for c in _changes("provider")] == [
        ("openai@api.openai.com", "openai@eu.example")
    ]
    assert _changes("model") == []


def test_a_call_with_no_model_neither_counts_nor_resets_the_last_one():
    runbound.init()

    runbound.record_call("gpt-4o", 10, 20)
    runbound.record_call(None, 10, 20)
    runbound.record_call("gpt-4o", 10, 20)

    assert _changes("model") == []


def test_note_runtime_value_never_records_on_first_sight():
    assert local_events.note_runtime_value("sdk_version", "0.4.0") is False
    assert _changes() == []


def test_note_runtime_value_records_only_a_real_change():
    local_events.note_runtime_value("policy_version", 3)
    assert local_events.note_runtime_value("policy_version", 3) is False
    assert local_events.note_runtime_value("policy_version", 4) is True

    assert [(c["from"], c["to"]) for c in _changes("policy_version")] == [(3, 4)]


def test_clear_for_tests_forgets_every_last_value():
    local_events.note_runtime_value("model", "a")
    local_events.clear_for_tests()

    assert local_events.note_runtime_value("model", "b") is False
    assert _changes() == []


# --- runtime_change: policy and Controls versions -----------------------------


class _PlaneClient:
    """Just enough of a plane client for the two fetches."""

    def __init__(self) -> None:
        self.policy_body: dict = {}
        self.controls_body: dict = {}

    def policy(self, service):
        return self.policy_body

    def controls(self, service):
        return self.controls_body


def test_a_new_policy_version_is_a_runtime_change():
    client = _PlaneClient()
    remote = RemoteState(client, None, GuardrailConfig())

    client.policy_body = {"version": 4, "policy": {"deny": ["wire"]}}
    remote._fetch_policy(4)
    client.policy_body = {"version": 5, "policy": {"deny": ["wire", "refund"]}}
    remote._fetch_policy(5)
    remote._fetch_policy(5)

    assert [(c["from"], c["to"]) for c in _changes("policy_version")] == [(4, 5)]


def test_a_new_controls_version_is_a_runtime_change():
    client = _PlaneClient()
    remote = RemoteState(client, None, GuardrailConfig())

    client.controls_body = {"version": 1, "controls": {}}
    remote._fetch_controls(1)
    client.controls_body = {"version": 2, "controls": {}}
    remote._fetch_controls(2)

    assert [(c["from"], c["to"]) for c in _changes("controls_version")] == [(1, 2)]


# --- posture transitions: session scope ---------------------------------------


def test_a_manual_session_posture_records_from_to_scope_and_key():
    runbound.init()

    with runbound.session("user:8842") as state:
        state.enter_safe_mode("support asked", "restricted")
        state.exit_safe_mode()

    entered, left = _postures()
    assert entered["scope"] == "session"
    assert entered["key"] == "user:8842"
    assert (entered["from"], entered["posture"], entered["source"]) == ("full", "restricted", "manual")
    assert entered["level"] is None
    assert (left["from"], left["posture"], left["source"]) == ("restricted", "full", "manual")


def test_a_posture_that_does_not_move_records_nothing():
    runbound.init()

    with runbound.session("user:8842") as state:
        state.enter_safe_mode("once", "restricted")
        state._enter_posture("restricted", "again", source="manual")
        state._exit_posture(source="ladder")  # not the ladder's entry: nothing lifts

    assert len(_postures()) == 1


def test_a_manual_narrowing_over_another_records_the_one_it_replaced():
    runbound.init()

    with runbound.session("user:8842") as state:
        state._enter_posture("restricted", "limited", source="ladder", level="limited")
        state.enter_safe_mode("stop it", "stopped")

    replaced = _postures()[-1]
    assert (replaced["from"], replaced["posture"]) == ("restricted", "stopped")


def test_the_ladder_narrowing_a_session_records_its_level():
    runbound.init(on_spike="limit", on_anomaly="warn", spike_warmup_calls=4, spike_confirm=2,
                  spike_limit_calls=2, spike_min_duration_s=1.0)

    for _ in range(15):
        with runbound.session("user:8842"):
            api._record_llm_call("gpt-4o", 10, 100, duration_s=2.0)
    for _ in range(2):
        with runbound.session("user:8842"):
            api._record_llm_call("gpt-4o", 10, 100, duration_s=400.0)

    ladder_moves = [p for p in _postures() if p["source"] == "ladder"]
    assert ladder_moves, "the ladder never narrowed the session"
    first = ladder_moves[0]
    assert (first["scope"], first["from"], first["posture"], first["level"]) == (
        "session",
        "full",
        "restricted",
        "limited",
    )
    assert first["key"] == "user:8842"


def test_clear_lifts_a_session_posture_and_records_it():
    runbound.init()

    with runbound.session("user:8842") as state:
        state.enter_safe_mode("hold", "restricted")
    runbound.clear("user:8842")

    lifted = _postures()[-1]
    assert (lifted["from"], lifted["posture"], lifted["scope"]) == ("restricted", "full", "session")
    assert lifted["reason"] == "cleared"


def test_ladder_level_names_cover_every_level():
    assert [ladder.LEVEL_NAMES[level] for level in ladder.LEVELS] == [
        "quiet",
        "watching",
        "limited",
        "closed",
        "blocked",
    ]


# --- posture transitions: process scope --------------------------------------


def test_a_process_posture_records_scope_process_and_what_it_replaced():
    runbound.init()

    runbound.enter_safe_mode("incident", "restricted")
    runbound.exit_safe_mode()

    entered, left = _postures()
    assert (entered["scope"], entered["from"], entered["posture"]) == ("process", "full", "restricted")
    assert (left["scope"], left["from"], left["posture"]) == ("process", "restricted", "full")
    assert "key" not in entered


# --- sinks --------------------------------------------------------------------


def test_every_sink_sees_every_record_after_the_on_event_callback():
    order = []
    runbound.init(on_event=lambda r: order.append(("callback", r["kind"])))
    local_events.add_sink(lambda r: order.append(("sink", r["kind"])))

    runbound.enter_safe_mode("x", "restricted")

    assert order == [("callback", "posture"), ("sink", "posture")]


def test_a_sink_that_raises_never_breaks_the_caller_or_the_next_sink(caplog):
    seen = []

    def broken(record):
        raise RuntimeError("sink bug")

    runbound.init()
    local_events.add_sink(broken)
    local_events.add_sink(seen.append)

    runbound.enter_safe_mode("x", "restricted")  # does not raise
    runbound.record_call("gpt-4o", 1, 1)
    runbound.record_call("gpt-4.1", 1, 1)  # a change, dispatched through the broken sink

    assert [r["kind"] for r in seen] == ["posture", "runtime_change"]


def test_a_removed_sink_hears_nothing_more():
    seen = []
    local_events.add_sink(seen.append)
    local_events.remove_sink(seen.append)
    local_events.remove_sink(seen.append)  # removing twice is harmless

    local_events.record_runtime_change("model", "a", "b")

    assert seen == []


def test_records_carry_no_prompt_reply_or_tool_argument():
    runbound.init(auto_wrap=False)
    fake = FakeOpenAI(responses=False)
    client = runbound.wrap(fake)

    @runbound.tool
    def refund(user: str, amount: float):
        return "ok"

    with runbound.session("user:8842") as state:
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "SECRET-PROMPT"}])
        refund(user="SECRET-ARG", amount=1.0)
        fake.chat.completions.model = "gpt-4.1"  # the fake answers with its own model name
        client.chat.completions.create(model="gpt-4.1", messages=[{"role": "user", "content": "SECRET-PROMPT"}])
        state.enter_safe_mode("x", "restricted")

    blob = repr(runbound.events(n=-1))
    assert "SECRET" not in blob
    assert _changes("model") and _postures()


def test_async_wrapped_calls_carry_the_provider_too():
    from test_coverage_warnings import FakeAsyncResponses  # noqa: F401  (module import check)

    class _AsyncCompletions:
        async def create(self, **kwargs):
            from test_coverage_warnings import FakeUsage

            response = type("R", (), {})()
            response.model = kwargs.get("model")
            response.usage = FakeUsage(prompt_tokens=3, completion_tokens=4)
            return response

    runbound.init(auto_wrap=False)
    seen = _observe()
    fake = type("Client", (), {})()
    fake.chat = type("Chat", (), {})()
    fake.chat.completions = _AsyncCompletions()
    client = runbound.wrap(fake)

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=[]))

    assert [e.provider for e in seen.events] == ["openai@default"]


# --- the exporter's changes lane ------------------------------------------------


class _Spy:
    service = "checkout"
    worker_id = "host-1:1"
    key_state = "ok"

    def __init__(self) -> None:
        self.batches: list = []

    def events(self, batch: dict) -> bool:
        self.batches.append(batch)
        return True


def _exporter(**kwargs):
    from runbound.export import Exporter

    client = _Spy()
    return Exporter(client, **kwargs), client


def test_the_exporter_posts_changes_on_their_own_lane():
    exporter, client = _exporter()
    exporter.on_change({"kind": "runtime_change", "at": 1_790_000_000.0, "what": "model", "from": "a", "to": "b"})
    exporter.on_change(
        {
            "kind": "posture",
            "at": 1_790_000_001.0,
            "source": "ladder",
            "posture": "restricted",
            "from": "full",
            "scope": "session",
            "level": "limited",
            "key": "user:8842",
            "reason": "limited: a confirmed spike on this session",
            "session_id": "s-1",
        }
    )

    exporter.flush()
    (batch,) = client.batches
    runtime, posture = batch["changes"]
    assert runtime == {
        "ts_wall": runtime["ts_wall"],
        "kind": "runtime_change",
        "what": "model",
        "from": "a",
        "to": "b",
    }
    assert runtime["ts_wall"].startswith("2026-")
    assert posture["kind"] == "posture_change"
    assert (posture["scope"], posture["from"], posture["to"], posture["source"], posture["level"]) == (
        "session",
        "full",
        "restricted",
        "ladder",
        "limited",
    )
    assert posture["key_hash"] and len(posture["key_hash"]) == 64
    assert batch["events"] == [] and batch["anomalies"] == []


def test_absent_fields_are_omitted_not_sent_as_null():
    exporter, client = _exporter()
    exporter.on_change(
        {"kind": "posture", "at": 1_790_000_000.0, "source": "manual", "posture": "restricted",
         "from": "full", "scope": "process", "level": None, "reason": "incident"}
    )

    exporter.flush()
    (change,) = client.batches[0]["changes"]
    assert set(change) == {"ts_wall", "kind", "scope", "from", "to", "source", "reason"}


def test_the_exporter_ignores_every_other_record_kind():
    exporter, client = _exporter()
    for kind in ("anomaly", "refusal", "decision"):
        exporter.on_change({"kind": kind, "at": 1.0})

    assert exporter.pending == 0


def test_a_posture_change_is_sent_even_with_events_export_off_a_runtime_change_is_not():
    """A posture change is state the fleet acts on, like a trip; which model
    a call used is telemetry, and ``export_events=False`` silences it."""
    exporter, client = _exporter(include_events=False)
    exporter.on_change({"kind": "runtime_change", "at": 1.0, "what": "model", "from": "a", "to": "b"})
    exporter.on_change({"kind": "posture", "at": 1.0, "source": "manual", "posture": "stopped",
                        "from": "full", "scope": "process", "reason": "x"})

    exporter.flush()
    assert [c["kind"] for c in client.batches[0]["changes"]] == ["posture_change"]


def test_a_refused_changes_batch_is_resent_whole():
    exporter, client = _exporter()
    exporter.on_change({"kind": "runtime_change", "at": 1.0, "what": "model", "from": "a", "to": "b"})
    taken = exporter._take()
    batch, priority, anomalies, events = taken
    exporter._requeue(batch["batch_id"], priority, anomalies, events)

    again, *_ = exporter._take()
    assert again["batch_id"] == batch["batch_id"]
    assert again["changes"] == batch["changes"]


def test_start_listens_for_changes_and_stop_stops_listening():
    exporter, client = _exporter(flush_every_s=3600.0)
    exporter.start()
    try:
        local_events.record_runtime_change("model", "a", "b")
        assert exporter.pending == 1
    finally:
        exporter.stop(0.5)
    local_events.record_runtime_change("model", "b", "c")
    assert exporter.pending == 0
    assert [c["to"] for b in client.batches for c in b["changes"]] == ["b"]


def test_a_broken_change_record_is_counted_dropped_never_raised():
    exporter, client = _exporter()
    exporter.on_change({"kind": "posture", "at": "not a time"})

    assert exporter.dropped == 1
