"""Tests for what happens around the edges of a trip: throttling under
asyncio, a stream closing on someone else's exception, verifying a webhook
signature, outbound threads draining at process exit, and the bounded token
window.

Nothing here sleeps for real and nothing touches the network: the engine's
``time`` module is replaced with a fake, and every clock ``verify_webhook_
signature`` reads is injected.

This file used to also cover the alerters themselves — ``SlackAlerter``,
``PagerDutyAlerter``, the generic ``WebhookAlerter`` — and the global alert
rate limit that gated them. Wave 31 moved delivery to the control plane
entirely (``tests/test_alerts.py`` and ``tests/test_webhook_alerter.py`` are
gone, and so is the rate limiter), so what is left here is exactly the two
things :mod:`runbound.alerts` still does: verify a signature, and drain a
tracked outbound thread at exit. Both used to be exercised through an
alerter's own ``send()``; here they are exercised directly, since there is
no alerter left to call.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from runbound import alerts
from runbound import engine as engine_module
from runbound.alerts import verify_webhook_signature
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import PRUNE_AFTER, TOKEN_WINDOW_SECONDS, SessionState
from runbound.wrappers import _AsyncGuardedStream, _GuardedStream

SECRET = "s3cret"


class FakeClock:
    """Stands in for the ``time`` module inside the engine."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)

    def monotonic(self) -> float:
        # A fixed instant is enough here: these tests set and immediately
        # read back a pending delay, never exercising its staleness window.
        return 0.0


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(
        kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash
    )


def anomaly(detector: str = "loop", severity: str = "critical") -> Anomaly:
    return Anomaly(detector=detector, severity=severity, message="looping", details={})


def warnings_matching(caplog, *fragments: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and all(fragment in record.getMessage() for fragment in fragments)
    ]


# --- throttle under a running event loop ------------------------------------


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


def test_throttle_stashes_the_delay_instead_of_sleeping_under_a_running_event_loop(
    clock, caplog
):
    """time.sleep on the event loop would stall every other user on the worker;

    the engine stashes the delay in a contextvar instead (Wave 24, T59c) for an
    async caller to retrieve with ``take_pending_delay()`` and await, and logs
    nothing — the old "disabled under asyncio" warning described a limitation
    that no longer exists.
    """
    engine = Engine(GuardrailConfig(loop_threshold=2, on_loop="throttle"))
    state = SessionState("s1")
    seen: list[float] = []

    async def drive():
        for step in (1, 2, 3):
            engine.process(state, tool_event(step))
            seen.append(engine_module.take_pending_delay())

    with caplog.at_level(logging.WARNING, logger="runbound"):
        asyncio.run(drive())

    assert clock.delays == []
    assert not warnings_matching(caplog, "event loop")
    assert any(delay > 0.0 for delay in seen)


def test_take_pending_delay_returns_it_once_then_zero_under_a_running_event_loop(clock):
    engine = Engine(GuardrailConfig(loop_threshold=2, on_loop="throttle"))
    state = SessionState("s1")

    async def drive():
        engine.process(state, tool_event(1))
        engine.process(state, tool_event(2))  # the repeat that trips the loop
        return engine_module.take_pending_delay(), engine_module.take_pending_delay()

    first, second = asyncio.run(drive())

    assert first > 0.0
    assert second == 0.0


def test_throttle_still_sleeps_off_the_event_loop(clock):
    engine = Engine(GuardrailConfig(loop_threshold=2, on_loop="throttle"))
    state = SessionState("s1")

    for step in (1, 2, 3):
        engine.process(state, tool_event(step))

    assert clock.delays  # the sync path is untouched


def test_a_throttled_loop_under_asyncio_never_raises(clock):
    config = GuardrailConfig(loop_threshold=2, on_loop="throttle", on_anomaly="raise")
    engine = Engine(config)
    state = SessionState("s1")

    async def drive():
        for step in (1, 2, 3):
            engine.process(state, tool_event(step))

    asyncio.run(drive())  # throttling stops nothing, on the loop or off it

    assert state.tripped_by is None


# --- a stream that closes while the host is already failing -----------------


class FakeStream:
    """Shaped like openai.Stream: iterator, context manager, closeable."""

    def __init__(self, chunks=()):
        self._chunks = iter(list(chunks))
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class FakeAsyncStream:
    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self._index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        self._index += 1
        return self._chunks[self._index - 1]

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.close()
        return False


def tripping_report(*args, **kwargs):
    raise GuardrailTripped(anomaly("budget"))


def no_usage(chunk, usage):
    return None


