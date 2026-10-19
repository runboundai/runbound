"""One span per tool call: ``execute_tool {tool}``, kind INTERNAL, scope ``runbound``.

Two paths, chosen **per call** and never both (a user who has OpenTelemetry wants one pipeline):

1. **A configured provider.** When ``opentelemetry`` is importable and its global
   ``TracerProvider`` is a real one (not the API's proxy), the span goes to that provider and
   its exporters, nests under whatever span is current, and becomes current while the body
   runs. Imported lazily; the SDK never depends on it, and never installs a provider.
2. **The stdlib emitter.** Otherwise, if ``init(otel_endpoint=...)`` names a collector, the span
   is queued on a :class:`~runbound.otlp.SpanExporter` and posted as OTLP/JSON.

With neither, :func:`begin` returns ``None`` and a call pays one attribute lookup.

A span carries the GenAI standard attributes and ``runbound.*`` ones (``otel_names.py``);
never an argument, a result or prompt text. Status: an exception from the body is ERROR with
``error.type``; a refusal is **not** a failure of the tool, so its status stays UNSET with
``runbound.outcome=refused`` and ``error.type=runbound.refused``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from . import ids as ids_module
from . import otel_names as n
from . import otlp
from .plane_types import redact_key

_LOG = logging.getLogger("runbound")

#: The stdlib emitter, set by :func:`configure`. ``None`` when ``otel_endpoint`` is unset.
_EXPORTER: "otlp.SpanExporter | None" = None
_RESOURCE: dict = {}

#: ``opentelemetry.trace`` and its context module, imported on first use; ``False`` if absent.
_API: Any = None


def configure(config: Any, version: str) -> None:
    """(Re)start the stdlib emitter for ``config``; called by :func:`runbound.init`. Never raises."""
    global _EXPORTER, _RESOURCE
    previous, _EXPORTER = _EXPORTER, None
    if previous is not None:
        previous.stop()
    endpoint = getattr(config, "otel_endpoint", None)
    if not endpoint:
        return
    try:
        _RESOURCE = {
            n.RESOURCE_SERVICE_NAME: config.service,
            n.RESOURCE_INSTANCE_ID: config.resolved_worker_id(),
            n.RESOURCE_SDK_NAME: "runbound",
        }
        exporter = otlp.SpanExporter(endpoint, config.token or None, _RESOURCE, version)
        exporter.start()
        _EXPORTER = exporter
    except Exception:
        _LOG.warning("runbound: could not start the OTLP span emitter; no spans", exc_info=True)


def shutdown(timeout: "float | None" = None) -> None:
    """Stop the stdlib emitter, posting what is queued (within ``timeout`` seconds when given)."""
    global _EXPORTER
    exporter, _EXPORTER = _EXPORTER, None
    if exporter is not None:
        exporter.stop() if timeout is None else exporter.stop(timeout)


def flush(timeout: float = 3.0) -> None:
    """Post the queued call spans now (up to ``timeout`` seconds) and keep the emitter running."""
    exporter = _EXPORTER
    if exporter is not None:
        exporter.flush(timeout)


def exporter() -> "otlp.SpanExporter | None":
    return _EXPORTER


def active() -> bool:
    """Would a span started now be received by anything?"""
    return _EXPORTER is not None or _provider() is not None


def _otel() -> Any:
    """``(trace, context)`` from opentelemetry-api, or ``None`` when it is not installed."""
    global _API
    if _API is None:
        try:
            from opentelemetry import context, trace

            _API = (trace, context)
        except Exception:
            _API = False
    return _API or None


def _provider() -> Any:
    """The user's real global ``TracerProvider``, or ``None`` (not installed, or only the proxy)."""
    api = _otel()
    if api is None:
        return None
    try:
        provider = api[0].get_tracer_provider()
        return None if type(provider).__name__ == "ProxyTracerProvider" else provider
    except Exception:
        return None


