"""The abuse ladder's state machine, one row per (level, observation) pair.

:func:`runbound.ladder.transition` is pure — plain data in, plain data out —
so the whole of the ladder fits in the table below and needs no session, no
detector, no clock and no mock to exercise. Every pair is here, including the
ones a session cannot be found on today (a closed or blocked session making
model calls), because a total function is what lets the two callers stop
guarding against each other's edge cases.

The tests that watch the ladder *behave* — a chatbot user climbing it call by
call, the rollover, the strikes, the cooldown — live in ``test_ladder_detector``,
``test_ladder_api`` and ``test_ladder_why``. This module only pins what the
machine decides.
"""

import dataclasses
import itertools

import pytest

from runbound.config import GuardrailConfig
from runbound.ladder import (
    LEVEL_BLOCKED,
    LEVEL_CLOSED,
    LEVEL_LIMITED,
    LEVEL_QUIET,
    LEVEL_WATCHING,
    LEVELS,
    UNCHANGED,
    Effect,
    Observation,
    Transition,
    entry_observation,
    observation,
    transition,
)

# Short names, so the table reads as a table.
NORMAL = Observation.NORMAL_CALL
NORMAL_SPIKING = Observation.NORMAL_CALL_STILL_SPIKING
ABNORMAL = Observation.ABNORMAL_CALL
ABNORMAL_AGAIN = Observation.ABNORMAL_CALL_ALREADY_NOTICED
CONFIRMED = Observation.CONFIRMED_SPIKE
GONE = Observation.ALLOWANCE_GONE
ENTRY = Observation.ENTRY_AFTER_CLOSE
LAST_ENTRY = Observation.ENTRY_OUT_OF_STRIKES

SET, SPEND, HEAL, CLOSE, ROLLOVER, BLOCK = (
    Effect.SET_ALLOWANCE,
    Effect.SPEND_ALLOWANCE,
    Effect.HEAL,
    Effect.CLOSE,
    Effect.ROLLOVER,
    Effect.BLOCK,
)

#: Entry answers the same from every rung: the api asks only once it has
#: established that the ladder closed this key's session.
ROLL = (LEVEL_QUIET, "rollover", (ROLLOVER,))
BLOCKED = (LEVEL_QUIET, "blocked", (ROLLOVER, BLOCK))


def stay(level: int) -> tuple:
    """The ladder does not react: same rung, no reason, no effects."""
    return (level, UNCHANGED, ())