def guarded(stream=None):
    return _GuardedStream(stream or FakeStream(), no_usage, tripping_report, "gpt-4o", None)


def async_guarded(stream=None):
    return _AsyncGuardedStream(
        stream or FakeAsyncStream(), no_usage, tripping_report, "gpt-4o", None
    )


def test_a_trip_at_stream_exit_never_replaces_the_hosts_own_exception(caplog):
    stream = guarded()

    with caplog.at_level(logging.WARNING, logger="runbound"):
        with pytest.raises(ValueError, match="the real failure"):
            with stream:
                raise ValueError("the real failure")

    assert warnings_matching(caplog, "stream", "original exception")
    assert stream._stream.closed  # the provider's stream was still closed


def test_a_trip_at_a_clean_stream_exit_still_reaches_the_caller():
    with pytest.raises(GuardrailTripped):
        with guarded():
            pass


def test_close_with_nothing_in_flight_still_raises():
    with pytest.raises(GuardrailTripped):
        guarded().close()


def test_exhaustion_still_raises():
    with pytest.raises(GuardrailTripped):
        list(guarded(FakeStream(["a", "b"])))


def test_async_trip_at_stream_exit_never_replaces_the_hosts_own_exception(caplog):
    stream = async_guarded()

    async def drive():
        with pytest.raises(ValueError, match="the real failure"):
            async with stream:
                raise ValueError("the real failure")

    with caplog.at_level(logging.WARNING, logger="runbound"):
        asyncio.run(drive())

    assert warnings_matching(caplog, "stream", "original exception")
    assert stream._stream.closed


def test_async_trip_at_a_clean_stream_exit_still_reaches_the_caller():
    async def drive():
        with pytest.raises(GuardrailTripped):
            async with async_guarded():
                pass

    asyncio.run(drive())


def test_aclose_with_nothing_in_flight_still_raises():
    async def drive():
        with pytest.raises(GuardrailTripped):
            await async_guarded().aclose()

    asyncio.run(drive())


# --- verifying a webhook signature -------------------------------------------
#
# Moved here from tests/test_webhook_alerter.py (Wave 31): the alerter that
# used to build these bodies is gone — the control plane's webhook adapter
# builds byte-compatible ones now — but verify_webhook_signature is still a
# public part of this SDK, for a customer running their own receiver. These
# tests build a body and a signature by hand instead of through an alerter.


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    """The documented signing string, spelled out independently of the SDK."""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def test_a_correctly_signed_delivery_verifies():
    body = b'{"version": 1, "anomaly": {"detector": "loop"}}'
    timestamp = "1000000"
    signature = _sign(SECRET, timestamp, body)

    assert (
        verify_webhook_signature(SECRET, timestamp, body, signature, now=lambda: 1_000_000)
        is True
    )


def test_verify_rejects_a_wrong_secret_and_a_tampered_body():
    body = b'{"version": 1}'
    timestamp = "1000000"
    signature = _sign(SECRET, timestamp, body)

    assert verify_webhook_signature("other", timestamp, body, signature) is False
    assert verify_webhook_signature(SECRET, timestamp, body + b" ", signature) is False
    later = str(int(timestamp) + 1)
    assert verify_webhook_signature(SECRET, later, body, signature) is False
    assert verify_webhook_signature(SECRET, timestamp, body, "sha256=bad") is False
    assert verify_webhook_signature(SECRET, timestamp, body, "") is False


def test_verify_rejects_a_stale_timestamp():
    body = b'{"version": 1}'
    signed_at = 1_000_000
    signature = _sign(SECRET, str(signed_at), body)

    fresh = lambda: signed_at + 299  # noqa: E731
    stale = lambda: signed_at + 301  # noqa: E731
    assert verify_webhook_signature(SECRET, str(signed_at), body, signature, now=fresh) is True
    assert verify_webhook_signature(SECRET, str(signed_at), body, signature, now=stale) is False
    assert (
        verify_webhook_signature(
            SECRET, str(signed_at), body, signature, tolerance_s=600, now=stale
        )
        is True
    )


def test_verify_rejects_a_timestamp_from_the_future_and_garbage():
    body = b"{}"
    signed_at = 1_000_000
    signature = _sign(SECRET, str(signed_at), body)

    early = lambda: signed_at - 400  # noqa: E731
    assert verify_webhook_signature(SECRET, str(signed_at), body, signature, now=early) is False
    assert verify_webhook_signature(SECRET, "not-a-number", body, signature) is False
    assert verify_webhook_signature(SECRET, None, body, signature) is False