def begin(tool_name: str, run_id: str | None, version: str, call_id: str | None = None) -> "CallSpan | None":
    """Start the span for one call, or ``None`` when nothing would receive it."""
    try:
        provider = _provider()
        if provider is not None:
            return CallSpan(tool_name, call_id, provider=provider, version=version, run_id=run_id)
        if _EXPORTER is not None:
            return CallSpan(tool_name, call_id, run_id=run_id)
    except Exception:
        _LOG.debug("runbound: could not start a span", exc_info=True)
    return None


class CallSpan:
    """The span of one call in flight. :meth:`end` finishes it exactly once."""

    def __init__(
        self, tool_name: str, call_id: str | None, *, provider: Any = None, version: str = "", run_id: str | None = None
    ) -> None:
        self.tool = tool_name
        self.call_id = call_id
        self.run_id = run_id
        self.args_digest: str | None = None
        self._ended = False
        self._marked = False
        self._start_ns = time.time_ns()
        self._span = self._token = self._tracer = None
        if provider is not None:
            trace, context = _otel()
            tracer = self._tracer = provider.get_tracer(n.SCOPE_NAME, version)
            self._span = tracer.start_span(n.SPAN_NAME.format(tool=tool_name), kind=trace.SpanKind.INTERNAL)
            self._token = context.attach(trace.set_span_in_context(self._span))
        else:
            self._trace_id = otlp.new_trace_id(run_id)
            self._span_id = otlp.new_span_id()

    def mark_started(self, ids: "dict | None" = None, config: Any = None) -> None:
        """Record that the tool body is about to run: a zero-length child span ``execution_started``, once per call. Never raises.

        It goes where the call's own span goes (the stdlib exporter, or the customer's tracer under their provider), so it is exported on the exporter's tick: a death inside that window,
        or during an outage of the receiver, loses it. The call span is exported only when the call ENDS, so a crash in the body loses exactly the span that carries the attempt's
        identity; the marker therefore carries it too: the action, attempt and evaluation ids, the boundary and the declared target, the tool (as ``runbound.marker.tool``, never
        ``gen_ai.tool.name``), the service, the worker and the run id. No argument, digest, decision or reason.
        """
        if self._marked or self._ended:
            return
        self._marked = True
        try:
            attrs = {n.BOUNDARY: ids_module.BOUNDARY_DECORATOR, n.DECLARED_TARGET: ids_module.BOUNDARY_DECORATOR, n.CONTRACT_VERSION: ids_module.CONTRACT_VERSION, n.MARKER_TOOL: self.tool,
                     n.RUN_ID: self.run_id, n.SERVICE: getattr(config, "service", None), n.WORKER_ID: config.resolved_worker_id() if config is not None else None,
                     n.SERVICE_VERSION: _service_version()}
            for name, key in ((n.ACTION_ID, "action_id"), (n.ATTEMPT_ID, "attempt_id"), (n.EVALUATION_ID, "evaluation_id")):
                attrs[name] = (ids or {}).get(key)
            attrs = {k: v for k, v in attrs.items() if v is not None}
            if self._span is not None:
                now = time.time_ns()
                marker = self._tracer.start_span(n.START_MARKER_NAME, start_time=now)
                for key, value in attrs.items():
                    marker.set_attribute(key, value)
                marker.end(end_time=now)
            elif _EXPORTER is not None:
                now = time.time_ns()
                _EXPORTER.add(otlp.span_json(self._trace_id, otlp.new_span_id(), n.START_MARKER_NAME, now, now, attrs, parent_span_id=self._span_id))
        except Exception:
            _LOG.debug("runbound: could not mark an execution start", exc_info=True)

    def context_ids(self) -> dict:
        """The span's own ``trace_id`` and ``span_id`` (lower-case hex), so the refusal that call raises can name the span that records it. ``{}`` if unknown."""
        try:
            if self._span is not None:
                context = self._span.get_span_context()
                return {"trace_id": format(context.trace_id, "032x"), "span_id": format(context.span_id, "016x")}
            return {"trace_id": self._trace_id, "span_id": self._span_id}
        except Exception:
            return {}

    def end(self, facts: dict, exc: BaseException | None = None) -> None:
        """Finish the span with ``facts`` (already the attribute dict). Never raises."""
        if self._ended:
            return
        self._ended = True
        try:
            attrs = {k: v for k, v in facts.items() if v is not None}
            if self._span is not None:
                self._end_provider(attrs, exc)
            elif _EXPORTER is not None:
                status = otlp.STATUS_ERROR if exc is not None else otlp.STATUS_UNSET
                _EXPORTER.add(otlp.span_json(
                    self._trace_id, self._span_id, n.SPAN_NAME.format(tool=self.tool),
                    self._start_ns, time.time_ns(), attrs, status,
                ))
        except Exception:
            _LOG.debug("runbound: could not end a span", exc_info=True)

    def _end_provider(self, attrs: dict, exc: BaseException | None) -> None:
        trace, context = _otel()
        try:
            for key, value in attrs.items():
                self._span.set_attribute(key, list(value) if isinstance(value, tuple) else value)
            if exc is not None:
                self._span.record_exception(exc)
                self._span.set_status(trace.Status(trace.StatusCode.ERROR))
        finally:
            self._span.end()
            if self._token is not None:
                context.detach(self._token)


