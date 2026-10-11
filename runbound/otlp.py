"""The stdlib OTLP/JSON span emitter: spans without ``opentelemetry`` installed.

Used when no real ``TracerProvider`` is configured and ``init(otel_endpoint=...)`` names a
collector (or Runbound's own ingest). Standard library only: ``json``, ``gzip``,
``urllib``. Everything below follows the OTLP/JSON encoding (opentelemetry-proto, Stable
for traces), whose traps are why this module exists as code and not as a comment:

* ``traceId`` (16 bytes) and ``spanId`` (8 bytes) are lower-case **hex** strings, not base64;
* ``startTimeUnixNano`` / ``endTimeUnixNano`` and ``intValue`` are decimal **strings**;
* ``kind`` and ``status.code`` are **integers**, enum names are not allowed;
* keys are lowerCamelCase; attribute values are wrapped (``{"stringValue": ...}``).

Spans are queued (bounded), posted in batches by a daemon thread with the same backoff
export.py uses, gzip-compressed, with ``Authorization: Bearer <token>``. 400, 401, 403 and
413 drop the batch (retrying cannot help); network errors, 429 and 5xx requeue it. Never
raises, never blocks the caller: a full queue drops the oldest span and counts it.
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import threading
import time
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import otel_names as names
from .export import _BACKOFF_INITIAL_S, _BACKOFF_MAX_S, DRAIN_TIMEOUT_S

_LOG = logging.getLogger("runbound")

KIND_INTERNAL = 1
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2

#: HTTP statuses after which retrying the same batch cannot help.
_DROP_STATUSES = frozenset({400, 401, 403, 404, 413, 415})


def new_trace_id(seed: str | None = None) -> str:
    """32 hex chars. With a ``seed`` (the run id) every call of one run shares a trace."""
    if seed:
        import hashlib

        return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]
    return os.urandom(16).hex()


def new_span_id() -> str:
    """16 hex chars."""
    return os.urandom(8).hex()


def _value(value) -> dict:
    """One attribute value in OTLP/JSON's wrapped form."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_value(item) for item in value]}}
    return {"stringValue": str(value)}


def attributes(mapping: dict) -> list:
    """``{"k": v}`` as OTLP's ``[{"key": k, "value": {...}}]``; ``None`` values are left out."""
    return [{"key": key, "value": _value(value)} for key, value in mapping.items() if value is not None]


def span_json(
    trace_id: str, span_id: str, name: str, start_ns: int, end_ns: int, attrs: dict, status_code: int = STATUS_UNSET
) -> dict:
    """One finished span, INTERNAL kind, no parent (a stdlib SDK has no incoming trace context)."""
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": KIND_INTERNAL,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes(attrs),
        "status": {"code": status_code},
    }


def request_body(spans: list, resource: dict, version: str) -> dict:
    """The ``ExportTraceServiceRequest`` JSON for one batch: one resource, one scope."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": attributes(resource)},
                "scopeSpans": [{"scope": {"name": names.SCOPE_NAME, "version": version}, "spans": spans}],
            }
        ]
    }


def endpoint_url(endpoint: str) -> str:
    """``http://host:4318`` -> ``http://host:4318/v1/traces`` (a full traces URL is kept)."""
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/traces") else base + "/v1/traces"


class SpanExporter:
    """Queues finished spans and posts them to an OTLP/HTTP endpoint. Never raises."""

    def __init__(
        self,
        endpoint: str,
        token: str | None,
        resource: dict,
        version: str,
        *,
        maxlen: int = 10_000,
        batch_size: int = 200,
        flush_every_s: float = 1.0,
        timeout_s: float = 5.0,
    ) -> None:
        self.url = endpoint_url(endpoint)
        self._token = token
        self._resource = dict(resource)
        self._version = version
        self._maxlen = maxlen
        self._batch_size = batch_size
        self._flush_every_s = flush_every_s
        self._timeout_s = timeout_s
        self._queue: deque = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self.dropped = 0
        self.sent = 0

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def add(self, span: dict) -> None:
        """Queue one finished span. Drops the oldest when full."""
        with self._lock:
            if len(self._queue) >= self._maxlen:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(span)
            full = len(self._queue) >= self._batch_size
        if full:
            self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, name="runbound-otlp", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.wait(self._backoff_s())
            self._wake.clear()
            self.flush(DRAIN_TIMEOUT_S)
        self.flush(DRAIN_TIMEOUT_S)

    def _backoff_s(self) -> float:
        if not self._failures:
            return self._flush_every_s
        return min(_BACKOFF_INITIAL_S * 2 ** (self._failures - 1), _BACKOFF_MAX_S)

    def stop(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Stop the thread and post what is left, within ``timeout``."""
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self.flush(timeout)

    def flush(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Post queued batches on the calling thread until the queue is empty or one is refused."""
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            with self._lock:
                batch = [self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))]
            if not batch:
                return
            verdict = self._post(batch)
            if verdict == "retry":
                with self._lock:
                    self._queue.extendleft(reversed(batch))
                self._failures += 1
                return
            self._failures = 0
            if verdict == "ok":
                self.sent += len(batch)
            else:
                self.dropped += len(batch)
            if time.monotonic() >= deadline:
                return

    def _post(self, spans: list) -> str:
        """``"ok"``, ``"drop"`` (retrying cannot help) or ``"retry"``. Never raises."""
        try:
            body = gzip.compress(json.dumps(request_body(spans, self._resource, self._version)).encode("utf-8"))
            headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            with urlopen(Request(self.url, data=body, headers=headers, method="POST"), timeout=self._timeout_s):
                return "ok"
        except HTTPError as error:
            if error.code in _DROP_STATUSES:
                _LOG.warning("runbound: the OTLP endpoint refused spans (HTTP %s); dropping the batch", error.code)
                return "drop"
            return "retry"
        except (URLError, TimeoutError, OSError):
            return "retry"
        except Exception:
            _LOG.warning("runbound: spans could not be encoded; dropping the batch", exc_info=True)
            return "drop"