#: The whole machine. ``(level, observation) -> (next level, reason, effects)``.
LADDER: dict[tuple[int, Observation], tuple] = {
    # Quiet: one abnormal call is a notice; enough of them is a limit.
    (LEVEL_QUIET, NORMAL): stay(LEVEL_QUIET),
    (LEVEL_QUIET, NORMAL_SPIKING): (LEVEL_LIMITED, "confirmed", (SET,)),
    (LEVEL_QUIET, ABNORMAL): (LEVEL_WATCHING, "first_abnormal", ()),
    (LEVEL_QUIET, ABNORMAL_AGAIN): stay(LEVEL_QUIET),
    (LEVEL_QUIET, CONFIRMED): (LEVEL_LIMITED, "confirmed", (SET,)),
    (LEVEL_QUIET, GONE): stay(LEVEL_QUIET),
    (LEVEL_QUIET, ENTRY): ROLL,
    (LEVEL_QUIET, LAST_ENTRY): BLOCKED,
    # Watching: the notice is not repeated, but a confirmed spike still limits.
    (LEVEL_WATCHING, NORMAL): stay(LEVEL_WATCHING),
    (LEVEL_WATCHING, NORMAL_SPIKING): (LEVEL_LIMITED, "confirmed", (SET,)),
    (LEVEL_WATCHING, ABNORMAL): (LEVEL_WATCHING, "first_abnormal", ()),
    (LEVEL_WATCHING, ABNORMAL_AGAIN): stay(LEVEL_WATCHING),
    (LEVEL_WATCHING, CONFIRMED): (LEVEL_LIMITED, "confirmed", (SET,)),
    (LEVEL_WATCHING, GONE): stay(LEVEL_WATCHING),
    (LEVEL_WATCHING, ENTRY): ROLL,
    (LEVEL_WATCHING, LAST_ENTRY): BLOCKED,
    # Limited: abnormal calls cost allowance, a normal one heals the session,
    # and an allowance spent to nothing closes it.
    (LEVEL_LIMITED, NORMAL): (LEVEL_WATCHING, "healed", (HEAL,)),
    (LEVEL_LIMITED, NORMAL_SPIKING): stay(LEVEL_LIMITED),
    (LEVEL_LIMITED, ABNORMAL): (LEVEL_LIMITED, "allowance_spent", (SPEND,)),
    (LEVEL_LIMITED, ABNORMAL_AGAIN): (LEVEL_LIMITED, "allowance_spent", (SPEND,)),
    (LEVEL_LIMITED, CONFIRMED): (LEVEL_LIMITED, "allowance_spent", (SPEND,)),
    (LEVEL_LIMITED, GONE): (LEVEL_CLOSED, "allowance_spent", (CLOSE,)),
    (LEVEL_LIMITED, ENTRY): ROLL,
    (LEVEL_LIMITED, LAST_ENTRY): BLOCKED,
    # Closed: the session is over. Only the key's next entry moves it, and
    # that is the api's rollover, not another model call.
    (LEVEL_CLOSED, NORMAL): stay(LEVEL_CLOSED),
    (LEVEL_CLOSED, NORMAL_SPIKING): stay(LEVEL_CLOSED),
    (LEVEL_CLOSED, ABNORMAL): stay(LEVEL_CLOSED),
    (LEVEL_CLOSED, ABNORMAL_AGAIN): stay(LEVEL_CLOSED),
    (LEVEL_CLOSED, CONFIRMED): stay(LEVEL_CLOSED),
    (LEVEL_CLOSED, GONE): stay(LEVEL_CLOSED),
    (LEVEL_CLOSED, ENTRY): ROLL,
    (LEVEL_CLOSED, LAST_ENTRY): BLOCKED,
    # Blocked: the terminus. Nothing a blocked key does moves it; only
    # runbound.clear() lets it back in, and that is not a ladder transition.
    (LEVEL_BLOCKED, NORMAL): stay(LEVEL_BLOCKED),
    (LEVEL_BLOCKED, NORMAL_SPIKING): stay(LEVEL_BLOCKED),
    (LEVEL_BLOCKED, ABNORMAL): stay(LEVEL_BLOCKED),
    (LEVEL_BLOCKED, ABNORMAL_AGAIN): stay(LEVEL_BLOCKED),
    (LEVEL_BLOCKED, CONFIRMED): stay(LEVEL_BLOCKED),
    (LEVEL_BLOCKED, GONE): stay(LEVEL_BLOCKED),
    (LEVEL_BLOCKED, ENTRY): ROLL,
    (LEVEL_BLOCKED, LAST_ENTRY): BLOCKED,
}


def ladder_config(**overrides) -> GuardrailConfig:
    settings = {"on_spike": "limit"}
    settings.update(overrides)
    config = GuardrailConfig(**settings)
    config.validate()
    return config


@pytest.fixture
def config() -> GuardrailConfig:
    return ladder_config()


def label(pair: tuple[int, Observation]) -> str:
    return f"L{pair[0]}-{pair[1].value}"


PAIRS = list(itertools.product(LEVELS, Observation))


def test_the_table_covers_every_level_and_observation_exactly_once():
    """No pair is missing from the table above, and none is invented."""
    assert set(LADDER) == set(PAIRS)


@pytest.mark.parametrize("pair", PAIRS, ids=label)
def test_every_pair_lands_where_the_table_says(pair, config):
    level, seen = pair
    next_level, reason, effects = LADDER[pair]
    assert transition(level, seen, config) == Transition(
        next_level, reason, frozenset(effects)
    )


@pytest.mark.parametrize("pair", PAIRS, ids=label)
def test_a_transition_that_moves_nothing_is_never_recorded(pair, config):
    """``moved`` is what tells the caller to write a history entry."""
    move = transition(pair[0], pair[1], config)
    assert move.moved == (move.reason != UNCHANGED)
    if not move.moved:
        assert not move.effects


@pytest.mark.parametrize("pair", PAIRS, ids=label)
@pytest.mark.parametrize("on_spike", ["notify", "trip"])
def test_no_ladder_no_transitions(pair, on_spike):
    """Only ``on_spike="limit"`` has a ladder; the rest never move."""
    config = ladder_config(on_spike=on_spike)
    assert transition(pair[0], pair[1], config) == Transition(
        pair[0], UNCHANGED, frozenset()
    )


@pytest.mark.parametrize("pair", PAIRS, ids=label)
def test_transition_is_pure(pair, config):
    """Same arguments, same answer — and nothing of the caller's touched."""
    before = dict(vars(config))
    first = transition(pair[0], pair[1], config)
    second = transition(pair[0], pair[1], config)
    assert first == second and first is not None
    assert vars(config) == before


