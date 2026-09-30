"""Tests for the control-plane HTTP client and its background poller.

Nothing here touches the network: ``runbound.plane.urlopen`` is patched in
every test, exactly as ``tests/test_alerts.py`` does it. Time is a movable
fake so the once-a-minute warning can be tested without sleeping, and the
poller is driven with short intervals and joined rather than slept on.
"""

import json
import logging
import threading
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from runbound import plane as plane_module
from runbound.plane import Poller, PlaneClient
from runbound.plane_types import EntryDecision, HelloReply, TripReport

URL = "https://plane.example.com"


class MovableClock:
    """A monotonic clock a test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def urlopen_mock(body: dict | bytes | None = None) -> MagicMock:
    """A urlopen mock whose return value behaves like a response manager."""
    if body is None:
        payload = b"{}"
    elif isinstance(body, bytes):
        payload = body
    else:
        payload = json.dumps(body).encode("utf-8")
    mock = MagicMock()
    response = MagicMock()
    response.read.return_value = payload
    mock.return_value.__enter__.return_value = response
    return mock


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "nope", {}, None)


def client(**kwargs) -> PlaneClient:
    options = {
        "url": URL,
        "api_key": "sk-test",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    options.update(kwargs)
    return PlaneClient(**options)


def sent(mock: MagicMock) -> tuple:
    """(request, headers-lowercased, kwargs) of the single call made."""
    assert mock.call_count == 1
    request = mock.call_args.args[0]
    headers = {name.lower(): value for name, value in request.headers.items()}
    return request, headers, mock.call_args.kwargs


def body_of(request) -> dict:
    return json.loads(request.data.decode("utf-8"))


def warnings_matching(caplog, *fragments: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and all(fragment in record.getMessage() for fragment in fragments)
    ]


# --- request shape ---------------------------------------------------------


def test_hello_posts_json_to_v1_hello_with_the_identity_headers():
    mock = urlopen_mock({"org_id": "org_7", "plan": "team", "poll_s": 9.0})
    with patch.object(plane_module, "urlopen", mock):
        reply = client().hello({"sdk": "0.2.0"})

    request, headers, _ = sent(mock)
    assert request.full_url == f"{URL}/v1/hello"
    assert request.get_method() == "POST"
    assert body_of(request) == {"sdk": "0.2.0"}
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["x-runbound-service"] == "checkout"
    assert headers["x-runbound-worker"] == "host-1:42"
    assert headers["content-type"] == "application/json"
    assert headers["x-runbound-sdk"]
    assert isinstance(reply, HelloReply)
    assert reply.org_id == "org_7"


def test_hello_uses_a_two_second_timeout():
    mock = urlopen_mock({"org_id": "o"})
    with patch.object(plane_module, "urlopen", mock):
        client().hello({})
    assert sent(mock)[2]["timeout"] == 2.0


def test_the_sdk_version_header_can_be_pinned():
    mock = urlopen_mock({"org_id": "o"})
    with patch.object(plane_module, "urlopen", mock):
        client(sdk_version="9.9.9").hello({})
    assert sent(mock)[1]["x-runbound-sdk"] == "9.9.9"


def test_an_empty_api_key_omits_the_authorization_header():
    mock = urlopen_mock({"org_id": "o"})
    with patch.object(plane_module, "urlopen", mock):
        client(api_key="").hello({})
    assert "authorization" not in sent(mock)[1]


def test_a_trailing_slash_on_the_url_does_not_double_up():
    mock = urlopen_mock({"org_id": "o"})
    with patch.object(plane_module, "urlopen", mock):
        client(url=URL + "/").hello({})
    assert sent(mock)[0].full_url == f"{URL}/v1/hello"


def test_enter_posts_the_payload_with_the_configured_timeout():
    mock = urlopen_mock({"allow": True, "fleet_spend_usd": 3.5, "strikes": 2})
    with patch.object(plane_module, "urlopen", mock):
        decision = client(timeout_s=0.05).enter({"key_hash": "abc", "seq": 1})

    request, _, kwargs = sent(mock)
    assert request.full_url == f"{URL}/v1/enter"
    assert body_of(request) == {"key_hash": "abc", "seq": 1}
    assert kwargs["timeout"] == 0.05
    assert isinstance(decision, EntryDecision)
    assert decision.allow is True
    assert decision.strikes == 2


def test_trip_posts_the_wire_report_and_returns_true():
    report = TripReport(
        key_hash="abc",
        anomaly={"detector": "budget", "severity": "critical"},
        latch_ttl_s=300.0,
        strikes=3,
        generation=1,
        refused_at_door=False,
    )
    mock = urlopen_mock({})
    with patch.object(plane_module, "urlopen", mock):
        assert client(timeout_s=0.2).trip(report) is True

    request, _, kwargs = sent(mock)
    assert request.full_url == f"{URL}/v1/trip"
    assert body_of(request)["key_hash"] == "abc"
    assert body_of(request)["anomaly"] == {"detector": "budget", "severity": "critical"}
    assert kwargs["timeout"] == 0.2


def test_events_posts_the_batch_with_a_five_second_timeout():
    batch = {"service": "checkout", "events": [{"kind": "llm_call"}]}
    mock = urlopen_mock({})
    with patch.object(plane_module, "urlopen", mock):
        assert client().events(batch) is True

    request, _, kwargs = sent(mock)
    assert request.full_url == f"{URL}/v1/events"
    assert body_of(request) == batch
    assert kwargs["timeout"] == 5.0


def test_policy_is_a_get_with_the_service_in_the_query():
    mock = urlopen_mock({"deny": ["wire_transfer"]})
    with patch.object(plane_module, "urlopen", mock):
        policy = client().policy("check out")

    request, _, kwargs = sent(mock)
    assert request.full_url == f"{URL}/v1/policy?service=check+out"
    assert request.get_method() == "GET"
    assert request.data is None
    assert kwargs["timeout"] == 2.0
    assert policy == {"deny": ["wire_transfer"]}


def test_clear_posts_the_key_hash_and_returns_the_count():
    mock = urlopen_mock({"cleared": 4})
    with patch.object(plane_module, "urlopen", mock):
        assert client().clear("abc123") == 4

    request, _, kwargs = sent(mock)
    assert request.full_url == f"{URL}/v1/clear"
    assert body_of(request) == {"key_hash": "abc123"}
    assert kwargs["timeout"] == 0.5


def test_clear_reports_zero_when_the_plane_says_nothing():
    with patch.object(plane_module, "urlopen", urlopen_mock({})):
        assert client().clear("abc123") == 0


# --- failure handling ------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.hello({}),
        lambda c: c.enter({}),
        lambda c: c.policy("s"),
        lambda c: c.clear("h"),
    ],
)
def test_a_transport_failure_returns_none_and_counts(call):
    mock = MagicMock(side_effect=urllib.error.URLError("no route"))
    with patch.object(plane_module, "urlopen", mock):
        agent = client()
        assert call(agent) is None
    assert agent.consecutive_failures == 1
    assert agent.last_success is None


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.events({"events": []}),
        lambda c: c.trip(
            TripReport(
                key_hash=None,
                anomaly={},
                latch_ttl_s=None,
                strikes=0,
                generation=0,
                refused_at_door=False,
            )
        ),
    ],
)
def test_a_transport_failure_returns_false_and_counts(call):
    mock = MagicMock(side_effect=OSError("boom"))
    with patch.object(plane_module, "urlopen", mock):
        agent = client()
        assert call(agent) is False
    assert agent.consecutive_failures == 1


def test_failures_accumulate_and_success_resets_them():
    clock = MovableClock()
    agent = client(now=clock)
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=OSError("boom"))):
        agent.hello({})
        agent.hello({})
    assert agent.consecutive_failures == 2
    assert agent.key_state == "unknown"

    clock.advance(5.0)
    with patch.object(plane_module, "urlopen", urlopen_mock({"org_id": "o"})):
        assert agent.hello({}) is not None
    assert agent.consecutive_failures == 0
    assert agent.last_success == 1005.0
    assert agent.key_state == "valid"


def test_a_body_that_is_not_json_counts_as_a_failure():
    with patch.object(plane_module, "urlopen", urlopen_mock(b"<html>gateway</html>")):
        agent = client()
        assert agent.hello({}) is None
    assert agent.consecutive_failures == 1


def test_an_empty_body_is_a_success_for_the_boolean_calls():
    with patch.object(plane_module, "urlopen", urlopen_mock(b"")):
        agent = client()
        assert agent.events({"events": []}) is True
    assert agent.consecutive_failures == 0


def test_an_unserializable_payload_is_reported_not_raised():
    mock = urlopen_mock({})
    with patch.object(plane_module, "urlopen", mock):
        agent = client()
        assert agent.enter({"bad": object()}) is None
    assert mock.call_count == 0
    assert agent.consecutive_failures == 1


def test_a_partial_hello_reply_is_parsed_with_defaults():
    with patch.object(plane_module, "urlopen", urlopen_mock({"org_id": "org_7"})):
        reply = client().hello({})
    assert reply is not None
    assert reply.org_id == "org_7"
    assert reply.halt is False


def test_a_reply_that_is_not_an_object_counts_as_a_failure():
    with patch.object(plane_module, "urlopen", urlopen_mock(b"[1, 2, 3]")):
        agent = client()
        assert agent.hello({}) is None
    assert agent.consecutive_failures == 1


# --- rate-limited warning --------------------------------------------------


def test_two_failures_inside_a_minute_log_one_warning(caplog):
    clock = MovableClock()
    agent = client(now=clock)
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=OSError("boom"))):
        with caplog.at_level(logging.WARNING, logger="runbound"):
            agent.hello({})
            clock.advance(59.0)
            agent.enter({})
    assert len(warnings_matching(caplog, "control plane")) == 1


def test_a_failure_after_the_minute_logs_again(caplog):
    clock = MovableClock()
    agent = client(now=clock)
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=OSError("boom"))):
        with caplog.at_level(logging.WARNING, logger="runbound"):
            agent.hello({})
            clock.advance(61.0)
            agent.hello({})
    assert len(warnings_matching(caplog, "control plane")) == 2


# --- a rejected key --------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_key_stops_every_further_call(code, caplog):
    mock = MagicMock(side_effect=http_error(code))
    agent = client()
    with patch.object(plane_module, "urlopen", mock):
        with caplog.at_level(logging.WARNING, logger="runbound"):
            assert agent.hello({}) is None
        assert agent.key_state == "invalid"

        calls_so_far = mock.call_count
        assert agent.enter({}) is None
        assert agent.events({}) is False
        assert agent.policy("s") is None
        assert agent.clear("h") is None
        assert mock.call_count == calls_so_far

    assert len(warnings_matching(caplog, "rejected the token", "local-only")) == 1


def test_the_rejection_is_logged_once_not_once_per_call(caplog):
    mock = MagicMock(side_effect=http_error(401))
    agent = client()
    with patch.object(plane_module, "urlopen", mock):
        with caplog.at_level(logging.WARNING, logger="runbound"):
            agent.hello({})
            agent.reset_key()
            agent.hello({})
            agent.hello({})
    assert len(warnings_matching(caplog, "rejected the token")) == 2


def test_reset_key_lets_the_client_try_again():
    agent = client()
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=http_error(401))):
        agent.hello({})
    assert agent.key_state == "invalid"

    agent.reset_key()
    assert agent.key_state == "unknown"
    with patch.object(plane_module, "urlopen", urlopen_mock({"org_id": "o"})):
        assert agent.hello({}) is not None
    assert agent.key_state == "valid"


def test_other_http_errors_are_plain_failures():
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=http_error(500))):
        agent = client()
        assert agent.hello({}) is None
    assert agent.key_state == "unknown"
    assert agent.consecutive_failures == 1


# --- classifying why a call failed -------------------------------------------


def http_error_with_body(code: int, body: dict) -> urllib.error.HTTPError:
    """An ``HTTPError`` whose ``.read()`` returns ``body`` — the shape a real
    503 plane-loss response takes, unlike :func:`http_error`'s bodyless one."""
    payload = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(URL, code, "nope", {}, BytesIO(payload))


