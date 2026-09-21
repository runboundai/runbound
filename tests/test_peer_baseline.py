"""Restored and peer baselines: judging a key from its first call.

On its own, the spike detector only ever knows a session's own history:
a brand-new key got a free warmup (``spike_warmup_calls`` unmeasured calls)
before anything could be called abnormal, and a worker that restarted lost
whatever it had learned about a key mid-spike.

These tests drive the detector directly, the way ``test_ladder_baseline.py``
does, with ``state.spike_baseline``/``state.spike_baseline_source``/
``state.service_baseline`` set by hand -- exactly what :mod:`runbound.api`
seeds them with at session entry (covered separately in
``test_plane_baseline_restore.py``). Nothing here measures real time.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.detectors import SpikeDetector
from runbound.events import Anomaly, Event
from runbound.state import SessionState
from spike_test_helpers import spiking_config

NORMAL_SECONDS = 2.0
NORMAL_TOKENS = 100.0
SPIKE_SECONDS = 40.0  # 20x normal
KEY = "user:new"


class Run:
    def __init__(self, config: GuardrailConfig, key: str | None = KEY) -> None:
        self.config = config
        self.detector = SpikeDetector()
        self.state = SessionState("s1", key=key)
        self.step = 0

    def call(self, duration: float = NORMAL_SECONDS, tokens_out: float = NORMAL_TOKENS):
        self.step += 1
        event = Event(
            kind="llm_call",
            ts=float(self.step),
            step=self.step,
            tokens_out=tokens_out,
            duration_s=duration,
            model="gpt-4o",
        )
        self.state.record(event)
        return self.detector.check(self.state, event, self.config)


def restored(run: Run, baseline=(NORMAL_SECONDS, NORMAL_TOKENS)) -> None:
    run.state.spike_baseline = baseline
    run.state.spike_baseline_source = "restored"


def peer(run: Run, baseline=(NORMAL_SECONDS, NORMAL_TOKENS)) -> None:
    run.state.service_baseline = baseline


# --- a restored per-key baseline arms detection immediately ------------------


def test_a_restored_baseline_catches_the_very_first_call():
    run = Run(spiking_config())
    restored(run)

    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert anomaly.details["baseline_source"] == "restored"
    assert anomaly.details["median"] == pytest.approx(NORMAL_SECONDS)


def test_a_plain_new_session_with_nothing_restored_still_needs_warmup():
    run = Run(spiking_config())  # no restored, no peer -- the old behavior

    assert run.call(SPIKE_SECONDS) is None


# --- a brand-new key is judged against the service median from call #1 -----


def test_a_new_key_with_no_history_is_judged_against_the_service_median():
    run = Run(spiking_config())
    peer(run)

    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert anomaly.details["baseline_source"] == "peer"
    assert anomaly.details["median"] == pytest.approx(NORMAL_SECONDS)


def test_restored_takes_priority_over_peer_when_both_are_known():
    run = Run(spiking_config())
    restored(run, baseline=(NORMAL_SECONDS, NORMAL_TOKENS))
    run.state.service_baseline = (1000.0, 1000.0)  # would never catch it

    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert anomaly.details["baseline_source"] == "restored"


# --- vs_service rides alongside whatever baseline was actually used --------


def test_vs_service_is_reported_alongside_a_local_baseline():
    run = Run(spiking_config(spike_warmup_calls=2))
    peer(run, baseline=(4.0, 400.0))  # a looser service normal
    for _ in range(2):
        assert run.call() is None  # warms the session's own (tighter) baseline

    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert anomaly.details["baseline_source"] == "local"
    assert anomaly.details["median"] == pytest.approx(NORMAL_SECONDS)
    assert anomaly.details["vs_service"] == pytest.approx(SPIKE_SECONDS / 4.0)


def test_the_message_names_both_ratios_when_a_service_baseline_is_known():
    """The dashboard card's own text ("N x its own normal, M x this
    service's") is exactly the anomaly's message -- no separate lookup."""
    run = Run(spiking_config(spike_warmup_calls=2))
    peer(run, baseline=(4.0, 400.0))
    for _ in range(2):
        assert run.call() is None

    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert "its own normal" in anomaly.message
    assert "this service's" in anomaly.message
    assert f"{SPIKE_SECONDS / NORMAL_SECONDS:.1f}x its own normal" in anomaly.message
    assert f"{SPIKE_SECONDS / 4.0:.1f}x this service's" in anomaly.message


def test_the_message_has_no_ratio_suffix_without_a_service_baseline():
    run = Run(spiking_config())
    restored(run)

    anomaly = run.call(SPIKE_SECONDS)

    assert "this service's" not in anomaly.message


def test_vs_service_is_absent_without_a_service_baseline():
    run = Run(spiking_config())
    restored(run)

    anomaly = run.call(SPIKE_SECONDS)

    assert "vs_service" not in anomaly.details


# --- a sustained train on a restored/peer baseline stays held --------------


def test_a_sustained_spike_train_on_a_restored_baseline_does_not_drift():
    """The whole point of a restored/peer baseline: a key that never warmed up locally
    must not have its own spike train become its "normal" either."""
    config = spiking_config(spike_confirm=5)  # only confirms on five straight
    run = Run(config)
    restored(run)

    seen = [run.call(SPIKE_SECONDS) for _ in range(6)]

    for anomaly in seen:
        assert anomaly is None or anomaly.details["median"] == pytest.approx(NORMAL_SECONDS)
    assert run.state.spike_baseline == pytest.approx((NORMAL_SECONDS, NORMAL_TOKENS))


def test_a_sustained_spike_train_on_a_peer_baseline_does_not_drift():
    config = spiking_config(spike_confirm=5)
    run = Run(config)
    peer(run)

    seen = [run.call(SPIKE_SECONDS) for _ in range(6)]

    for anomaly in seen:
        assert anomaly is None or anomaly.details["median"] == pytest.approx(NORMAL_SECONDS)
    assert run.state.spike_baseline == pytest.approx((NORMAL_SECONDS, NORMAL_TOKENS))


# --- once local history warms up, it takes over from the external one ------


def test_local_warmup_eventually_supersedes_a_restored_baseline():
    config = spiking_config(spike_warmup_calls=3)
    run = Run(config)
    restored(run, baseline=(1.0, 1.0))  # a stale, tighter restored baseline

    for _ in range(4):  # warmup_calls of *history*, so one more call than that
        assert run.call(duration=5.0) is None  # this session's real normal is 5s

    assert run.state.spike_baseline_source == "local"
    assert run.state.spike_baseline == pytest.approx((5.0, NORMAL_TOKENS))


# --- trap #3: a peer comparison alone must not close a new user's first call


def test_on_spike_limit_a_new_keys_single_long_first_call_only_watches():
    """Even under on_spike="limit", one abnormal call -- however it was
    judged -- is a notice (level 1), never a restriction (level 2): the
    ladder still needs spike_confirm of the trailing window before it
    narrows anything. A peer/restored baseline must not remove that."""
    config = spiking_config(on_spike="limit", on_trip="latch")
    run = Run(config, key="user:brand-new")
    peer(run)

    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert anomaly.details["level"] == 1  # watching, not limited
    assert run.state.spike_level == 1
    assert run.state.posture is None  # nothing was restricted


def test_on_spike_limit_confirming_twice_on_a_peer_baseline_does_limit():
    config = spiking_config(on_spike="limit", on_trip="latch", spike_confirm=2)
    run = Run(config, key="user:brand-new-2")
    peer(run)

    run.call(SPIKE_SECONDS)
    anomaly = run.call(SPIKE_SECONDS)

    assert anomaly is not None
    assert anomaly.details["level"] == 2
    assert run.state.posture is not None
    assert run.state.posture.name == "restricted"
