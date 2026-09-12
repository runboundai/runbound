#!/usr/bin/env python3
"""One end-user pushed down the whole abuse ladder, nobody else touched.

    .venv/bin/python examples/ladder_demo.py

No network, no API key, no packages: the model calls below are reported
straight to runbound with the numbers a real wrapper would have measured,
and the latch clock is a fake, so a 100-second cooldown costs this demo
nothing.

The business code never changes. Every turn is the same
`with runbound.session(key):` block it always was, and the only exception it
ever sees is the `GuardrailTripped` it already handles. What changes is how
hard runbound leans on one session: a notice, then a limit, then a closed
session with a cooldown, then a block.
"""

import logging

import runbound
from runbound import GuardrailTripped, api
from runbound import engine as engine_module

ABUSER = "user:5310"
REGULAR = "user:1041"
MODEL = "gpt-4o"

NORMAL_SECONDS = 2.0  # what this bot's answers normally take
SPIKE_SECONDS = 400.0  # what the exploit prompt costs it
COOLDOWN = 100.0
WARM_TURNS = 15  # ordinary chatting before anything happens


class Clock:
    """The clock runbound ages a latch against, advanced by hand."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:  # pragma: no cover - never slept
        pass

    def advance(self, seconds: float) -> None:
        self.t += seconds


CLOCK = Clock()


class _Notices(logging.Handler):
    """Prints runbound's own log lines inline, where the story needs them."""

    def emit(self, record: logging.LogRecord) -> None:
        print(f"             {record.getMessage()}")


def turn(key: str, seconds: float = NORMAL_SECONDS):
    """One chatbot turn for one end-user; the anomaly if it was stopped.

    A real integration calls a wrapped client here. This demo hands runbound
    the same numbers directly so the durations can be fictional.
    """
    try:
        with runbound.session(key, tags={"service": "support-bot"}):
            api._record_llm_call(MODEL, 900, 150, duration_s=seconds)
        return None
    except GuardrailTripped as tripped:
        # runbound stopped the session and handed control straight back; the
        # sentence the end-user reads is the business's to write, not ours.
        return tripped.anomaly


def show(label: str) -> None:
    status = runbound.session_status(ABUSER)
    print(f"  {label:<18} level {status['level']}   "
          f"allowance left {status['allowance_left']}   "
          f"strikes {status['strikes']}   "
          f"back in {status['cooldown_remaining_s']:.0f}s")


def climb(warm_turns: int):
    """Chat normally, spike to the limit, then spend the allowance down."""
    for _ in range(warm_turns):
        turn(ABUSER)
    turn(ABUSER, SPIKE_SECONDS)
    show("first spike")
    turn(ABUSER, SPIKE_SECONDS)
    show("confirmed")
    for _ in range(6):
        anomaly = turn(ABUSER, SPIKE_SECONDS)
        if anomaly is not None:
            return anomaly
        show("another spike")
    raise AssertionError("the ladder never closed the session")


def main() -> int:
    engine_module.time = CLOCK  # the demo's latch clock, so a cooldown is free
    logger = logging.getLogger("runbound")
    logger.setLevel(logging.INFO)
    logger.addHandler(_Notices())
    print("\n" + "=" * 74)
    print("The abuse ladder: one end-user walked all the way down it")
    print("=" * 74)
    print("  runbound.init(on_anomaly='raise', on_spike='limit',")
    print("                  spike_limit_calls=2, spike_cooldown_seconds=100,")
    print("                  spike_max_strikes=2)   # tightened so this is short")
    runbound.init(
        on_anomaly="raise", on_spike="limit", spike_warmup_calls=4, spike_confirm=2,
        spike_limit_calls=2, spike_cooldown_seconds=COOLDOWN, spike_max_strikes=2,
        spike_min_duration_s=1.0,
    )

    print("\nACT 1  two end-users chat; runbound learns what each one's normal is")
    for _ in range(WARM_TURNS):
        turn(REGULAR)
    print(f"  {REGULAR}: {WARM_TURNS} turns, 2.0s per answer, nothing said")

    print(f"\nACT 2  {ABUSER} finds a prompt that makes the model think for 400s")
    anomaly = climb(WARM_TURNS)
    print(f"  the closing call raised: {anomaly.message}")
    show("closed")

    print("\nACT 3  their next message is refused at the door - no model call at all")
    anomaly = turn(ABUSER)
    print(f"  GuardrailTripped: action={anomaly.details['action']!r}, "
          f"strike {anomaly.details['strikes']} of {anomaly.details['max_strikes']}")
    show("rolled over")
    print(f"  {REGULAR} in the meantime: stopped={turn(REGULAR) is not None}")

    print(f"\nACT 4  {COOLDOWN:.0f}s pass; the key is served again, on tighter terms")
    CLOCK.advance(COOLDOWN + 1)
    print(f"  first message after the cooldown: stopped={turn(ABUSER) is not None}")
    anomaly = climb(8)
    print(f"  half the allowance, so it closes sooner: strike "
          f"{anomaly.details['strikes']} of {anomaly.details['max_strikes']}")

    print("\nACT 5  the last strike: blocked until the business says otherwise")
    anomaly = turn(ABUSER)
    print(f"  GuardrailTripped: level {anomaly.details['level']}, "
          f"action={anomaly.details['action']!r}")
    CLOCK.advance(10_000.0)
    print(f"  and no cooldown heals it: still "
          f"action={turn(ABUSER).details['action']!r} three hours later")

    print("\nACT 6  support forgives them: runbound.clear(key)")
    runbound.clear(ABUSER)
    print(f"  next message: stopped={turn(ABUSER) is not None}")
    show("after clear()")

    print("\n  level 1  watching  a first odd call is a notice, nothing stops")
    print("  level 2  limited   confirmed spiking costs an allowance, not an answer")
    print("  level 3  closed    the allowance ran out: rollover + 100s cooldown")
    print("  level 4  blocked   strikes spent; only clear() lets them back in")
    print(f"\n  {REGULAR} was never touched: every one of their turns was served.")
    print("  The door closed gradually, one rung at a time, and the business's")
    print("  code never changed - same session(key) block, same GuardrailTripped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
