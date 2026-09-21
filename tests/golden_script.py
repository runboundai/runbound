"""The golden fixture: what runbound observably does, before the execution
envelope exists.

This is not a pytest test module (no ``test_`` prefix; ``tests/test_behaviour_golden.py``
is the test that reads it) — it is the one place this scenario is defined,
so the capture that produced the fixtures and the check that guards them can
never drift apart. Both call :func:`run_scenario`.

THE SCENARIO HAS TWO PHASES
===========================
**Phase one** (the bulk of it) runs under ``on_anomaly="warn"`` so a trip
never stops the run — the same shape as ``_script()`` in
``tests/test_budget_soft_and_reservation.py:173-193``:

* three "walls" — ``budget_usd``, ``max_steps``, ``max_total_tokens`` — each
  walked past and then past again. Each gets its **own keyed session**
  (``golden:wall-budget``, ``golden:wall-steps``, ``golden:wall-tokens``):
  ``BudgetDetector`` fires at most once per session for *either*
  ``budget_usd`` or ``max_total_tokens`` (``runbound/detectors.py:184-199``),
  so the only way to observe both independently in one run is to give them
  different sessions. Every one of these calls is uncapped (no
  ``max_tokens``/``max_completion_tokens``/``max_output_tokens``), so
  ``budget_admission`` cannot affect this part at all — see
  ``test_with_no_stated_cap_capped_is_byte_identical_to_admission_off`` in
  that same file.
* a **reservation wall**, its own session (``golden:wall-reservation``):
  one uncapped call spends $2 of the $3 budget, then one call
  *states* a 2-token cap on a model priced at $1/output token — a $2.00 worst
  case against $1.00 remaining. Under the default ``budget_admission="capped"``
  this is refused before it goes out (``detector="budget"``,
  ``details["reason"] == details["rule"] == "reservation"``,
  ``details["reserved_usd"]`` present) and the fake never sees it; under
  ``budget_admission=False`` admission never runs at all, so the same call
  goes out and is recorded (actual usage 1 token, $1, landing the session
  exactly at its $3 limit without crossing it). This is the one call in the
  whole scenario where the two fixtures are expected to — and do — diverge:
  see ``test_the_two_fixtures_differ`` and the "WHAT DIFFERS" section below.
* one streamed call, exhausted by ordinary iteration (not closed early, not
  abandoned) — no stream-finalizer/``gc`` timing to be nondeterministic about.
* one provider error: a ``create()`` that raises a plain ``RuntimeError``.
  ``on_provider_failure`` stays at its default ("notify"), so this does not
  open the circuit or raise ``CircuitOpen`` — a bare provider failure that
  propagates unchanged, exactly as today's wrapper promises.
* four ``@runbound.tool``-decorated calls, in the default (unkeyed) session:

  - ``limited_tool()`` (``max_calls=1``), called twice — once allowed, once
    refused by the policy engine alone (``detector="policy"``,
    ``details["rule"] == "max_calls"``).
  - ``financial_tool()`` (``effects={"financial"}``, no ``max_calls``), called
    once after a posture of ``"restricted"``, set by hand with
    ``runbound.enter_safe_mode`` (free and local, on the process and the
    session — see "THE ``enter_safe_mode`` PROBLEM" below) — refused by the
    posture alone (``detector="safe_mode"``).
  - ``both_tool()`` (``effects={"financial"}`` **and** ``max_calls=1``),
    called twice after the same posture — a tool that would trip
    *both* rules. ``_announce_call`` runs ``_admit_posture`` before
    ``_enforce_policy`` (``runbound/api.py:3165-3169``), so **both** calls are
    refused by the posture, never by the policy — in particular the second
    call, which is the one whose ``calls_so_far`` (2) already exceeds
    ``max_calls=1`` and would be a policy violation if the posture check ever
    let it through. This is the invariant this fixture pins ("a call refused
    by both reports posture"): swap the two checks in ``_announce_call`` and
    this call's refusal becomes ``detector="policy"`` instead, which is
    exactly the regression this golden exists to catch. Both ``both_tool``
    refusals are deduped out of the *anomalies* list — ``Engine._alert``'s
    dedup key for ``safe_mode`` is ``(session_id, "safe_mode", "warn", None,
    None)`` with no per-tool component (unlike ``policy``, whose key includes
    the rule and the tool — ``runbound/engine.py:1241-1245``), and
    ``financial_tool``'s refusal already claimed that key for this session —
    but both still raise, and both still appear in *refusals*, because that
    list is built from each exception's own ``.anomaly``, independent of
    whether an observer ever heard about it.

**Phase two** exists for one purpose: a deterministic envelope deny must
latch "exactly as the wall would have", and phase one
never latches anything (``on_anomaly="warn"`` never does — see
``Engine._react``), so a golden built only from phase one cannot protect that
sentence at all. Phase two tears everything down, re-``init()``s the *same*
config with ``on_anomaly="raise"`` instead, and walks one fresh keyed session
(``golden:latch``) six calls into ``max_steps=5``: the sixth raises
``GuardrailTripped`` and latches. Captured under the result's ``"latching"``
key: the latched anomaly's detector (``is_tripped("golden:latch").detector``)
and the refusal that raised. Both phases tear down completely before and
after (``api._teardown_for_tests()`` plus ``_coverage.reset_for_tests()``), so
no posture, no registry entry and no engine state leaks from phase one into
phase two or out of the scenario altogether — a fresh ``Engine`` (and a fresh
default session) is built for each phase's ``init()``.

Spike detection is on by default (``spike_detection=True``, free and local)
but never actually fires in this scenario: every session here makes at
most a handful of calls, well under ``spike_warmup_calls`` (4), so the
detector's baseline never arms and it never reports anything for the
``anomalies`` comparison to see. ``BASE_INIT_KWARGS`` states no
``spike_detection`` override on purpose — captured with and without an
explicit ``spike_detection=False``, the two fixtures come back
byte-identical, which is the evidence for leaving it unstated rather than
pinning a value that turns out to change nothing here.

WHAT IS COMPARED, AND WHAT IS DELIBERATELY EXCLUDED
====================================================
:func:`run_scenario` returns a plain, JSON-able dict with exactly these keys:

* ``refusals`` — for each call that raised, in the order it raised:
  ``exception`` (the exception class name), and, when the exception carries
  an ``.anomaly`` (every ``GuardrailTripped`` does; a bare provider error does
  not), ``detector``, and ``details.get("rule")`` / ``details.get("reason")``.
  **Not** the message text: ``str(exc)``/``anomaly.message`` is prose, and
  is expected to get reworded over time. A refusal missing "rule"
  or "reason" (the policy ``max_calls`` violation carries no "reason" key at
  all; a plain wall trip like phase two's ``steps`` anomaly carries neither)
  reports ``None`` for it rather than guessing.
* ``anomalies`` — every anomaly a recording observer *actually saw* in phase
  one, as ``(detector, severity, reacted, details)``, with ``session_id``,
  ``anomaly_id`` and the (not-yet-existing) ``decision`` key stripped from
  ``details``. Stripping ``decision`` now, before it exists, is what lets this
  same fixture stay valid once a ``Decision`` is added to every refusal
  anomaly — the comparison must not start failing on a key this fixture
  predates. This
  faithfully includes today's dedup behaviour (see ``both_tool`` above): it is
  not "every refusal", it is what an observer is actually told, which is a
  real, worth-protecting fact about the engine.
* ``client_calls`` — phase one's fake completions object's own ``.calls``
  counter: every request that actually reached it. Tool calls do not call
  ``create()`` and are not counted here; a reservation refusal is turned away
  at the door and does not reach the fake either, so this number is sensitive
  to admission mode (see "WHAT DIFFERS" below).
* ``budget`` — a dict keyed by session, not a single reading: ``"default"``
  (the unkeyed session the stream, the provider error and the tool calls run
  in) plus each of the four keyed sessions above, each as
  ``runbound.budget(key)`` via ``dataclasses.asdict``. A single "default
  session" reading would be structurally all zeros — nothing spends money
  there — which would read the same whether the budget machinery worked or
  had been deleted outright; the four keyed sessions are where this scenario
  actually spends, and their numbers are what changing the reservation or
  wall math would move.
* ``is_tripped`` — the *default* session's latched anomaly's detector, or
  ``None``. It reads ``None`` in phase one, honestly: every wall trip there
  happens under ``on_anomaly="warn"``, which never latches, and the default
  session itself never crosses any wall. That is still worth recording (the
  envelope must not make it non-``None`` by accident) — the case where it
  *should* be non-``None`` is covered separately, by ``latching`` below.
* ``latching`` — phase two's result: ``{"detector": ..., "refusal": {...}}``,
  the one place this fixture actually exercises a latch and ``is_tripped()``
  returning something other than ``None``.

Deliberately **excluded**, and why — nothing in this scenario's actual output
needed excluding (no anomaly here carries a timestamp, a salted hash or a raw
set), but the exclusions are named so a future edit to the scenario cannot
silently reintroduce one without this list changing too:

* wall-clock / monotonic values (``entered_at``, any ``*_at``, ``duration_s``,
  ``seconds``, ages) — none of ``budget``/``steps``/``policy``/``safe_mode``
  anomaly details carry one, and the scenario never reads ``PostureState`` or
  any other object that would.
* the per-process salted hash (``runbound.api._HASH_SALT`` /
  ``_args_hash`` — see ``tests/test_hash_salt.py``) — no compared anomaly
  carries an ``args_hash`` (only ``tool_request``/``tool_call`` *events* do,
  and events are not part of the comparison surface at all).
* floats — rounded to 6 decimal places by :func:`_round_floats`, applied to
  the whole result, on general principle: this scenario's arithmetic is exact
  (every price is a whole-dollar-per-token rate and every token count is an
  integer), but rounding costs nothing and a future edit to the scenario
  should not have to rediscover this rule.
* set/dict ordering — ``effects`` is already stored sorted
  (``runbound/engine.py``, ``_violation``/``_posture_anomaly`` build it via
  ``sorted(effects or ())``); nothing else in the compared surface is
  derived from a set.
* ``events`` — captured by ``_script()`` in the budget/reservation tests, but
  not part of this fixture's comparison surface, so left out here entirely
  rather than half-captured.

WHAT DIFFERS BETWEEN THE TWO FIXTURES, AND WHY
===============================================
``behaviour_golden.json`` (``budget_admission`` at its default, ``"capped"``)
and ``behaviour_golden_no_admission.json`` (``budget_admission=False``) now
genuinely diverge, entirely because of the reservation wall's second call:

* ``refusals`` has one more entry under ``"capped"`` (the reservation
  refusal); ``anomalies`` likewise has one more (``detector="budget"``,
  ``details["reason"] == "reservation"``).
* ``client_calls`` differs by exactly one: the refused call never reaches the
  fake under ``"capped"``, and does under ``False``.
* ``budget["golden:wall-reservation"]`` differs: ``spent`` is $2.00 under
  ``"capped"`` (the refused call adds nothing) and $3.00 under ``False`` (it
  goes out and actually costs $1.00); ``remaining`` is $1.00 vs $0.00
  accordingly.

Nothing else differs — every other call in the scenario is deliberately
uncapped, and an uncapped call takes the same path regardless of
``budget_admission``. ``test_the_two_fixtures_differ`` names these
fields explicitly rather than asserting general inequality, so a future
change that happens to make the two fixtures diverge somewhere *else* is
still required to explain itself here.

THE ``envelope=False`` PROBLEM
===============================
This golden is captured "with ``envelope=False``". Before the execution
envelope existed, ``envelope`` was not yet a ``GuardrailConfig`` field, so
``runbound.init(envelope=False)`` would have raised ``TypeError``.
:func:`_prepare_init_kwargs` resolves that: it drops any key that
``dataclasses.fields(GuardrailConfig)`` does not define, logging each dropped
key at DEBUG, before calling ``init()``. Every init-kwargs dict this module
builds (:data:`BASE_INIT_KWARGS` and its ``budget_admission=False`` variant,
used by both phases) includes ``envelope=False`` un-conditionally. Against a
build that predates the envelope, that key is silently dropped and the
scenario runs unmodified, which is what makes these fixtures a valid
pre-envelope baseline at all; against today's build, the exact same dict is
passed to the exact same :func:`run_scenario` and ``envelope=False`` is
accepted and takes effect (turning the admission-at-the-door stage off) —
the scenario code itself never changes, which is what makes this a
*before/after* comparison of one script rather than two different
experiments that happen to look similar.

THE ``enter_safe_mode`` PROBLEM
================================
``runbound.enter_safe_mode`` is free and local — callable directly, on the
process and the session, without a connected plane — and this scenario's
posture half (``financial_tool``/``both_tool``, above) calls it directly:
``runbound.enter_safe_mode(reason="behaviour golden: incident drill",
posture="restricted")``. An earlier revision of this scenario drove the
same posture through a fleet directive instead
(:class:`~test_shared_state.FakePlane`, a ``HelloReply(posture="restricted")``
applied through ``api._SHARED.apply_hello``), to cover the case where a
connected plane narrows a posture rather than a local call. Recapturing the
fixture against the direct call reproduced the fleet-directive capture
byte for byte, which is the strongest evidence available that the two paths
— a local call and a plane directive — converge on the same observable
result. The plane-directive posture path is still covered, just not by this
fixture: ``tests/test_postures.py`` and ``test_plane_only_controls.py``'s
own ``test_a_plane_directive_still_moves_the_posture_too`` cover it
directly.

REGENERATING
============
Regenerating a fixture is a decision to be justified in a commit message,
never a way to make a red test green::

    python -m tests.golden_script --write

rewrites both ``tests/fixtures/behaviour_golden.json`` and
``tests/fixtures/behaviour_golden_no_admission.json`` from the scenario as
it exists right now. If ``test_behaviour_golden.py`` is red, the right fix
is almost always to the code the scenario is exercising — not to the
fixture.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import pathlib
import sys
from typing import Any

import runbound
from runbound import _coverage, api
from runbound.config import GuardrailConfig

_LOG = logging.getLogger("runbound.golden_script")

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
DEFAULT_FIXTURE = FIXTURES_DIR / "behaviour_golden.json"
NO_ADMISSION_FIXTURE = FIXTURES_DIR / "behaviour_golden_no_admission.json"

#: $1.00/output token, $0/input token — a call's cost is exactly its output
#: token count, in whole dollars, with no chars/4 estimate anywhere in it.
MODEL_BUDGET = "golden-budget-model"
#: Free: input and output both price at $0, so token accumulation never
#: touches ``budget_usd``.
MODEL_FREE = "golden-free-model"

SHORT = [{"role": "user", "content": "hi"}]

KEY_BUDGET = "golden:wall-budget"
KEY_STEPS = "golden:wall-steps"
KEY_TOKENS = "golden:wall-tokens"
KEY_RESERVATION = "golden:wall-reservation"
KEY_LATCH = "golden:latch"

#: The scenario's own configuration. ``envelope=False`` is dropped by
#: :func:`_prepare_init_kwargs` against a pre-envelope build and accepted by
#: today's — see the module docstring's "envelope=False problem" section.
BASE_INIT_KWARGS: dict[str, Any] = {
    "budget_usd": 3.0,
    "max_steps": 5,
    "max_total_tokens": 1000,
    "on_anomaly": "warn",
    "custom_prices": {
        MODEL_BUDGET: (0.0, 1_000_000.0),
        MODEL_FREE: (0.0, 0.0),
    },
    "envelope": False,
}

ROUND_NDIGITS = 6


def _no_admission_kwargs() -> dict:
    kwargs = dict(BASE_INIT_KWARGS)
    kwargs["budget_admission"] = False
    return kwargs


# --- fakes -------------------------------------------------------------


class _Usage:
    def __init__(self, tokens_in: int, tokens_out: int) -> None:
        self.prompt_tokens = tokens_in
        self.completion_tokens = tokens_out


class _Response:
    def __init__(self, model: str, tokens_in: int, tokens_out: int) -> None:
        self.model = model
        self.usage = _Usage(tokens_in, tokens_out)


class _FakeChunk:
    def __init__(self, text: str = "", model: str | None = None, usage: _Usage | None = None) -> None:
        self.text = text
        self.model = model
        self.usage = usage


class _FakeStream:
    """Shaped like ``openai.Stream``: iterator, context manager, closeable."""

    def __init__(self, chunks: list) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self) -> "_FakeStream":
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


class GoldenCompletions:
    """One OpenAI-shaped ``create`` driving every model call in one phase.

    Tokens and cost are stated explicitly per call, via ``tokens_in`` /
    ``tokens_out`` keyword arguments the fake reads directly, rather than
    inferred from the model name — the scenario controls its own arithmetic
    exactly instead of guessing at what a real provider would return.
    ``induce_error=True`` raises instead of answering, ``stream=True``
    returns a small, fixed, three-chunk stream ending in a usage-bearing
    chunk, and a stated ``max_tokens`` (or the other cap fields) is read by
    the wrapper itself, before this fake ever sees the call, for the
    reservation check — it plays no part in what this fake returns.
    """

    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: Any):
        self.calls += 1
        if kwargs.get("induce_error"):
            raise RuntimeError("golden_script: simulated provider outage")
        if kwargs.get("stream"):
            chunks = [
                _FakeChunk(text="Hel", model=MODEL_FREE),
                _FakeChunk(text="lo", model=MODEL_FREE),
                _FakeChunk(text="", model=MODEL_FREE, usage=_Usage(0, 1)),
            ]
            return _FakeStream(chunks)
        return _Response(kwargs["model"], kwargs.get("tokens_in", 0), kwargs.get("tokens_out", 0))


class GoldenClient:
    """Shaped like ``openai.OpenAI``: ``client.chat.completions.create``."""

    def __init__(self) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = GoldenCompletions()


class _Recorder:
    """Collects every anomaly a recording observer sees, verbatim."""

    def __init__(self) -> None:
        self.anomalies: list = []

    def on_event(self, session: Any, event: Any) -> None:  # pragma: no cover - unused
        pass

    def on_anomaly(self, session: Any, anomaly: Any, reacted: str) -> None:
        self.anomalies.append((anomaly, reacted))


# --- helpers shared by both phases --------------------------------------


def _prepare_init_kwargs(kwargs: dict) -> dict:
    """Drop any key ``GuardrailConfig`` does not (yet) define, logged at DEBUG.

    See the module docstring's "envelope=False problem" section: this is what
    lets the same ``init_kwargs`` dict run unchanged before and after the
    ``envelope`` field exists.
    """
    known = {f.name for f in dataclasses.fields(GuardrailConfig)}
    accepted: dict = {}
    dropped: list = []
    for key, value in kwargs.items():
        if key in known:
            accepted[key] = value
        else:
            dropped.append(key)
    if dropped:
        _LOG.debug(
            "golden_script: init kwargs unknown to GuardrailConfig, dropped: %s",
            sorted(dropped),
        )
    return accepted


def _call(
    client: GoldenClient,
    model: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    max_tokens: int | None = None,
) -> None:
    kwargs: dict = dict(model=model, messages=SHORT, tokens_in=tokens_in, tokens_out=tokens_out)
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    client.chat.completions.create(**kwargs)


def _refusal(exc: BaseException) -> dict:
    anomaly = getattr(exc, "anomaly", None)
    details = getattr(anomaly, "details", None) or {}
    return {
        "exception": type(exc).__name__,
        "detector": getattr(anomaly, "detector", None),
        "rule": details.get("rule"),
        "reason": details.get("reason"),
    }


def _round_floats(obj: Any) -> Any:
    """Round every float in ``obj`` to :data:`ROUND_NDIGITS` places, recursively.

    Applied once, to the whole result, on general principle (see the module
    docstring) — this scenario's own arithmetic never actually produces an
    inexact float, but a future edit to it should not have to rediscover why
    this matters.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, ROUND_NDIGITS)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def _budget_dict(key: str | None) -> dict | None:
    view = runbound.budget(key)
    return None if view is None else dataclasses.asdict(view)