def error_type(exc: BaseException) -> str:
    """The exception's qualified class name (``builtins.`` left off), never its message."""
    cls = type(exc)
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def _service_version() -> "str | None":
    """``RUNBOUND_SERVICE_VERSION`` (what the deployment says it is running), or ``None``: a comparison label, never a decision input."""
    value = os.environ.get("RUNBOUND_SERVICE_VERSION")
    return value if isinstance(value, str) and value else None


def facts(
    tool: str, outcome: str, *, config: Any, run_id: str | None, key: str | None, effects: frozenset,
    call_id: str | None, args_digest: str | None, exc: BaseException | None, decision: Any,
    policy_version: int | None, flags: tuple, flag_score: float, result_chars: int,
    approval_request_id: str | None = None, would_refuse: str | None = None, record: "dict | None" = None,
    reason_code: str | None = None, ids: "dict | None" = None, anomaly_id: str | None = None, effects_source: str | None = None,
) -> dict:
    """The attribute dict of one finished call (RS-3 / otel-clickhouse.md §1.2)."""
    refused = outcome == "refused"
    out = {
        n.OPERATION_NAME: n.OPERATION_EXECUTE_TOOL,
        n.TOOL_NAME: tool,
        n.TOOL_TYPE: n.TOOL_TYPE_FUNCTION,
        n.TOOL_CALL_ID: call_id,
        n.SERVICE: getattr(config, "service", None),
        n.WORKER_ID: config.resolved_worker_id() if config is not None else None,
        n.RUN_ID: run_id,
        n.CAPABILITY: ",".join(sorted(effects)) or None,
        n.CAPABILITY_SOURCE: effects_source or ("decorator" if effects else "undeclared"),
        n.CAPABILITY_EFFECTIVE: (record or {}).get("effective_effects"),
        n.SERVICE_VERSION: _service_version(),
        n.OUTCOME: outcome,
        n.ARGS_DIGEST: args_digest,
        n.RESULT_BYTES: result_chars or None,
        n.RESULT_FLAGS: tuple(flags) or None,
        n.RESULT_FLAG_SCORE: flag_score or None,
        n.APPROVAL_REQUEST_ID: approval_request_id,
        n.DECISION_VERDICT: "refuse" if refused else "would_refuse" if would_refuse else "allow",
        n.DECISION_KIND: "action",
        n.BOUNDARY: ids_module.BOUNDARY_DECORATOR,
        n.CONTRACT_VERSION: ids_module.CONTRACT_VERSION,
    }
    for name, key in ((n.ACTION_ID, "action_id"), (n.ATTEMPT_ID, "attempt_id"), (n.EVALUATION_ID, "evaluation_id"), (n.PARENT_ACTION_ID, "parent_action_id")):
        out[name] = (ids or {}).get(key)
    if refused:
        out[n.ERROR_TYPE] = n.ERROR_TYPE_REFUSED
        out[n.ANOMALY_ID] = anomaly_id  # the refusal event this span records: the same refusal, provably (AT-02)
        if decision is not None:
            reason = decision.reason
            out[n.DECISION_BOUNDARY] = decision.boundary
            out[n.DECISION_LEVEL] = decision.level
            out[n.DECISION_DETECTOR] = decision.detector
            out[n.DECISION_REASON] = redact_key(reason, key) if isinstance(reason, str) else None
    elif exc is not None:
        out[n.ERROR_TYPE] = error_type(exc)
    if would_refuse and not refused:  # a dry_run row's rule: the call ran, the span says what a live row would have done
        out[n.DECISION_BOUNDARY] = "policy"
        out[n.DECISION_DETECTOR] = "policy"
        out[n.DECISION_REASON] = would_refuse
    pv = policy_version if policy_version else getattr(decision, "policy_version", None)
    out[n.DECISION_POLICY_VERSION] = pv or None
    if record:  # the decision record: the primary code (an existing reason, verbatim), what matched, what contributed, in which state
        matched = tuple(record.get("matched_rule_ids") or ())
        out[n.DECISION_REASON_CODE] = reason_code if refused and reason_code else (
            would_refuse if would_refuse else "rule" if matched else "fallback")
        out[n.DECISION_MATCHED_RULE_IDS] = matched
        out[n.DECISION_SERVICE_DEFAULT_REVISION] = record.get("service_default_revision")
        out[n.DECISION_POLICY_STATE] = record.get("policy_state")
        contributors = record.get("contributors") or ()
        stamped = getattr(decision, "evaluation", None)
        if refused and isinstance(stamped, dict) and stamped.get("contributors"):
            contributors = stamped["contributors"]  # the refusal's own record: it also names the limit that refused the call
        out[n.DECISION_CONTRIBUTORS] = tuple(contributors)
        out[n.SDK_VERSION] = record.get("sdk_version")
        # VPC (R1, R-C): the revision and configuration the call ran under, and what the revision being tried made of it. Absent when not served / not being tried.
        out[n.DECISION_RULE_REVISION] = record.get("rule_revision")
        out[n.DECISION_CFG_HASH] = record.get("cfg_hash")
        out[n.DECISION_BOOT_ID] = record.get("boot_id")
        out[n.WAITED_MS] = record.get("waited_ms")
        out[n.DECISION_CANDIDATE_OUTCOME] = record.get("candidate_outcome")
        out[n.DECISION_CANDIDATE_REVISION] = record.get("candidate_revision")
        out[n.DECISION_CANDIDATE_OF] = record.get("candidate_of")
        rechecked = record.get("rechecked")
        if isinstance(rechecked, dict):
            for field in n.RECHECK_FIELDS:
                out[n.RECHECK_PREFIX + field] = rechecked.get(field)
        for name, key in ((n.DECISION_RECHECK_RULE_REVISION, "recheck_rule_revision"), (n.DECISION_RECHECK_CFG_HASH, "recheck_cfg_hash"),
                          (n.DECISION_RECHECK_POLICY_VERSION, "recheck_policy_version"), (n.DECISION_RECHECK_SERVICE_DEFAULT_REVISION, "recheck_service_default_revision"),
                          (n.DECISION_RECHECK_CONTRIBUTOR, "recheck_contributor"), (n.DECISION_RECHECK_REASON, "recheck_reason")):
            out[name] = record.get(key)
    return out
