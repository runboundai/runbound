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

**Batch identity (PS-DEDUPE-1).** Every post carries ``X-Runbound-Batch: <boot_id>.<exporter_id>.<seq>(.p<N>)*`` so ingest can deduplicate a retried insert (a token is never
shared by two different bodies). The token is minted ONCE per batch. A batch whose post fails retryably becomes an immutable in-flight unit (the token plus the exact serialized,
compressed body) held OUTSIDE the queue and resent first, byte for byte, until it is accepted or dropped; new spans queue behind it and the drop-oldest rule never touches it. A 413
HALVES the unit (the events lane's rule): each half is a part with its own token, the parent's with a further ``.p0`` / ``.p1``, so a part refused again halves again, down to
``MAX_SPLIT_DEPTH`` levels; a part refused at the limit, or a single span, is dropped, counted once. The parent token is retired and a part is never re-batched with new spans.
One flusher at a time touches the in-flight unit.
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import delivery, otel_names as names
from .export import _BACKOFF_INITIAL_S, _BACKOFF_MAX_S, DRAIN_TIMEOUT_S

_LOG = logging.getLogger("runbound")

KIND_INTERNAL = 1
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2

#: HTTP statuses after which retrying the same batch cannot help.
_DROP_STATUSES = frozenset({400, 401, 403, 404, 413, 415})
#: Too large: the unit is halved instead of dropped.
_SPLIT_STATUS = 413
#: How many times a batch may be halved (200 -> 100 -> 50 -> 25 -> 12): a part refused at this depth is dropped.
MAX_SPLIT_DEPTH = 4
BATCH_HEADER = "X-Runbound-Batch"

#: Drawn once per process: a restart is a new boot, so a token can never be reused across restarts.
_BOOT_ID = uuid.uuid4().hex


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
    trace_id: str, span_id: str, name: str, start_ns: int, end_ns: int, attrs: dict, status_code: int = STATUS_UNSET,
    parent_span_id: "str | None" = None,
) -> dict:
    """One finished span, INTERNAL kind; no parent unless ``parent_span_id`` is given (a stdlib SDK has no incoming trace context)."""
    out = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": KIND_INTERNAL,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes(attrs),
        "status": {"code": status_code},
    }
    if parent_span_id:
        out["parentSpanId"] = parent_span_id
    return out


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


def rejected_spans_of(raw: bytes) -> int:
    """``partialSuccess.rejectedSpans`` of an OTLP export response body (``0`` when the body is empty, not JSON, or names none).

    A 2xx with a positive count means the receiver kept the request and refused that many of its spans: they were not recorded.
    """
    try:
        body = json.loads(raw) if raw and raw.strip() else {}
        count = (body.get("partialSuccess") or {}).get("rejectedSpans")
        count = int(count) if isinstance(count, (int, str)) and not isinstance(count, bool) else 0
        return max(count, 0)
    except (ValueError, TypeError, AttributeError):
        return 0


