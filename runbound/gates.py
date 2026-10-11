"""The two doors a call passes before anything else: a fleet halt, and a plane that has gone away.

Both are asked of ``engine.shared`` (what the plane said) and both refuse through ``engine.refuse``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import admission
from .events import Anomaly
from .hooks import _warn_once

_LOG = logging.getLogger("runbound")

#: How long between the "fleet halted" warnings under ``on_halt="warn"``.
_HALT_WARN_INTERVAL_S = 60.0

_WARN_LOCK = threading.Lock()
_HALT_WARNED_AT: dict = {}  # engine id -> monotonic instant of the last warning


def halt(engine: Any, session: Any, tool: str) -> None:
    """Enforce a fleet halt: ``on_halt="raise"`` refuses every tool; ``"warn"`` says so once a minute.

    A halt of mode ``"narrow"`` is treated as a full halt (narrowing left the product), logged once.
    """
    shared = engine.shared
    try:
        mode = shared.halt_mode() if shared.halted() else None
    except Exception:
        _LOG.debug("runbound could not read the fleet halt", exc_info=True)
        return
    if mode is None:
        return
    if mode == "narrow":
        _warn_once(("halt-narrow",), "runbound: the control plane sent a halt of mode 'narrow', which is no "
                   "longer supported; treating it as a full halt")
    config = engine.config
    if config.on_halt != "raise":
        now = time.monotonic()
        with _WARN_LOCK:
            last = _HALT_WARNED_AT.get(id(engine))
            if last is not None and now - last < _HALT_WARN_INTERVAL_S:
                return
            _HALT_WARNED_AT[id(engine)] = now
        _LOG.warning("[runbound] Fleet halted by the control plane (mode: %s; on_halt='warn': tools keep running)", mode)
        return
    anomaly = Anomaly("halt", "critical", "Fleet halted by the control plane", {
        "service": config.service, "worker_id": config.resolved_worker_id(),
        "session_id": getattr(session, "session_id", ""), "tool": tool, "on_halt": config.on_halt, "halt_mode": mode,
    })
    engine.refuse(session, anomaly, admission.halted(True, f"halted by the control plane (mode: {mode})"), reacted="door")


def plane(engine: Any, session: Any, call: Any, effects: "frozenset | None") -> None:
    """Fail closed when the plane is gone (see ``RemoteState.plane_refusal``)."""
    why = getattr(engine.shared, "plane_refusal", lambda _effects: None)(effects)
    if why:
        from .engine import _anomaly

        anomaly = _anomaly(session, "plane", "warn", f"Refused tool {call.name!r}{{whose}}: {why}",
                           {"tool": call.name, "rule": "plane_unavailable"})
        engine.refuse(session, anomaly, admission.plane_unavailable(why), reacted="door")
