"""The abuse ladder, as one state machine.

A session that starts behaving unlike itself is not slammed shut: under
``on_spike="limit"`` it climbs a ladder. Quiet (0), watching (1), limited (2),
closed (3), and — once a key has spent every strike — blocked (4). The rungs
were once written twice, in the spike detector and in the api's rollover, and
two copies of one state machine is one copy too many.

This module holds the whole of it, and nothing else:

* :class:`Observation` — everything the ladder reacts to, named. The caller
  says what it saw; it does not say what should happen next.
* :func:`transition` — a pure function from ``(level, observation, config)``
  to the rung the session lands on, why, and the :class:`Effect`\\ s the
  caller must apply. No clock, no state, no I/O: plain data in, plain data
  out, so the machine's whole behavior is one table test away.
* :func:`observation` and :func:`entry_observation` — the two classifiers
  that turn a detector's or the api's raw facts into an
  :class:`Observation`. They read configuration (``spike_confirm``,
  ``spike_max_strikes``); the rungs themselves do not.

Applying an effect is the caller's job, because only the caller can: the
detector owns the allowance counter and the anomalies, the api owns the key
registry, the strike ledger and the latch. What neither of them owns any more
is the decision.

Level 4 is worn by the *latch* a blocked key is held on, not by a session's
own counter: a blocked key is rolled over like any other, and its fresh
session starts at level 0 behind a latch that never expires. So
:func:`transition` never returns 4 — see :data:`LEVEL_BLOCKED`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .config import GuardrailConfig

#: The session is behaving like itself.
LEVEL_QUIET = 0
#: One abnormal call has been noticed; nothing is limited.
LEVEL_WATCHING = 1
#: A confirmed spike: the session has an allowance of abnormal calls left.
LEVEL_LIMITED = 2
#: The allowance ran out; the session is over and awaits its rollover.
LEVEL_CLOSED = 3
#: The terminus, carried by the latch rather than by a session — see the
#: module docstring. A key here is refused until :func:`runbound.clear`.
LEVEL_BLOCKED = 4

#: Every rung, for anyone enumerating the machine (the table test does).
LEVELS: tuple[int, ...] = (
    LEVEL_QUIET,
    LEVEL_WATCHING,
    LEVEL_LIMITED,
    LEVEL_CLOSED,
    LEVEL_BLOCKED,
)

#: The reason of a transition that is not one: nothing moved, nothing is
#: recorded in the session's ladder history.
UNCHANGED = "unchanged"


class Observation(Enum):
    """What a caller saw, in the ladder's vocabulary.

    The first six are one model call, judged against the session's own recent
    calls; the last two are a request arriving for a key whose session the
    ladder has closed.
    """

    #: A call that looks like the session's others, and a trailing window
    #: that no longer confirms a spike.
    NORMAL_CALL = "normal_call"
    #: A call that looks normal while the trailing window still confirms.
    NORMAL_CALL_STILL_SPIKING = "normal_call_still_spiking"
    #: One abnormal call, not yet enough of them to confirm, on a session
    #: nobody has been told about yet.
    ABNORMAL_CALL = "abnormal_call"
    #: The same, on a session already reported once.
    ABNORMAL_CALL_ALREADY_NOTICED = "abnormal_call_already_noticed"
    #: Enough of the trailing calls are abnormal to confirm a spike.
    CONFIRMED_SPIKE = "confirmed_spike"
    #: A limited session just spent the last of its allowance.
    ALLOWANCE_GONE = "allowance_gone"
    #: The key is entered again on a session the ladder closed.
    ENTRY_AFTER_CLOSE = "entry_after_close"
    #: ...and this rollover spends the key's last strike.
    ENTRY_OUT_OF_STRIKES = "entry_out_of_strikes"


class Effect(Enum):
    """What the caller must do about a transition it just asked for."""

    #: Start the limit's allowance at the session's base.
    SET_ALLOWANCE = "set_allowance"
    #: Charge this abnormal call one of the allowance.
    SPEND_ALLOWANCE = "spend_allowance"
    #: Forget the allowance: the session is behaving again.
    HEAL = "heal"
    #: Close the session and ask the api for a rollover.
    CLOSE = "close"
    #: Retire the key's session and register its next generation.
    ROLLOVER = "rollover"
    #: Hold the fresh session shut with no expiry, not a cooldown.
    BLOCK = "block"


@dataclass(frozen=True)
class Transition:
    """Where a session lands, why, and what the caller must apply."""

    next_level: int
    reason: str
    effects: frozenset[Effect] = field(default_factory=frozenset)

    @property
    def moved(self) -> bool:
        """Is this a transition at all — one to record and to report on?"""
        return self.reason != UNCHANGED


def _stay(level: int) -> Transition:
    """The answer to everything the ladder does not react to."""
    return Transition(level, UNCHANGED, frozenset())


def _to(level: int, reason: str, *effects: Effect) -> Transition:
    return Transition(level, reason, frozenset(effects))


#: The ladder itself. Every pair absent from it leaves the session where it
#: is: the machine is total, and silence is its most common answer.
_RUNGS: dict[tuple[int, Observation], Transition] = {
    # Quiet and watching: one abnormal call is a notice, a confirmed spike is
    # a limit. A session already reported on is not reported on twice.
    (LEVEL_QUIET, Observation.ABNORMAL_CALL): _to(LEVEL_WATCHING, "first_abnormal"),
    (LEVEL_QUIET, Observation.NORMAL_CALL_STILL_SPIKING): _to(
        LEVEL_LIMITED, "confirmed", Effect.SET_ALLOWANCE
    ),
    (LEVEL_QUIET, Observation.CONFIRMED_SPIKE): _to(
        LEVEL_LIMITED, "confirmed", Effect.SET_ALLOWANCE
    ),
    (LEVEL_WATCHING, Observation.ABNORMAL_CALL): _to(LEVEL_WATCHING, "first_abnormal"),
    (LEVEL_WATCHING, Observation.NORMAL_CALL_STILL_SPIKING): _to(
        LEVEL_LIMITED, "confirmed", Effect.SET_ALLOWANCE
    ),
    (LEVEL_WATCHING, Observation.CONFIRMED_SPIKE): _to(
        LEVEL_LIMITED, "confirmed", Effect.SET_ALLOWANCE
    ),
    # Limited: every further abnormal call costs one of the allowance and is
    # otherwise silent; a call that looks normal again, on a window that no
    # longer confirms, heals the session back to watching.
    (LEVEL_LIMITED, Observation.NORMAL_CALL): _to(
        LEVEL_WATCHING, "healed", Effect.HEAL
    ),
    (LEVEL_LIMITED, Observation.ABNORMAL_CALL): _to(
        LEVEL_LIMITED, "allowance_spent", Effect.SPEND_ALLOWANCE
    ),
    (LEVEL_LIMITED, Observation.ABNORMAL_CALL_ALREADY_NOTICED): _to(
        LEVEL_LIMITED, "allowance_spent", Effect.SPEND_ALLOWANCE
    ),
    (LEVEL_LIMITED, Observation.CONFIRMED_SPIKE): _to(
        LEVEL_LIMITED, "allowance_spent", Effect.SPEND_ALLOWANCE
    ),
    (LEVEL_LIMITED, Observation.ALLOWANCE_GONE): _to(
        LEVEL_CLOSED, "allowance_spent", Effect.CLOSE
    ),
}

#: Entry answers the same way from every rung, because the api asks only
#: once it has established that the ladder closed this key's session.
_ENTRY: dict[Observation, Transition] = {
    Observation.ENTRY_AFTER_CLOSE: _to(LEVEL_QUIET, "rollover", Effect.ROLLOVER),
    Observation.ENTRY_OUT_OF_STRIKES: _to(
        LEVEL_QUIET, "blocked", Effect.ROLLOVER, Effect.BLOCK
    ),
}


def transition(
    level: int, observation: Observation, config: GuardrailConfig
) -> Transition:
    """Where ``observation`` leaves a session sitting on ``level``.

    Pure and total: every level — including the ones a session cannot be
    found on today — has an answer for every observation, and the answer
    depends on nothing but these three arguments. Anything the ladder does
    not react to leaves the session exactly where it was, with no effects and
    no history entry (:attr:`Transition.moved` is then ``False``).

    Closed (3) and blocked (4) sessions do not move on model calls: only an
    entry does, by rolling the key over. Under any ``on_spike`` but
    ``"limit"`` there is no ladder at all, so nothing ever moves.
    """
    if config.on_spike != "limit":
        return _stay(level)
    entry = _ENTRY.get(observation)
    if entry is not None:
        return entry
    return _RUNGS.get((level, observation), _stay(level))


def observation(
    *, abnormal: bool, abnormal_recent: int, noticed: bool, config: GuardrailConfig
) -> Observation:
    """Name what one model call looked like.

    ``abnormal`` is this call's own verdict, ``abnormal_recent`` how many of
    the trailing window were abnormal (``spike_confirm`` of them confirm a
    spike), and ``noticed`` whether this session has already been reported
    on once.
    """
    confirmed = abnormal_recent >= config.spike_confirm
    if not abnormal:
        return (
            Observation.NORMAL_CALL_STILL_SPIKING
            if confirmed
            else Observation.NORMAL_CALL
        )
    if confirmed:
        return Observation.CONFIRMED_SPIKE
    return (
        Observation.ABNORMAL_CALL_ALREADY_NOTICED
        if noticed
        else Observation.ABNORMAL_CALL
    )


def entry_observation(strikes: int, config: GuardrailConfig) -> Observation:
    """Name a request arriving for a key whose session the ladder closed.

    ``strikes`` is the count this rollover would leave the key on. Reaching
    ``spike_max_strikes`` is what separates a cooldown from a block.
    """
    return (
        Observation.ENTRY_OUT_OF_STRIKES
        if strikes >= config.spike_max_strikes
        else Observation.ENTRY_AFTER_CLOSE
    )