def test_a_store_down_503_is_classified_as_plane_loss():
    """Only a 503 naming the specific ``cause`` a genuine state outage
    carries (a genuine live-store outage's own path, ``app.STORE_UNAVAILABLE`` on
    the plane) is plane loss."""
    error = http_error_with_body(
        503, {"error": "plane_unavailable", "cause": "fleet_state_unavailable"}
    )
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "plane_loss"


def test_a_plain_plane_unavailable_503_is_not_plane_loss():
    """The generic body ``app.FailOpenMiddleware`` answers *any* unhandled
    SDK-path failure with (a saturated pool, a handler bug, a failed batch
    write) carries no ``cause`` at all -- must read as its own, distinct
    kind, never the specific "the plane's state is gone" one. This is the
    exact mislabel this split exists to close: before the plane's ``cause``
    field existed, this body was indistinguishable from a real state
    outage."""
    error = http_error_with_body(503, {"error": "plane_unavailable"})
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "plane_unavailable"
    assert agent.last_failure_kind != "plane_loss"


def test_a_plane_unavailable_503_with_an_unrecognized_cause_is_not_plane_loss():
    """A future cause value this SDK does not know about must not be
    mistaken for the one specific value that means plane loss."""
    error = http_error_with_body(
        503, {"error": "plane_unavailable", "cause": "something_new"}
    )
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "plane_unavailable"