# --- phase one: the walls, the reservation, the stream, the tools --------


def _run_phase_one(init_kwargs: dict) -> dict:
    runbound.init(**_prepare_init_kwargs(init_kwargs))
    recorder = _Recorder()
    api._ENGINE.observers.append(recorder)
    client = runbound.wrap(GoldenClient())

    refusals: list = []

    # --- three walls, each its own keyed session (see module docstring:
    # BudgetDetector fires once per session for *either* budget_usd or
    # max_total_tokens, so budget_usd and max_total_tokens each need their
    # own session to be observed independently) ---
    with runbound.session(KEY_BUDGET):
        for tokens_out in (1, 1, 2, 1):  # 3rd call crosses $3.00; 4th walks past it
            _call(client, MODEL_BUDGET, tokens_out=tokens_out)

    with runbound.session(KEY_STEPS):
        for _ in range(7):  # 6th call crosses 5 steps; 7th walks past it
            _call(client, MODEL_FREE, tokens_out=1)

    with runbound.session(KEY_TOKENS):
        for _ in range(5):  # 4th call crosses 1000 tokens; 5th walks past it
            _call(client, MODEL_FREE, tokens_out=300)

    # --- the reservation wall: $2 spent, then a call whose stated
    # 2-token cap ($2.00 worst case) exceeds the $1.00 left. Under
    # budget_admission="capped" this is refused before it reaches the fake;
    # under False it goes out and is recorded (actual cost $1.00) — the one
    # place the two fixtures are expected to diverge (see module docstring's
    # "WHAT DIFFERS" section) ---
    with runbound.session(KEY_RESERVATION):
        _call(client, MODEL_BUDGET, tokens_out=2)
        try:
            _call(client, MODEL_BUDGET, tokens_out=1, max_tokens=2)
        except Exception as exc:
            refusals.append(_refusal(exc))

    # --- the default (unkeyed) session: one exhausted stream, one
    # provider error, four decorated tool calls ---
    stream = client.chat.completions.create(model=MODEL_FREE, messages=SHORT, stream=True)
    for _ in stream:
        pass

    try:
        client.chat.completions.create(model=MODEL_FREE, messages=SHORT, induce_error=True)
    except Exception as exc:  # the fake's own RuntimeError, propagated unchanged
        refusals.append(_refusal(exc))

    @runbound.tool(name="golden_limited_tool", effects={"read"}, max_calls=1)
    def limited_tool() -> str:
        return "done"

    @runbound.tool(name="golden_financial_tool", effects={"financial"}, max_calls=1)
    def financial_tool() -> str:
        return "done"

    @runbound.tool(name="golden_both_tool", effects={"financial"}, max_calls=1)
    def both_tool() -> str:
        return "done"

    limited_tool()  # 1st call: allowed
    try:
        limited_tool()  # 2nd call: refused by the policy's own max_calls=1 alone
    except Exception as exc:
        refusals.append(_refusal(exc))

    runbound.enter_safe_mode(reason="behaviour golden: incident drill", posture="restricted")
    try:
        financial_tool()  # refused by the posture alone: restricted denies "financial"
    except Exception as exc:
        refusals.append(_refusal(exc))

    try:
        both_tool()  # 1st call: also refused by the posture (max_calls=1 alone would allow it)
    except Exception as exc:
        refusals.append(_refusal(exc))
    try:
        both_tool()  # 2nd call: would ALSO violate max_calls=1 now — posture still wins
    except Exception as exc:
        refusals.append(_refusal(exc))

    anomalies = [
        (
            anomaly.detector,
            anomaly.severity,
            reacted,
            {
                k: v
                for k, v in anomaly.details.items()
                if k not in ("session_id", "anomaly_id", "decision")
            },
        )
        for anomaly, reacted in recorder.anomalies
    ]

    budgets = {"default": _budget_dict(None)}
    for key in (KEY_BUDGET, KEY_STEPS, KEY_TOKENS, KEY_RESERVATION):
        budgets[key] = _budget_dict(key)

    tripped = runbound.is_tripped()

    return {
        "refusals": refusals,
        "anomalies": anomalies,
        "client_calls": client.chat.completions.calls,
        "budget": budgets,
        "is_tripped": None if tripped is None else tripped.detector,
    }


