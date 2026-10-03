"""OpenTelemetry export: ``pip install "runbound[otel]"``.

Sends what this process already records locally (:func:`runbound.events`) to
your OpenTelemetry pipeline, as log records and five metrics::

    import runbound
    from runbound import otel

    runbound.init(budget_usd=5.00)
    otel.enable()   # uses the global OpenTelemetry providers

Log records, one per anomaly, refusal, posture change and runtime change
(``event_name`` ``runbound.anomaly``, ``runbound.refusal``,
``runbound.posture`` and ``runbound.runtime_change``). A refusal or anomaly
that carries a :class:`~runbound.events.Decision` puts its fields on the
record as ``runbound.decision.*`` attributes, the numbers behind it as
``runbound.decision.evaluation.<name>``. Metrics: ``runbound.refusals``,
``runbound.anomalies`` and ``runbound.posture_changes``, and, one tick per guarded model call,
``runbound.guarded_calls`` (``runbound.model``, ``runbound.provider``, ``runbound.outcome`` of ``ok`` or ``error``) and
``runbound.estimated_usd`` (the SDK's own price estimate for the call, in USD, with ``runbound.model``, ``runbound.provider``
and ``runbound.priced`` of ``estimated`` when the price came from a fallback). All counters.

Content independence holds, as everywhere else in the SDK: a record carries
detector names, scopes, numbers and the sentence a refusal already carries,
never a prompt, a reply or a call argument, and a session key leaves only as
``runbound.key_hash`` (the same sha256 the plane gets), redacted out of any
sentence that names it.

Without the OpenTelemetry packages installed :func:`enable` returns ``False``
and does nothing; importing this module never needs them. Like every
observer here it is fail-open: an exporter that raises never reaches the
call that produced the record.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import local_events
from .plane_types import key_hash, redact_key

_LOG = logging.getLogger("runbound")

_INSTRUMENTATION_NAME = "runbound"

_LOCK = threading.Lock()
_ACTIVE: "_Exporter | None" = None


def enable(*, logger_provider: Any = None, meter_provider: Any = None) -> bool:
    """Start exporting; ``True`` when the OpenTelemetry packages are present.

    ``logger_provider`` and ``meter_provider`` default to the global ones.
    Calling it again replaces the earlier export (one exporter per process).
    """
    global _ACTIVE
    try:
        from opentelemetry import _logs, metrics
    except ImportError:
        _LOG.debug("runbound.otel: opentelemetry is not installed; export is off")
        return False
    try:
        exporter = _Exporter(
            (logger_provider or _logs.get_logger_provider()).get_logger(_INSTRUMENTATION_NAME),
            (meter_provider or metrics.get_meter_provider()).get_meter(_INSTRUMENTATION_NAME),
            _logs,
        )
    except Exception:
        _LOG.warning("runbound.otel: could not start; export is off", exc_info=True)
        return False
    with _LOCK:
        previous, _ACTIVE = _ACTIVE, exporter
    if previous is not None:
        local_events.remove_sink(previous)
        local_events.remove_call_sink(previous._call_record)
    local_events.add_sink(exporter)
    local_events.add_call_sink(exporter._call_record)
    return True


def disable() -> None:
    """Stop exporting. A no-op when it is not on."""
    global _ACTIVE
    with _LOCK:
        exporter, _ACTIVE = _ACTIVE, None
    if exporter is not None:
        local_events.remove_sink(exporter)
        local_events.remove_call_sink(exporter._call_record)


class _Exporter:
    """A :mod:`runbound.local_events` sink that writes OpenTelemetry."""

    def __init__(self, logger: Any, meter: Any, logs_api: Any) -> None:
        self._logger = logger
        self._severity = logs_api.SeverityNumber
        self._refusals = meter.create_counter(
            "runbound.refusals", unit="{refusal}", description="Calls runbound refused.")
        self._anomalies = meter.create_counter(
            "runbound.anomalies", unit="{anomaly}", description="Anomalies runbound detected.")
        self._postures = meter.create_counter(
            "runbound.posture_changes", unit="{change}", description="Posture transitions.")
        self._guarded = meter.create_counter(
            "runbound.guarded_calls", unit="{call}", description="Model calls runbound guarded, however they ended.")
        self._usd = meter.create_counter(
            "runbound.estimated_usd", unit="USD", description="Cost runbound estimated for the model calls it guarded.")

    def __call__(self, record: dict) -> None:
        kind = record.get("kind")
        handler = _HANDLERS.get(kind)
        if handler is not None:
            handler(self, record)

    # --- one handler per record kind -----------------------------------------

    def _anomaly(self, record: dict) -> None:
        attrs = _base(record)
        attrs["runbound.detector"] = record.get("detector")
        attrs["runbound.severity"] = record.get("severity")
        attrs["runbound.reacted"] = record.get("reacted")
        attrs.update(_decision(record))
        critical = record.get("severity") == "critical"
        self._emit("runbound.anomaly", record, _text(record, record.get("message")),
                   self._severity.ERROR if critical else self._severity.WARN, attrs)
        self._anomalies.add(1, _only(attrs, "runbound.detector", "runbound.severity"))

    def _refusal(self, record: dict) -> None:
        attrs = _base(record)
        attrs["runbound.detector"] = record.get("detector")
        attrs["runbound.reacted"] = record.get("reacted")
        attrs.update(_decision(record))
        self._emit("runbound.refusal", record, _text(record, record.get("message")),
                   self._severity.ERROR, attrs)
        self._refusals.add(1, _only(
            attrs, "runbound.detector", "runbound.decision.boundary", "runbound.decision.level"))

    def _posture(self, record: dict) -> None:
        attrs = _base(record)
        attrs.update({
            "runbound.posture": record.get("posture"),
            "runbound.posture.from": record.get("from"),
            "runbound.posture.scope": record.get("scope"),
            "runbound.posture.source": record.get("source"),
            "runbound.posture.level": record.get("level"),
        })
        self._emit("runbound.posture", record, _text(record, record.get("reason")),
                   self._severity.INFO, attrs)
        self._postures.add(1, _only(attrs, "runbound.posture", "runbound.posture.source"))

    def _call_record(self, record: dict) -> None:
        if record.get("kind") == "call":
            self._call(record)

    def _call(self, record: dict) -> None:
        attrs = {
            "runbound.model": record.get("model"),
            "runbound.provider": record.get("provider"),
        }
        self._guarded.add(1, _only({**attrs, "runbound.outcome": record.get("outcome")}, *attrs, "runbound.outcome"))
        cost = record.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
            self._usd.add(float(cost), _only({**attrs, "runbound.priced": record.get("priced")},
                                             *attrs, "runbound.priced"))

    def _runtime_change(self, record: dict) -> None:
        attrs = {
            "runbound.runtime.what": record.get("what"),
            "runbound.runtime.from": _scalar(record.get("from")),
            "runbound.runtime.to": _scalar(record.get("to")),
        }
        self._emit("runbound.runtime_change", record,
                   f"{record.get('what')} changed", self._severity.INFO, attrs)

    def _emit(self, name: str, record: dict, body: Any, severity: Any, attrs: dict) -> None:
        self._logger.emit(
            timestamp=_nanos(record.get("at")),
            severity_number=severity,
            severity_text=severity.name,
            body=body,
            attributes={k: v for k, v in attrs.items() if v is not None},
            event_name=name,
        )


_HANDLERS = {
    "anomaly": _Exporter._anomaly,
    "refusal": _Exporter._refusal,
    "posture": _Exporter._posture,
    "runtime_change": _Exporter._runtime_change,
}


# --- shaping a record -----------------------------------------------------


def _base(record: dict) -> dict:
    """The attributes every record carries: its session and its key's hash."""
    attrs: dict = {"runbound.session_id": record.get("session_id")}
    key = record.get("key")
    if isinstance(key, str) and key:
        attrs["runbound.key_hash"] = key_hash(key)
    return attrs


