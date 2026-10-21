"""W19: a plane that is down gives ONE short actionable line, not a chained traceback; the call still fails closed."""

from __future__ import annotations

import logging
import socket

from runbound.plane import PlaneClient


def _dead_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _client():
    return PlaneClient(_dead_url(), "rb_test_key", "svc", "w-1")


def test_a_refused_connection_logs_one_line_without_a_traceback(caplog):
    client = _client()
    with caplog.at_level(logging.WARNING, logger="runbound"):
        result = client.hello({})
    assert result is None, "plane down stays fail-closed: no reply"
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert records[0].exc_info in (None, False) or records[0].exc_info[0] is None, "no traceback attached"
    text = records[0].getMessage()
    assert "Traceback" not in text and "\n" not in text
    assert "refused" in text.lower() or "Errno" in text, text
    assert "control plane" in text and "reachable" in text


def test_the_traceback_is_still_available_at_debug(caplog):
    client = _client()
    with caplog.at_level(logging.DEBUG, logger="runbound"):
        client.hello({})
    assert any(r.exc_info and r.exc_info[0] for r in caplog.records if r.levelno == logging.WARNING)