class _Unit:
    """One batch as it goes on the wire: its token, the spans it carries and the exact bytes. Immutable once built: a retry resends these bytes under this token."""

    __slots__ = ("token", "spans", "body", "depth")

    def __init__(self, token: "str | None", spans: list, body: bytes, depth: int = 0) -> None:
        self.token, self.spans, self.body, self.depth = token, spans, body, depth


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
        batch_header: bool = True,
        boot_id: "str | None" = None,
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
        #: Serializes every flusher: the exporter thread and ``stop()`` never both build or resend the in-flight unit.
        self._flush_lock = threading.Lock()
        self._inflight: deque = deque()  # units not yet accepted, in order, resent before anything new
        self._batch_header = batch_header
        self._boot_id = boot_id or _BOOT_ID
        self._exporter_id = os.urandom(4).hex()  # per instance: two inits (or a re-init) in one process never share a token
        self._seq = 0
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self.dropped = 0
        self.sent = 0
        self.rejected = 0  # accepted requests whose body reported rejected spans (partialSuccess)
        self._last_rejected = 0
        self._drop_reason = "dropped"

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue) + sum(len(unit.spans) for unit in self._inflight)

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
        """Post units on the calling thread until nothing is left or one is refused. Only one flusher runs at a time; another caller waits for it up to ``timeout``."""
        deadline = time.monotonic() + max(timeout, 0.0)
        if not self._flush_lock.acquire(timeout=max(timeout, 0.0)):
            return
        try:
            while True:
                unit = self._next_unit()
                if unit is None:
                    return
                verdict = self._post(unit)
                if verdict == "retry":
                    self._failures += 1
                    return  # the unit stays at the head of the in-flight line, untouched
                self._failures = 0
                if verdict == "split":
                    self._split(unit)
                else:
                    with self._lock:
                        self._inflight.popleft()
                    count = len(unit.spans)
                    if verdict == "ok":
                        lost = min(self._last_rejected, count)  # a 2xx whose partialSuccess names rejected spans: those were not recorded
                        self.sent += count - lost
                        self.rejected += lost
                        delivery.rejected_spans(lost)
                    else:
                        self.dropped += count
                        delivery.dropped("spans", self._drop_reason, count)
                if time.monotonic() >= deadline:
                    return
        finally:
            self._flush_lock.release()

    def _next_unit(self) -> "_Unit | None":
        """The unit to post next: the in-flight head if there is one, else a new batch cut from the queue and made immutable. Caller holds the flush lock."""
        while True:
            with self._lock:
                if self._inflight:
                    return self._inflight[0]
                batch = [self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))]
            if not batch:
                return None
            token = None
            if self._batch_header:
                token = f"{self._boot_id}.{self._exporter_id}.{self._seq}"
                self._seq += 1
            unit = self._unit(token, batch)
            if unit is None:
                self.dropped += len(batch)  # could not be encoded: nothing to resend
                delivery.dropped("spans", "unencodable", len(batch))
                continue
            with self._lock:
                self._inflight.append(unit)
            return unit

    def _unit(self, token: "str | None", spans: list, depth: int = 0) -> "_Unit | None":
        try:
            return _Unit(token, spans, gzip.compress(json.dumps(request_body(spans, self._resource, self._version)).encode("utf-8")), depth)
        except Exception:
            _LOG.warning("runbound: spans could not be encoded; dropping the batch", exc_info=True)
            return None

    def _split(self, unit: "_Unit") -> None:
        """A 413: replace the head unit with its two halves (each its own token: the parent's plus ``.p0`` / ``.p1``, its own bytes, built once). A single span, or a unit already
        halved ``MAX_SPLIT_DEPTH`` times, cannot be cut further: it is dropped, counted once, with one warning that says so. Caller holds the flush lock."""
        spans = unit.spans
        with self._lock:
            self._inflight.popleft()
        if len(spans) < 2 or unit.depth >= MAX_SPLIT_DEPTH:
            why = "a single span" if len(spans) < 2 else f"already halved {MAX_SPLIT_DEPTH} times"
            _LOG.warning("runbound: the OTLP endpoint refused %d span(s) as too large (HTTP 413) and they cannot be split further (%s); dropping them", len(spans), why)
            self.dropped += len(spans)
            self._drop_reason = "http_413"
            delivery.dropped("spans", self._drop_reason, len(spans))
            return
        _LOG.warning("runbound: the OTLP endpoint refused a batch of %d spans as too large (HTTP 413); sending it in halves", len(spans))
        middle = (len(spans) + 1) // 2
        made = []
        for index, chunk in enumerate((spans[:middle], spans[middle:])):
            half = self._unit(None if unit.token is None else f"{unit.token}.p{index}", chunk, unit.depth + 1)
            if half is None:
                self.dropped += len(chunk)
                delivery.dropped("spans", "unencodable", len(chunk))
            else:
                made.append(half)
        with self._lock:
            self._inflight.extendleft(reversed(made))

    def _post(self, unit: "_Unit") -> str:
        """``"ok"``, ``"drop"`` (retrying cannot help), ``"split"`` (too large) or ``"retry"``. Never raises. Posts the unit's own bytes."""
        self._last_rejected = 0
        try:
            headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
            if unit.token:
                headers[BATCH_HEADER] = unit.token
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            with urlopen(Request(self.url, data=unit.body, headers=headers, method="POST"), timeout=self._timeout_s) as response:
                self._last_rejected = rejected_spans_of(response.read())
                return "ok"
        except HTTPError as error:
            if error.code == _SPLIT_STATUS:
                return "split"
            if error.code in _DROP_STATUSES:
                _LOG.warning("runbound: the OTLP endpoint refused spans (HTTP %s); dropping the batch", error.code)
                self._drop_reason = f"http_{error.code}"
                return "drop"
            return "retry"
        except (URLError, TimeoutError, OSError):
            return "retry"
        except Exception:
            _LOG.warning("runbound: spans could not be sent; dropping the batch", exc_info=True)
            self._drop_reason = "unencodable"
            return "drop"
