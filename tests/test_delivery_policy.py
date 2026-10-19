"""AAI-1 (PS-AAI-03f): what each telemetry lane does with an answer, and what it counts.

* events (``/v1/events``): 400/404/409/422 drop the batch and count it with the status as the reason (retrying the same bytes cannot help); 413 sends the batch in halves and
  drops a single record that is still too big (``too_large``); the plane's non-retryable ``ledger_unavailable`` drops with that reason; 5xx and a down plane still requeue;
  a rejected key stops the lane (visible in the counters) and is re-probed every hour;
* spans (``/v1/traces``): ``partialSuccess.rejectedSpans`` is counted as rejected, not sent;
* approval reports: a 400 for an outcome an older plane does not know is ``plane_too_old``.

A real client against a local stdlib server; nothing here changes what a worker enforces.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from runbound import approvals, delivery
from runbound.export import LANE_REPROBE_S, Exporter
from runbound.otlp import SpanExporter
from runbound.plane import PlaneClient


class Server:
    """Answers each POST from ``script`` (a callable taking the decoded body and returning ``(status, json body)``) and keeps every body it saw."""

    def __init__(self, script) -> None:
        self.script, self.seen = script, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    import gzip

                    raw = gzip.decompress(raw)
                body = json.loads(raw)
                outer.seen.append(body)
                status, answer = outer.script(body)
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return None

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.http.shutdown()


@pytest.fixture(autouse=True)
def _clean():
    delivery._reset_for_tests()
    yield
    delivery._reset_for_tests()


def exporter_for(server, **options):
    client = PlaneClient(server.url, "sk-test", "svc", "w-1")
    return Exporter(client, maxlen=1000, flush_every_s=60.0, batch_size=options.pop("batch_size", 200), **options), client


def queue_events(exporter, n):
    for i in range(n):
        exporter._enqueue(exporter._events, {"kind": "tool_call", "i": i})


def lane(snapshot, name="events"):
    return snapshot["dropped"].get(name, {})


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_an_answer_that_retrying_cannot_change_drops_the_batch_and_counts_it_by_status(status):
    server = Server(lambda body: (status, {"error": "no"}))
    try:
        exporter, _ = exporter_for(server)
        queue_events(exporter, 3)
        exporter.flush(2.0)
        assert len(server.seen) == 1, "sent once, not retried"
        assert exporter.pending == 0 and exporter.dropped == 3 and exporter.resends == 0
        assert lane(delivery.snapshot()) == {f"http_{status}": 3}
    finally:
        server.close()


@pytest.mark.parametrize("status", [500, 501, 502, 503])
def test_a_plane_that_failed_is_retried_not_dropped(status):
    server = Server(lambda body: (status, {"error": "down"}))
    try:
        exporter, _ = exporter_for(server)
        queue_events(exporter, 3)
        exporter.flush(2.0)
        assert exporter.pending == 3 and exporter.dropped == 0 and lane(delivery.snapshot()) == {}
    finally:
        server.close()


def test_a_413_sends_the_batch_in_halves_until_it_fits_and_drops_nothing():
    server = Server(lambda body: (413, {}) if len(body["events"]) > 2 else (202, {"accepted": len(body["events"])}))
    try:
        exporter, _ = exporter_for(server)
        queue_events(exporter, 7)
        for _ in range(10):
            exporter.flush(2.0)
        sizes = [len(body["events"]) for body in server.seen]
        assert exporter.pending == 0 and exporter.dropped == 0 and lane(delivery.snapshot()) == {}
        assert sizes[0] == 7 and max(sizes[1:]) <= 3, sizes
        delivered = sum(len(b["events"]) for b in server.seen if len(b["events"]) <= 2)
        assert delivered == 7, "every record landed exactly once"
    finally:
        server.close()


def test_a_single_record_the_plane_says_is_too_large_is_dropped_and_counted():
    server = Server(lambda body: (413, {}))
    try:
        exporter, _ = exporter_for(server)
        queue_events(exporter, 1)
        exporter.flush(2.0)
        assert exporter.pending == 0 and lane(delivery.snapshot()) == {"too_large": 1}
    finally:
        server.close()


@pytest.mark.parametrize("status", [202, 400, 409, 501])
def test_the_planes_ledger_unavailable_code_is_dropped_with_that_reason_never_requeued(status):
    server = Server(lambda body: (status, {"error": "ledger_unavailable"}))
    try:
        exporter, _ = exporter_for(server)
        queue_events(exporter, 2)
        exporter.flush(2.0)
        assert len(server.seen) == 1 and exporter.pending == 0
        assert lane(delivery.snapshot()) == {"ledger_unavailable": 2}
    finally:
        server.close()


def test_a_rejected_key_stops_the_lane_visibly_and_it_is_probed_again_after_an_hour():
    clock = [1000.0]
    server = Server(lambda body: (401, {"error": "bad key"}))
    try:
        client = PlaneClient(server.url, "sk-test", "svc", "w-1")
        exporter = Exporter(client, maxlen=1000, flush_every_s=60.0, now=lambda: clock[0])
        queue_events(exporter, 2)
        exporter.flush(2.0)
        exporter.flush(2.0)
        snap = delivery.snapshot()
        assert snap["stopped_lanes"]["events"]["reason"] == "key_rejected" and client.key_state == "invalid"
        assert lane(snap) == {"key_rejected": 2}
        calls = len(server.seen)

        clock[0] += LANE_REPROBE_S - 1
        exporter.flush(2.0)
        assert client.key_state == "invalid" and len(server.seen) == calls, "not before the hour"

        clock[0] += 2
        exporter.flush(2.0)
        assert client.key_state == "unknown", "the rejection is forgotten: the next batch is the probe"
        queue_events(exporter, 1)
        exporter.flush(2.0)
        assert len(server.seen) == calls + 1 and client.key_state == "invalid", "still a bad key: the probe stopped the lane again"
    finally:
        server.close()


def test_a_lane_that_delivers_again_is_no_longer_listed_as_stopped():
    delivery.lane_stopped("events", "key_rejected")
    server = Server(lambda body: (202, {}))
    try:
        exporter, _ = exporter_for(server)
        queue_events(exporter, 1)
        exporter.flush(2.0)
        assert delivery.snapshot()["stopped_lanes"] == {}
    finally:
        server.close()


def span_exporter(server):
    return SpanExporter(server.url, None, {}, "0", batch_size=50)


def test_spans_the_receiver_rejected_are_counted_as_rejected_not_sent():
    server = Server(lambda body: (200, {"partialSuccess": {"rejectedSpans": 2, "errorMessage": "bad ids"}}))
    try:
        exporter = span_exporter(server)
        for i in range(5):
            exporter.add({"n": i})
        exporter.flush(2.0)
        assert exporter.sent == 3 and exporter.rejected == 2 and exporter.dropped == 0
        assert delivery.snapshot()["rejected_spans"] == 2
    finally:
        server.close()


def test_an_empty_or_clean_span_response_rejects_nothing():
    server = Server(lambda body: (200, {}))
    try:
        exporter = span_exporter(server)
        for i in range(4):
            exporter.add({"n": i})
        exporter.flush(2.0)
        assert exporter.sent == 4 and exporter.rejected == 0 and delivery.snapshot()["rejected_spans"] == 0
    finally:
        server.close()


def test_a_span_batch_the_receiver_refuses_for_good_is_counted_with_its_status():
    server = Server(lambda body: (400, {}))
    try:
        exporter = span_exporter(server)
        for i in range(3):
            exporter.add({"n": i})
        exporter.flush(2.0)
        assert exporter.dropped == 3 and lane(delivery.snapshot(), "spans") == {"http_400": 3}
    finally:
        server.close()


class Reports:
    def __init__(self, status):
        self.status, self.calls = status, []

    def approvals(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        return self.status, {}


def test_a_report_the_plane_refuses_for_an_outcome_it_does_not_know_is_plane_too_old():
    assert approvals._post_report(Reports(400), {"approval_id": "a1", "outcome": "cancelled", "worker_id": "w"}) is True
    assert approvals._post_report(Reports(400), {"approval_id": "a2", "outcome": "failed", "worker_id": "w"}) is True
    assert approvals._post_report(Reports(200), {"approval_id": "a3", "outcome": "executed", "worker_id": "w"}) is True
    assert lane(delivery.snapshot(), "approval_reports") == {"plane_too_old": 1, "http_400": 1}


def test_a_failing_telemetry_lane_never_reaches_a_decision():
    """The exporter only observes: a client that raises, and a plane that refuses everything, leave a flush returning normally."""

    class Raises:
        service, worker_id, key_state = "svc", "w", "valid"

        def events(self, batch):
            raise RuntimeError("client bug")

    exporter = Exporter(Raises(), maxlen=10, flush_every_s=60.0)
    queue_events(exporter, 2)
    exporter.flush(1.0)  # must not raise
    assert exporter.pending == 0
