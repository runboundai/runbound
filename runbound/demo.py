"""The whole core loop, for a stranger with no clone and no key::

    pip install runbound
    python -m runbound.demo

No account, no API key, no network: :func:`main` wraps a fake, in-process
provider transport (never a real client) so the story runs the same offline
as it would against a real one. It walks the shape every guarded agent hits
sooner or later -- a runaway, detected, narrowed to a safer posture, a
dangerous action refused, the run stopped -- and prints the
:class:`~runbound.events.Decision` behind each refusal in plain language.
Ends by printing ``PASS`` and exiting ``0``; a broken assertion prints
``FAIL`` and exits non-zero instead.

The demo quiets the ``"runbound"`` logger for the life of the walkthrough
(restored afterward, in a ``finally``) and narrates each refusal itself
instead: the library's own ``WARNING`` line and this module's ``print``
would otherwise say the same thing twice, in whichever order stdout and
stderr happen to interleave -- which, piped to a file or run under a test
harness, is not necessarily print order, since stdout is block-buffered
when it is not a terminal. One narrator, one stream, in order, wherever
this runs.

Standard library and the public ``runbound`` API only -- nothing here reaches
past what ``import runbound`` already gives an installed package. This module
is intentionally *not* imported by :mod:`runbound` itself (see
``tests/test_demo.py``): ``import runbound`` stays cheap, and the demo is
only ever loaded when a caller asks for it by name, the same way
:mod:`this` or :mod:`antigravity` are part of the standard library without
being loaded by every ``import`` of anything else.

``runbound-sdk/examples/core_loop_demo.py`` is the same walkthrough reached
from the examples directory instead of ``python -m``; both call this
module's :func:`main`.
"""

from __future__ import annotations

import logging
import sys

import runbound

MODEL = "demo-model"

_LOG = logging.getLogger("runbound")


class _Usage:
    prompt_tokens = 0
    completion_tokens = 1


class _Response:
    def __init__(self, model: str) -> None:
        self.model = model
        self.usage = _Usage()


class _FakeCompletions:
    """Just enough of an OpenAI-shaped ``chat.completions`` to be wrapped.

    Never opens a socket: every call returns immediately with a made-up
    response, so the whole demo runs in milliseconds with no network and no
    provider account.
    """

    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> _Response:
        self.calls += 1
        return _Response(str(kwargs.get("model", MODEL)))


def _fake_client(completions: _FakeCompletions):
    client = type("FakeOpenAI", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = completions
    return runbound.wrap(client)


def main() -> int:
    """Run the core loop once and return an exit code (``0`` for PASS,
    ``1`` on a failed assertion)."""
    previous_level = _LOG.level
    _LOG.setLevel(logging.ERROR)  # the narrative below says what the WARNING lines would
    try:
        runbound.init(on_anomaly="raise", max_actions_per_run=3)

        completions = _FakeCompletions()
        client = _fake_client(completions)
        executed: list[tuple[str, float]] = []

        @runbound.tool(effects={"financial"}, max_calls=10)
        def issue_refund(user: str, amount: float) -> str:
            executed.append((user, amount))
            return "refunded"

        @runbound.tool(effects={"read"})
        def lookup_order(order_id: str) -> dict:
            return {"order": order_id}

        with runbound.session("demo-user"):
            print("1. A runaway: a guarded model call, then three ordinary tool calls")
            print("   (this demo's local action cap is 3)")
            client.chat.completions.create(model=MODEL, messages=[])
            assert completions.calls == 1
            lookup_order("A1")
            lookup_order("A2")
            lookup_order("A3")

            print()
            print("2. Detected: narrowed to 'restricted' by hand -- free, local, no plane")
            print("   required. Every locally-decidable safety control ships free in the")
            print("   open-source SDK, forever.")
            runbound.enter_safe_mode(reason="spend looks like a runaway", posture="restricted")
            print(f"   posture is now: {runbound.posture()}")
            assert runbound.posture() == "restricted"

            print()
            print("3. A financial action is denied: issue_refund declares the 'financial'")
            print("   capability, and 'restricted' denies it.")
            try:
                issue_refund("u", 20.0)
                raise AssertionError("expected 'restricted' to refuse issue_refund")
            except runbound.SafeModeViolation as exc:
                print(f"   refused: {exc.anomaly.message}")
                print(f"   Decision: boundary={exc.decision.boundary!r} reason={exc.decision.reason!r}")
            assert executed == []

            print()
            print("4. Stopped: a fourth ordinary call crosses this run's own action cap.")
            try:
                lookup_order("A4")
                raise AssertionError("expected the action cap to refuse a fourth call")
            except runbound.GuardrailTripped as exc:
                print(f"   refused: {exc.anomaly.message}")
                print(f"   Decision: boundary={exc.decision.boundary!r} reason={exc.decision.reason!r}")
                assert exc.decision.boundary == "blast_radius"

            env = runbound.envelope()
            print(f"   envelope().execution.actions_remaining: {env['execution']['actions_remaining']}")
            assert env["execution"]["actions_remaining"] == 0

        print()
        print("PASS")
        return 0
    except AssertionError as exc:
        print()
        print(f"FAIL: {exc}")
        return 1
    finally:
        _LOG.setLevel(previous_level)


if __name__ == "__main__":
    sys.exit(main())