def test_a_503_with_an_unrelated_body_is_a_plain_error():
    error = http_error_with_body(503, {"error": "something_else"})
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "error"


def test_a_503_with_no_readable_body_is_a_plain_error():
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=http_error(503))):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "error"


def test_a_500_is_a_plain_error_not_plane_loss():
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=http_error(500))):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "error"


def test_a_socket_timeout_is_classified_as_timeout():
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=TimeoutError("slow"))):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "timeout"


def test_a_urlerror_wrapping_a_timeout_is_classified_as_timeout():
    error = urllib.error.URLError(TimeoutError("slow"))
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "timeout"


def test_a_connection_refused_is_a_plain_error_not_a_timeout():
    error = urllib.error.URLError(ConnectionRefusedError("nope"))
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "error"


def test_last_failure_kind_starts_none_and_clears_on_success():
    agent = client()
    assert agent.last_failure_kind is None

    with patch.object(plane_module, "urlopen", MagicMock(side_effect=OSError("boom"))):
        agent.hello({})
    assert agent.last_failure_kind == "error"

    with patch.object(plane_module, "urlopen", urlopen_mock({"org_id": "o"})):
        agent.hello({})
    assert agent.last_failure_kind is None


# --- enter_with_kind: this call's own outcome, not the shared "last" one ----
#
# ``last_failure_kind`` is whole-client state (the test above proves it: a
# ``hello`` clears what an ``enter`` set). A caller with concurrent traffic on
# one client -- an entry call racing the background heartbeat, in practice --
# cannot read it back afterward and trust it still describes *that* call: a
# concurrent success on the same client can clear or overwrite it first.
# ``enter_with_kind`` hands the caller its own call's classification directly,
# at the moment this call decides it, so there is nothing to race.


