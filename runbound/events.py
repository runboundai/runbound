"""Immutable event records — the only thing detectors ever look at.

Events are frozen: once an edge (a client wrapper or the ``@tool`` decorator)
creates one, nothing downstream can mutate it.
"""

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(frozen=True)
class Event:
    """A single observed action inside an agent session.

    ``ts`` is monotonic time, so events are safe to compare across a clock
    change. ``args_hash`` is a sha256 hex digest of canonicalized arguments;
    raw arguments are never stored.

    ``tokens_out`` is the provider's completion count. ``tokens_reasoning``
    is the *subset* of it that was reasoning (thinking) — OpenAI's
    ``completion_tokens_details.reasoning_tokens``, Anthropic's
    ``output_tokens_details.thinking_tokens`` (T173) — carried separately so a
    caller can see the split. It is never added to ``tokens_out``, priced, or
    counted against a cap: doing so would bill and measure a thinking call
    twice (see ``SessionState.record``).

    ``tokens_cached_in`` (T139) is the subset of ``tokens_in`` that was served
    from the provider's prompt cache — never additional tokens on top of
    ``tokens_in``, always a slice of it, so ``total_tokens`` keeps meaning
    "every token this call touched" without double-counting. It is priced at
    a discount where the model publishes a cached-input rate
    (:mod:`runbound.pricing`) and at the plain input rate otherwise.

    The five kinds:

    ``llm_call``
        A model call that returned (tokens, cost, model, duration).
    ``tool_call``
        A decorated tool the developer's code actually ran (tool_name,
        args_hash).
    ``tool_error``
        That tool raised (tool_name, error).
    ``llm_error``
        A model call that failed (error, model, duration_s) — what a retry
        storm is made of, and what the provider circuit breaker counts.
    ``tool_request``
        A tool call the *model asked for*, recorded before the developer
        dispatches it (tool_name, args_hash). Its hash carries a ``"req:"``
        prefix so a request and the executed call it leads to are never
        mistaken for two of the same action.

    ``loop_exempt`` marks a ``tool_call``/``tool_request`` that must count
    towards ``tool_calls()`` and any action policy's ``max_calls`` — this is
    a real, executed (or requested) action — but never towards the loop
    window: ``@runbound.tool(repeatable=True)`` and a name listed in
    ``loop_ignore_tools`` both set it. It marks polling, not a tunable knob:
    a tool the customer has told runbound is *supposed* to repeat should
    never trip the loop detector, however the count comes out.

    ``priced`` names how ``cost_usd`` was priced when it was not a plain
    table lookup: ``"estimated"`` when the model had no static or custom
    price and ``on_unpriced_model="estimate"`` (or a ``"refuse"`` call that
    still had to be recorded, e.g. via :func:`~runbound.record_call`) priced
    it from ``unpriced_price_per_1m_usd`` instead. ``None`` is an ordinary,
    exactly-priced (or genuinely free) call.

    ``partial`` marks an ``llm_call`` that never actually finished — a
    streamed response abandoned mid-read, reported by a finalizer once the
    stream proxy is garbage collected. Every detector, budget and alert
    treats it like any other call; only the provider circuit is left alone,
    because a call whose real outcome was never observed is not evidence the
    provider is failing. ``tokens_estimated`` says whether *that* partial
    call's ``tokens_out`` came from the provider's own usage (``False``) or
    was guessed from the character count of what streamed by before it was
    abandoned (``True``) — independent of ``priced``, which is about the
    dollar figure, not the token count it was computed from.
    """

    kind: str  # "llm_call" | "tool_call" | "tool_error" | "llm_error" | "tool_request"
    ts: float  # time.monotonic() at creation
    step: int  # 1-based step number within session
    tokens_in: int = 0
    tokens_cached_in: int = 0  # subset of tokens_in served from the provider's cache
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    tool_name: str | None = None
    args_hash: str | None = None  # sha256 hex of canonicalized args
    error: str | None = None
    duration_s: float = 0.0  # wall time the call took, 0.0 when unmeasured
    tokens_reasoning: int = 0  # thinking tokens, a subset of tokens_out
    loop_exempt: bool = False  # counts for tool_calls()/max_calls, not the loop window
    priced: str | None = None  # "estimated" when cost_usd came from the unpriced fallback
    partial: bool = False  # an llm_call reported by an abandoned-stream finalizer
    tokens_estimated: bool = False  # a partial call's tokens_out is chars/4, not real usage


@dataclass(frozen=True)
class Anomaly:
    """A detector's verdict that something has gone wrong.

    ``message`` is human-readable and includes the numbers that tripped the
    detector; ``details`` carries the same numbers machine-readably.

    ``anomaly_id`` is this verdict's own identity, stamped once here and
    never again. One anomaly reaches the control plane on two channels — the
    synchronous trip (``POST /v1/trip``) and the telemetry export
    (``POST /v1/events``) — and the id is what lets the plane recognise the
    two reports as one fact instead of guessing from their timing. It is a
    fresh random hex string per anomaly, derived from nothing about the
    session, the key or the customer, so it carries no information beyond
    "these two reports are the same one".
    """

    detector: str  # "loop" | "budget" | "velocity" | "steps" | "events" | ...
    severity: str  # "warn" | "critical"
    message: str  # human-readable, includes numbers
    details: dict
    # Last, and defaulted, so every positional construction keeps working.
    anomaly_id: str = field(default_factory=lambda: uuid4().hex)


#: Which detector wins a tie among anomalies of the same severity (T135).
#:
#: Before this, ties among critical anomalies co-firing on the same event
#: were decided by the order ``detectors.DEFAULT_DETECTORS`` happened to list
#: them in — invisible, and an accident of that list's history rather than a
#: stated decision. This is the one place the order is stated; the engine's
#: winner-selection (``engine._winner``) reads it instead of iteration order,
#: so reversing ``DEFAULT_DETECTORS`` produces the same winner.
#:
#: Lower number wins. Fixed order (EM decision, highest first): a policy
#: violation outranks everything (the customer wrote the rule themselves);
#: then budget, loop, error_storm, steps, events, timeout, spike, velocity —
#: roughly cost, then repetition, then failure, then shape, then time, then
#: behavior, with velocity (warn-only, never stops anything) last. Door
#: anomalies (``halt``, ``circuit``, ``inflight``, ``plane``) are never in a
#: tie because they are raised before detection ever runs, so they have no
#: entry here. A detector not listed here — a customer's own, or one this
#: table has not caught up with yet — sorts after every named one and never
#: crashes the selection; see ``engine._priority_rank``.
PRIORITY: dict[str, int] = {
    "policy": 0,
    "budget": 1,
    "loop": 2,
    "error_storm": 3,
    "steps": 4,
    "events": 5,
    "timeout": 6,
    "spike": 7,
    "velocity": 8,
}