# --- phase two: a latch, on purpose --------------------------------------


def _run_phase_two(init_kwargs: dict) -> dict:
    """Walk one fresh session into ``max_steps`` under ``on_anomaly="raise"``.

    See the module docstring: this is the only place in the fixture where
    something actually latches, because phase one runs entirely under
    ``on_anomaly="warn"``, which never does.
    """
    latch_kwargs = dict(init_kwargs)
    latch_kwargs["on_anomaly"] = "raise"
    runbound.init(**_prepare_init_kwargs(latch_kwargs))
    recorder = _Recorder()
    api._ENGINE.observers.append(recorder)
    client = runbound.wrap(GoldenClient())

    refusal = None
    with runbound.session(KEY_LATCH):
        try:
            for _ in range(6):  # max_steps=5; the 6th call crosses and latches
                _call(client, MODEL_FREE, tokens_out=1)
        except Exception as exc:
            refusal = _refusal(exc)

    tripped = runbound.is_tripped(KEY_LATCH)
    return {
        "detector": None if tripped is None else tripped.detector,
        "refusal": refusal,
    }


def run_scenario(init_kwargs: dict) -> dict:
    """Run both phases of the fixed scenario and return the semantic outputs.

    ``init_kwargs`` is passed to :func:`runbound.init` (after
    :func:`_prepare_init_kwargs` filters it) for phase one, and again, with
    ``on_anomaly`` forced to ``"raise"``, for phase two. See the module
    docstring for what each phase does and exactly what is (and is not) in
    the returned dict.

    Tears down completely before phase one, between the two phases, and after
    phase two — exactly like every other test in this suite — so neither
    phase ever sees the other's (or another call's) leftover state, and
    nothing is left behind for whatever runs next.
    """
    api._teardown_for_tests()
    _coverage.reset_for_tests()
    try:
        result = _run_phase_one(init_kwargs)
    finally:
        api._teardown_for_tests()
        _coverage.reset_for_tests()

    try:
        result["latching"] = _run_phase_two(init_kwargs)
    finally:
        api._teardown_for_tests()
        _coverage.reset_for_tests()

    return _round_floats(result)


# --- regeneration entry point -------------------------------------------


def _write_fixture(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.golden_script",
        description=(
            "Golden fixture regeneration. See the module docstring: "
            "regenerating is a decision to justify in a commit message, "
            "never a way to make a red test green."
        ),
    )
    parser.add_argument(
        "--write", action="store_true", help="Rewrite both fixtures from the scenario as it exists now."
    )
    parser.add_argument(
        "--print",
        dest="which",
        choices=["default", "no_admission"],
        help="Print one scenario's JSON to stdout without writing a file.",
    )
    args = parser.parse_args(argv)

    if args.write:
        _write_fixture(DEFAULT_FIXTURE, run_scenario(dict(BASE_INIT_KWARGS)))
        _write_fixture(NO_ADMISSION_FIXTURE, run_scenario(_no_admission_kwargs()))
        print(f"wrote {DEFAULT_FIXTURE}\nwrote {NO_ADMISSION_FIXTURE}")
        return 0

    if args.which is not None:
        kwargs = dict(BASE_INIT_KWARGS) if args.which == "default" else _no_admission_kwargs()
        print(json.dumps(run_scenario(kwargs), sort_keys=True))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