def test_enter_with_kind_reports_plane_loss_for_a_store_down_503():
    error = http_error_with_body(
        503, {"error": "plane_unavailable", "cause": "fleet_state_unavailable"}
    )
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        decision, kind = client().enter_with_kind({})
    assert decision is None
    assert kind == "plane_loss"


def test_enter_with_kind_reports_plane_unavailable_for_the_generic_503():
    error = http_error_with_body(503, {"error": "plane_unavailable"})
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        decision, kind = client().enter_with_kind({})
    assert decision is None
    assert kind == "plane_unavailable"


def test_enter_with_kind_reports_error_for_an_unrelated_503_body():
    error = http_error_with_body(503, {"error": "something_else"})
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        decision, kind = client().enter_with_kind({})
    assert decision is None
    assert kind == "error"


def test_enter_with_kind_reports_timeout_for_a_socket_timeout():
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=TimeoutError("slow"))):
        decision, kind = client().enter_with_kind({})
    assert decision is None
    assert kind == "timeout"


def test_enter_with_kind_reports_none_on_success():
    mock = urlopen_mock({"allow": True})
    with patch.object(plane_module, "urlopen", mock):
        decision, kind = client().enter_with_kind({"key_hash": "abc"})
    assert isinstance(decision, EntryDecision)
    assert kind is None


