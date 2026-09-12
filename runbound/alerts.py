"""Verifying a runbound webhook, and the bookkeeping that lets outbound
threads drain.

Delivery — Slack, PagerDuty, the generic signed webhook — is not sent from
here any more (Wave 31). The owner's call: "we send alerts from control
plane not sdk, sdk is there to detect. all the paid business logic stays
in control plane." An earlier version of this module gated its senders
behind a token, and a review proved that could never work — a truthiness
check running inside the customer's own process cannot enforce a paid
feature (``control_plane_url=""`` walked straight past it, and any
non-empty token turned delivery on for the life of the process, revoked or
not). So the senders left rather than grew a second gate: the control
plane's ``alerting/adapters/webhook.py`` is byte-compatible with what used
to be sent from here (same envelope, same headers, same signing string),
plus Opsgenie, routing rules, fleet-wide dedup and a plan-aware 403 that
this module could never issue on its own.

What stays is the two things a receiver, and the rest of the SDK, still
need from here:

* :func:`verify_webhook_signature` and its signing helpers, so a customer
  running their own receiver for the plane's webhook adapter can check a
  delivery without writing HMAC code themselves. This is still a public
  import path — ``runbound.alerts.verify_webhook_signature`` — and the
  plane's adapter docstring points at it.
* The outbound-thread bookkeeping (``_track``, ``_ALERT_THREADS``,
  ``_drain_alerts``, and the ``atexit`` hook that runs it) that
  :mod:`runbound.export` uses so its telemetry-flushing thread is waited
  for at interpreter exit — a trip that stops the process must not take
  the record of it down too, and that was true before there was anything
  here to send and is true now that there is nothing here to send at all.
"""

import atexit
import hashlib
import hmac
import threading
import time
from collections.abc import Callable

#: Schema version of the webhook body the plane's adapter sends. Bumped only
#: for a breaking change, so a receiver can branch on it. Kept here because
#: it is part of the wire contract :func:`verify_webhook_signature` checks
#: against, not because anything in this module builds a body any more.
WEBHOOK_BODY_VERSION = 1

#: How far a webhook's timestamp may be from the receiver's clock, in seconds,
#: before :func:`verify_webhook_signature` calls it a replay.
WEBHOOK_TOLERANCE_SECONDS = 300

#: Total seconds the exit hook will wait for in-flight outbound threads, all
#: together.
DRAIN_TIMEOUT_SECONDS = 3.0

#: POST threads that may still be running, newest last. Pruned of finished
#: threads whenever one is added, and read by the exit hook. Nothing in this
#: module starts one any more — :mod:`runbound.export` builds its own
#: flusher thread and hands it to :func:`_track` — but the bookkeeping is
#: kept here because it is where the ``atexit`` hook already lived.
_ALERT_THREADS: list[threading.Thread] = []
_THREADS_LOCK = threading.Lock()


def _track(thread: threading.Thread) -> None:
    """Remember a live outbound thread so the exit hook can wait for it."""
    with _THREADS_LOCK:
        _ALERT_THREADS[:] = [live for live in _ALERT_THREADS if live.is_alive()]
        _ALERT_THREADS.append(thread)


def _drain_alerts(timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
    """Wait up to ``timeout`` seconds, in total, for tracked threads to land.

    Registered with :mod:`atexit`, where it earns its keep: a trip that stops
    the process would otherwise take the record of the incident down with
    it, since a daemon thread is killed outright when the interpreter exits.
    The budget is shared across every outstanding thread — a hung POST delays
    exit by seconds, never indefinitely — and the hook never raises, whatever
    state the interpreter is in by the time it runs.

    The budget is counted in real seconds (``time.monotonic`` directly, not
    a swappable clock): how long a process may take to die is not something
    a test clock should be able to stretch.
    """
    try:
        deadline = time.monotonic() + max(timeout, 0.0)
        with _THREADS_LOCK:
            threads = list(_ALERT_THREADS)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(remaining)
    except Exception:
        # Deliberately silent: this runs during interpreter shutdown, where
        # logging may already be torn down and an exception from an exit hook
        # would be printed at the user's process death for no benefit.
        pass


atexit.register(_drain_alerts)


def _signing_string(timestamp: str, body: bytes) -> bytes:
    """The bytes that are HMAC'd: the timestamp, a dot, then the raw body."""
    return timestamp.encode("utf-8") + b"." + body


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), _signing_string(timestamp, body), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_webhook_signature(
    secret: str,
    timestamp: str,
    body_bytes: bytes,
    signature: str,
    tolerance_s: float = WEBHOOK_TOLERANCE_SECONDS,
    now: Callable[[], float] = time.time,
) -> bool:
    """Check a delivery from runbound's webhook adapter. Receiver-side,
    three lines::

        body = request.get_data()                       # raw bytes, unparsed
        if not verify_webhook_signature(SECRET, request.headers["X-Runbound-Timestamp"],
                                        body, request.headers["X-Runbound-Signature"]):
            return "bad signature", 400

    ``signature`` is ``"sha256=" + HMAC-SHA256(secret, b"<timestamp>." + body)``
    in hex, and is compared in constant time. A timestamp more than
    ``tolerance_s`` seconds from ``now()`` in either direction is rejected
    before the compare, so a captured delivery cannot be replayed later.

    Returns ``False`` — never raises — for anything malformed: a timestamp
    that is not a number, a missing signature, a body that has been touched.
    Verify the *raw* body bytes; re-serializing the parsed JSON will not
    reproduce them.
    """
    try:
        age = now() - float(timestamp)
        if abs(age) > tolerance_s:
            return False
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode("utf-8")
        expected = _signature(secret, str(timestamp), body_bytes)
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError):
        return False