def _text(record: dict, text: Any) -> str | None:
    """A sentence with the session's raw key redacted out of it."""
    if not isinstance(text, str):
        return None if text is None else str(text)
    key = record.get("key")
    return redact_key(text, key, key_hash(key)) if isinstance(key, str) and key else text


def _decision(record: dict) -> dict:
    """The record's Decision as ``runbound.decision.*`` attributes."""
    decision = (record.get("details") or {}).get("decision")
    if not isinstance(decision, dict):
        return {}
    attrs: dict = {}
    for name in ("verdict", "kind", "boundary", "level", "detector", "policy_version"):
        attrs[f"runbound.decision.{name}"] = _scalar(decision.get(name))
    attrs["runbound.decision.reason"] = _text(record, decision.get("reason"))
    evaluation = decision.get("evaluation")
    if isinstance(evaluation, dict):
        for name, value in evaluation.items():
            if isinstance(value, (bool, int, float, str)):
                attrs[f"runbound.decision.evaluation.{name}"] = _text(record, value) if isinstance(value, str) else value
    return attrs


def _scalar(value: Any) -> Any:
    """An OpenTelemetry attribute value: a primitive as itself, else its text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _only(attrs: dict, *names: str) -> dict:
    return {name: attrs[name] for name in names if attrs.get(name) is not None}


def _nanos(at: Any) -> int | None:
    return int(at * 1_000_000_000) if isinstance(at, (int, float)) else None