def test_a_transition_is_frozen_data(config):
    """Callers pass it around; nobody gets to rewrite the decision."""
    move = transition(LEVEL_LIMITED, GONE, config)
    with pytest.raises(dataclasses.FrozenInstanceError):
        move.next_level = LEVEL_QUIET  # type: ignore[misc]
    same = Transition(LEVEL_CLOSED, "allowance_spent", frozenset({CLOSE}))
    assert hash(move) == hash(same)


def test_an_unknown_level_is_left_exactly_where_it_is(config):
    """Totality is not just the five rungs: a bad number changes nothing."""
    for level in (-1, 7, 99):
        for seen in (NORMAL, ABNORMAL, CONFIRMED, GONE):
            assert transition(level, seen, config) == Transition(
                level, UNCHANGED, frozenset()
            )


def test_every_effect_the_enum_names_is_reachable():
    """A named effect nobody can trigger would be a lie in the interface."""
    used = {
        effect for _level, _reason, effects in LADDER.values() for effect in effects
    }
    assert used == set(Effect)


def test_every_reason_is_one_the_session_history_documents():
    """``record_ladder_transition`` names these; the machine invents none."""
    known = {
        "first_abnormal",
        "confirmed",
        "healed",
        "allowance_spent",
        "rollover",
        "blocked",
        UNCHANGED,
    }
    assert {reason for _level, reason, _effects in LADDER.values()} <= known


def test_the_climb_a_spiking_session_actually_walks(config):
    """The reachable path, rung by rung, as the detector and api walk it."""
    watching = transition(LEVEL_QUIET, ABNORMAL, config)
    assert watching.next_level == LEVEL_WATCHING and not watching.effects

    limited = transition(watching.next_level, CONFIRMED, config)
    assert limited.next_level == LEVEL_LIMITED and limited.effects == {SET}

    spent = transition(limited.next_level, ABNORMAL, config)
    assert spent.next_level == LEVEL_LIMITED and spent.effects == {SPEND}

    closed = transition(spent.next_level, GONE, config)
    assert closed.next_level == LEVEL_CLOSED and closed.effects == {CLOSE}

    rolled = transition(closed.next_level, ENTRY, config)
    assert rolled.next_level == LEVEL_QUIET and rolled.effects == {ROLLOVER}


def test_a_session_that_behaves_again_walks_back_down(config):
    """Healing is a rung down, not a reset: the notice stands at level 1."""
    healed = transition(LEVEL_LIMITED, NORMAL, config)
    assert healed == Transition(LEVEL_WATCHING, "healed", frozenset({HEAL}))
    assert transition(healed.next_level, CONFIRMED, config).effects == {SET}


# --- the two classifiers -----------------------------------------------------


@pytest.mark.parametrize(
    ("abnormal", "recent", "noticed", "expected"),
    [
        (False, 0, False, NORMAL),
        (False, 1, False, NORMAL),  # one short of spike_confirm
        (False, 2, False, NORMAL_SPIKING),
        (False, 2, True, NORMAL_SPIKING),
        (True, 0, False, ABNORMAL),
        (True, 1, False, ABNORMAL),
        (True, 0, True, ABNORMAL_AGAIN),
        (True, 1, True, ABNORMAL_AGAIN),
        (True, 2, False, CONFIRMED),
        (True, 2, True, CONFIRMED),
        (True, 3, True, CONFIRMED),
    ],
)
def test_a_call_is_named_by_what_the_window_says(abnormal, recent, noticed, expected):
    """``spike_confirm`` of the trailing calls confirm; fewer only warn."""
    config = ladder_config(spike_confirm=2)
    assert (
        observation(
            abnormal=abnormal,
            abnormal_recent=recent,
            noticed=noticed,
            config=config,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("strikes", "expected"), [(0, ENTRY), (1, ENTRY), (2, LAST_ENTRY), (3, LAST_ENTRY)]
)
def test_the_last_strike_is_what_separates_a_cooldown_from_a_block(strikes, expected):
    """At ``spike_max_strikes`` — not one before — the key is blocked."""
    config = ladder_config(spike_max_strikes=2)
    assert entry_observation(strikes, config) is expected


def test_one_strike_is_a_legal_ladder_and_blocks_on_the_first_rollover():
    """``spike_max_strikes=1``: there is no cooldown to serve, only a block."""
    config = ladder_config(spike_max_strikes=1)
    assert entry_observation(1, config) is LAST_ENTRY
    assert transition(LEVEL_CLOSED, LAST_ENTRY, config).effects == {ROLLOVER, BLOCK}
