"""``runbound.flush()`` and ``runbound.shutdown()``: the public way to deliver what is queued before a process ends (a SIGTERM handler, a ``preStop`` hook, the end of a job).

Python runs its exit hooks on a normal exit and on SIGINT but NOT when SIGTERM takes its default action, which is what Docker and Kubernetes send: the spans still queued were lost."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import runbound
from runbound import api, spans
from test_policy_rows import FakeShared, start
from test_spans import Collector


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def one_call(name="lookup"):
    @runbound.tool(name=name, effects={"read"}, reviewed=True)
    def lookup(order_id: str):
        return "ok"

    return lookup(order_id="A-1")


def names(collector):
    return [s["name"] for s in collector.all_spans()]


def test_flush_posts_what_is_queued_and_the_emitter_keeps_running(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="fs")
    one_call("first")
    runbound.flush()
    assert "execute_tool first" in names(collector), "flush delivered the queued span"
    one_call("second")
    runbound.flush()
    assert "execute_tool second" in names(collector), "and the emitter is still running afterwards"


def test_shutdown_delivers_the_queue_stops_the_emitter_and_is_idempotent(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="fs")
    one_call("last")
    runbound.shutdown()
    assert "execute_tool last" in names(collector)
    assert spans.exporter() is None, "the span emitter is stopped"
    runbound.shutdown()  # again: nothing to do, nothing raised
    runbound.flush()


def test_before_init_both_are_no_ops():
    runbound.flush()
    runbound.shutdown()


def test_before_any_init_both_are_a_silent_no_op_and_log_nothing(caplog):
    """Before the first ``init()`` there is no state at all (``_SHARED`` is ``None``): the docstring says no-op, so no 'draining the queue failed' warning either."""
    from runbound.shared import LocalState

    api._SHARED = None
    try:
        with caplog.at_level("WARNING", logger="runbound"):
            runbound.flush()
            runbound.shutdown()
    finally:
        api._SHARED = LocalState()
    assert caplog.records == [], [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("bad", [-1, "3", None, True])
def test_a_timeout_that_is_not_a_number_of_seconds_is_refused(bad):
    with pytest.raises(ValueError, match="timeout"):
        runbound.flush(bad)
    with pytest.raises(ValueError, match="timeout"):
        runbound.shutdown(bad)


def test_a_receiver_that_never_answers_cannot_hold_the_process_past_the_bound():
    class Hang(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(15)

        def log_message(self, *args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Hang)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        start(FakeShared(), otel_endpoint=f"http://127.0.0.1:{server.server_port}", service="fs")
        one_call()
        began = time.monotonic()
        runbound.shutdown(timeout=60)  # asks for a minute; the SDK holds the process 3 s at most (plus one request's own timeout)
        assert time.monotonic() - began < 9.0
    finally:
        server.shutdown()


def test_the_documented_sigterm_recipe_delivers_the_spans_of_a_process_stopped_by_sigterm(tmp_path, collector):
    """The recipe: call ``runbound.shutdown()`` in your SIGTERM handler. Without it the same process, stopped the same way, loses the span."""
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent('''
        import json, signal, sys, time
        import runbound
        from test_policy_rows import FakeShared, start
        mode, endpoint = sys.argv[1], sys.argv[2]
        start(FakeShared(), otel_endpoint=endpoint, service="fs-" + mode)
        if mode == "with":
            signal.signal(signal.SIGTERM, lambda *_: (runbound.shutdown(), sys.exit(0)))

        @runbound.tool(name="job_" + mode, effects={"read"}, reviewed=True)
        def job():
            return "done"

        job()
        print("READY", flush=True)
        time.sleep(60)
    '''))
    here = str(Path(__file__).parent)
    for mode in ("without", "with"):
        proc = subprocess.Popen([sys.executable, str(child), mode, collector.url], stdout=subprocess.PIPE, text=True, env={**__import__("os").environ, "PYTHONPATH": here + ":" + str(Path(__file__).parent.parent)})
        assert proc.stdout.readline().strip() == "READY"
        proc.terminate()
        proc.wait(15)
    seen = names(collector)
    assert "execute_tool job_with" in seen, "the recipe delivered the span"
    assert "execute_tool job_without" not in seen, "and without it the span is lost: the reason for the recipe"
