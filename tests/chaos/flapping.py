"""A scripted plane that misbehaves on a timeline, and a rig that drives the SDK's real link logic through it with a fake clock.

No sockets, no threads, no sleeping. ``FlappingPlane`` stands where :class:`runbound.plane.PlaneClient` stands (``hello``,
``events``, ``policy``) and answers by the phase the clock is in; the rig ticks :class:`runbound.plane.Poller` by hand (the same
``_tick`` the heartbeat thread runs), so a miss, a reply and a state transition travel exactly the code paths production does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import runbound
from runbound import api, cache
from runbound.config import RunboundConfig
from runbound.plane import Poller
from runbound.plane_types import HelloReply
from runbound.shared import RemoteState

URL = "http://plane.example"
SERVICE = "chaos"
TOKEN = "rb_live_chaos_token"


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@dataclass
class Phase:
    """``seconds`` long, answering ``kind``: ``ok`` (a reply built from ``reply``), ``503`` (the plane answers, badly: no reply
    comes back), ``slow`` (it answers after the client's timeout: the client gives up, no reply), ``refused`` (nothing listens)."""

    seconds: float
    kind: str = "ok"
    reply: dict = field(default_factory=dict)


class FlappingPlane:
    service = SERVICE
    worker_id = "w-1"
    key_state = "unknown"
    consecutive_failures = 0
    timeout_s = 3.0

    def __init__(self, clock: Clock, script: list) -> None:
        self.clock, self.script, self.started = clock, script, clock()
        self.hellos: list = []  # (elapsed, phase kind)

    def phase(self) -> Phase:
        elapsed = self.clock() - self.started
        for phase in self.script:
            if elapsed < phase.seconds:
                return phase
            elapsed -= phase.seconds
        return self.script[-1]

    def hello(self, payload: dict):
        phase = self.phase()
        self.hellos.append((round(self.clock() - self.started, 1), phase.kind))
        return HelloReply(**phase.reply) if phase.kind == "ok" else None  # every other kind is a miss to the caller

    def events(self, batch):  # pragma: no cover - the exporter is not wired here
        return False

    def policy(self, service):
        """What a healthy plane serves a service with no rows and no defaults: an empty policy. (An unreachable one fails the read, as the hello fails.)
        The first answered hello reads the policy whatever version it announced, and a worker that could not read one refuses consequential classes."""
        phase = self.phase()
        if phase.kind != "ok":
            return None
        return {"version": 0, "dry_run": False, "policy": None, "rows": [], "rows_hash": "empty", "cfg_hash": "empty"}


class Rig:
    """A RemoteState wired to a FlappingPlane, ticked by hand every ``poll_s`` of the fake clock."""

    def __init__(self, tmp_path, script: list, *, grace_s: float = 60.0, poll_s: float = 5.0, cached_deny: tuple = ()) -> None:
        api._teardown_for_tests()
        cache.reset_for_tests()
        self.clock = Clock()
        self.poll_s = poll_s
        config = RunboundConfig(control_plane_url=URL, token=TOKEN, service=SERVICE, worker_id="w-1",
                                 cache_dir=str(tmp_path), consequential_grace_s=grace_s)
        config.validate()
        if cached_deny:
            path = cache.resolve(str(tmp_path), URL, SERVICE)
            assert cache.write(path, TOKEN, cache.body_for(SERVICE, 1, {"deny": list(cached_deny)}, False))
        self.plane = FlappingPlane(self.clock, script)
        self.shared = RemoteState(self.plane, None, config, now=self.clock)
        self.poller = Poller(self.plane, poll_s, self.shared.apply_hello, self.shared._hello_payload, on_miss=self.shared.record_miss)
        # The SDK under test is the real engine; only its plane link is this rig's.
        runbound.init(consequential_grace_s=grace_s, cache_dir=str(tmp_path))
        api._ENGINE.shared = self.shared
        api._SHARED = self.shared
        self.modes: list = []

    def tick(self) -> None:
        """One heartbeat, then ``poll_s`` of fake time."""
        self.poller._tick()
        self.modes.append(self.shared.status().mode)
        self.clock.t += self.poll_s

    def run(self, seconds: float) -> None:
        for _ in range(int(seconds / self.poll_s)):
            self.tick()

    def modes_seen(self) -> list:
        """The distinct modes in the order first seen: ``["connected", "degraded", "unavailable", "connected"]``."""
        out: list = []
        for mode in self.modes:
            if not out or out[-1] != mode:
                out.append(mode)
        return out

    def close(self) -> None:
        api._teardown_for_tests()