def test_enter_still_returns_only_the_decision_and_still_sets_last_failure_kind():
    """``enter`` itself is unchanged: same return value, same whole-client
    bookkeeping as before -- ``enter_with_kind`` is additive, not a
    replacement other callers must migrate to."""
    error = http_error_with_body(
        503, {"error": "plane_unavailable", "cause": "fleet_state_unavailable"}
    )
    with patch.object(plane_module, "urlopen", MagicMock(side_effect=error)):
        agent = client()
        assert agent.enter({}) is None
    assert agent.last_failure_kind == "plane_loss"


# --- the poller ------------------------------------------------------------


class FakeClient:
    """Records hello() payloads and answers with a canned reply."""

    def __init__(self, reply: HelloReply | None = None, raises: bool = False) -> None:
        self.reply = reply
        self.raises = raises
        self.payloads: list[dict] = []
        self.key_state = "unknown"
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.calls = threading.Semaphore(0)

    def hello(self, payload: dict):
        self.payloads.append(payload)
        self.calls.release()
        if self.raises:
            raise RuntimeError("client exploded")
        return self.reply


def reply(**kwargs) -> HelloReply:
    fields = {
        "org_id": "org_7",
        "plan": "team",
        "entitlements": {},
        "halt": False,
        "policy_version": 3,
        "circuits": {},
        "poll_s": 5.0,
        "notice": None,
    }
    fields.update(kwargs)
    return HelloReply(**fields)


def run_poller(fake: FakeClient, on_reply, wanted: int = 2) -> Poller:
    poller = Poller(fake, poll_s=0.001, on_reply=on_reply, payload_fn=lambda: {"ping": 1})
    poller.start()
    try:
        for _ in range(wanted):
            assert fake.calls.acquire(timeout=5), "the poller stopped calling hello"
    finally:
        poller.stop()
    return poller


def test_the_poller_hands_every_reply_to_the_callback():
    seen: list[HelloReply] = []
    fake = FakeClient(reply(policy_version=4))
    poller = run_poller(fake, seen.append)

    assert not poller.is_alive()
    assert len(seen) >= 2
    assert all(item.policy_version == 4 for item in seen)
    assert fake.payloads[0] == {"ping": 1}


def test_the_poller_asks_immediately_without_waiting_a_period():
    fake = FakeClient(reply())
    poller = Poller(fake, poll_s=30.0, on_reply=lambda _: None, payload_fn=dict)
    poller.start()
    try:
        assert fake.calls.acquire(timeout=5)
    finally:
        poller.stop()
    assert not poller.is_alive()


def test_the_poller_skips_the_callback_when_there_is_no_reply():
    seen: list[HelloReply] = []
    fake = FakeClient(None)
    run_poller(fake, seen.append)
    assert seen == []


def test_a_callback_that_raises_is_logged_and_swallowed(caplog):
    def explode(_reply: HelloReply) -> None:
        raise RuntimeError("callback exploded")

    fake = FakeClient(reply())
    with caplog.at_level(logging.WARNING, logger="runbound"):
        poller = run_poller(fake, explode)

    assert not poller.is_alive()
    assert warnings_matching(caplog, "poll")


def test_a_client_that_raises_does_not_kill_the_poller(caplog):
    fake = FakeClient(reply(), raises=True)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        poller = run_poller(fake, lambda _: None)
    assert not poller.is_alive()
    assert len(fake.payloads) >= 2


def test_a_payload_function_that_raises_does_not_kill_the_poller():
    fake = FakeClient(reply())
    exploded = threading.Event()

    def payload_fn() -> dict:
        exploded.set()
        raise RuntimeError("payload exploded")

    poller = Poller(fake, poll_s=0.001, on_reply=lambda _: None, payload_fn=payload_fn)
    poller.start()
    try:
        assert exploded.wait(timeout=5)
    finally:
        poller.stop()
    assert not poller.is_alive()
    assert fake.payloads == []


def test_stopping_a_poller_twice_is_harmless():
    fake = FakeClient(reply())
    poller = Poller(fake, poll_s=0.001, on_reply=lambda _: None, payload_fn=dict)
    poller.start()
    assert fake.calls.acquire(timeout=5)
    poller.stop()
    poller.stop()
    assert not poller.is_alive()


def test_a_poller_that_was_never_started_stops_cleanly():
    poller = Poller(FakeClient(), poll_s=1.0, on_reply=lambda _: None, payload_fn=dict)
    poller.stop()
    assert not poller.is_alive()