def test_verify_accepts_a_str_body_the_same_as_its_utf8_bytes():
    body = '{"version": 1, "who": "café"}'
    timestamp = "1000000"
    signature = _sign(SECRET, timestamp, body.encode("utf-8"))

    assert (
        verify_webhook_signature(SECRET, timestamp, body, signature, now=lambda: 1_000_000)
        is True
    )


def test_signature_matches_the_documented_signing_string():
    """runbound.alerts._signature (used by verify_webhook_signature itself)
    must produce exactly the format the docstring promises a receiver."""
    body = b'{"version": 1}'
    timestamp = "1000000"

    assert alerts._signature(SECRET, timestamp, body) == _sign(SECRET, timestamp, body)


# --- outbound threads are drained at exit -----------------------------------
#
# Moved here from tests/test_alerts.py (Wave 31): there is no alerter left to
# start a thread, so these track a plain thread by hand with the same
# ``alerts._track`` / ``alerts._drain_alerts`` bookkeeping
# :mod:`runbound.export`'s flusher thread actually uses.


def test_drain_joins_outstanding_tracked_threads():
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(5), daemon=True)
    thread.start()
    alerts._track(thread)

    alerts._drain_alerts(0.2)  # the thread is still blocked; the budget runs out
    assert thread.is_alive()

    release.set()
    alerts._drain_alerts(5.0)
    assert not thread.is_alive()


def test_drain_returns_quietly_when_there_is_nothing_to_join():
    assert alerts._drain_alerts(0.1) is None


def test_drain_never_raises_even_when_a_tracked_thread_is_broken(monkeypatch):
    class BrokenThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            raise RuntimeError("cannot join")

    monkeypatch.setattr(alerts, "_ALERT_THREADS", [BrokenThread()])

    assert alerts._drain_alerts(0.1) is None


DRAIN_AT_EXIT_CHILD = """
import sys, threading, time
from runbound import alerts

marker = sys.argv[1]

def slow_write():
    time.sleep(0.4)                      # still in flight when the process ends
    with open(marker, "w") as handle:
        handle.write("delivered")

thread = threading.Thread(target=slow_write, daemon=True)
thread.start()
alerts._track(thread)
"""


def test_a_tracked_thread_survives_the_processs_exit(tmp_path):
    """The record of the trip that ends the process must not die with it."""
    marker = tmp_path / "delivered"
    repo_root = str(pathlib.Path(alerts.__file__).resolve().parent.parent)
    environment = {**os.environ, "PYTHONPATH": repo_root}

    result = subprocess.run(
        [sys.executable, "-c", DRAIN_AT_EXIT_CHILD, str(marker)],
        env=environment,
        timeout=60,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert marker.exists(), "the daemon thread was killed before the write landed"


def test_finished_threads_do_not_pile_up():
    for _ in range(10):
        thread = threading.Thread(target=lambda: None, daemon=True)
        thread.start()
        alerts._track(thread)
        thread.join(timeout=5)

    assert len(alerts._ALERT_THREADS) <= 2  # the finished ones were dropped


# --- the token window stays bounded -----------------------------------------


def llm_event(step: int, ts: float, tokens: int = 10) -> Event:
    return Event(kind="llm_call", ts=ts, step=step, tokens_in=tokens)


def test_token_timestamps_stay_bounded_with_velocity_disabled():
    """The default configuration runs no velocity detector to prune for us."""
    state = SessionState("s1")

    for step in range(1, 201):
        state.record(llm_event(step, ts=float(step)))

    assert len(state.token_timestamps) <= PRUNE_AFTER + 1  # far below the 200 recorded
    # A minute's worth, plus whatever arrived since the last prune.
    assert 200.0 - state.token_timestamps[0][0] < TOKEN_WINDOW_SECONDS * 1.5


def test_a_long_run_never_grows_the_window():
    """Ten hours of one call a second used to retain 36,000 entries."""
    state = SessionState("s1")
    sizes = []

    for step in range(1, 1001):
        state.record(llm_event(step, ts=float(step)))
        sizes.append(len(state.token_timestamps))

    assert max(sizes) == max(sizes[-100:])  # the size plateaus instead of growing


def test_recent_events_are_all_kept():
    state = SessionState("s1")

    for step in range(1, 101):
        state.record(llm_event(step, ts=float(step) / 10.0))  # 10s of traffic

    assert len(state.token_timestamps) == 100


def test_pruning_tolerates_out_of_order_timestamps():
    state = SessionState("s1")

    for step in range(1, 101):
        state.record(llm_event(step, ts=100.0))
    state.record(llm_event(101, ts=0.0))  # a clock that went backwards

    assert len(state.token_timestamps) == 101
