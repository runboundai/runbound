"""The two doors a call passes before anything else: a fleet halt, and a plane that has gone away.

Both are asked of ``engine.shared`` (what the plane said) and both refuse through ``engine.refuse``.
"""

from __future__ import annotations

import logging
from typing import Any

from . import admission
from .events import Anomaly
from .hooks import _warn_once

_LOG = logging.getLogger("runbound")


def halt(engine: Any, session: Any, tool: str) -> None:
    """Enforce a fleet halt at the tool boundary: every tool is refused, whatever ``on_halt`` says (the enforcement floor, F1).

    ``on_halt`` no longer changes this: the refusal is the same under ``"raise"`` and ``"warn"``.
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
    anomaly = Anomaly("halt", "critical", "Fleet halted by the control plane", {
        "service": config.service, "worker_id": config.resolved_worker_id(),
        "session_id": getattr(session, "session_id", ""), "tool": tool, "on_halt": config.on_halt, "halt_mode": mode,
    })
    engine.refuse(session, anomaly, admission.halted(True, f"halted by the control plane (mode: {mode})"), reacted="door")


def plane(engine: Any, session: Any, call: Any, effects: "frozenset | None") -> None:
    """Fail closed when the plane is gone (see ``RemoteState.plane_refusal``), and before a first handshake or sealed-cache load.

    The second is the floor (F3, register C14): until the first hello is answered or a sealed cache is loaded, no governed
    call runs, because the worker does not yet know what the plane would have said. The classes that already fail closed
    (financial, destructive, privileged, unclassified) keep their own ``plane_unavailable`` refusal below.
    """
    from .hooks import fails_closed

    halted = getattr(engine.shared, "halted_reasons", lambda _tool: None)(call.name)
    if halted is not None:
        # The plane halted this tool because this worker cannot enforce a restriction it was served and has no restrictive fallback
        # (halt_tools_v1): exactly this tool is refused, the others run. It holds until the plane lifts it.
        from .engine import _anomaly

        why = "the control plane halted this tool: " + (", ".join(halted) or "this worker cannot enforce a restriction on it")
        anomaly = _anomaly(session, "plane", "critical", f"Refused tool {call.name!r}{{whose}}: {why}",
                           {"tool": call.name, "rule": "policy_halted"})
        engine.refuse(session, anomaly, admission.plane_unavailable(why, "policy_halted"), reacted="door")
    if not fails_closed(effects) and getattr(engine.shared, "awaiting_first_policy", lambda: False)():
        from .engine import _anomaly

        why = "the control plane has not answered yet and there is no sealed policy cache"
        anomaly = _anomaly(session, "plane", "warn", f"Refused tool {call.name!r}{{whose}}: {why}",
                           {"tool": call.name, "rule": "policy_unavailable"})
        engine.refuse(session, anomaly, admission.plane_unavailable(why, "policy_unavailable"), reacted="door")
    state = getattr(engine.shared, "policy_state", lambda: "fresh")()
    held = getattr(engine.shared, "row_for", lambda _tool: None)(call.name) if state == "unavailable" else None
    if held is not None and not held.dry_run and (held.blocked or held.approval_required):
        # K7: the last sealed policy, though expired, bounds fail-open. A tool it denied, or needed a human for, is never let
        # through on the strength of fail mode.
        from .engine import _anomaly

        why = "the policy this worker holds has expired and it restricted this tool"
        anomaly = _anomaly(session, "plane", "warn", f"Refused tool {call.name!r}{{whose}}: {why}",
                           {"tool": call.name, "rule": "policy_expired_restricted"})
        engine.refuse(session, anomaly, admission.plane_unavailable(why, "policy_expired_restricted"), reacted="door")
    why = getattr(engine.shared, "plane_refusal", lambda _effects: None)(effects)
    if why:
        from .engine import _anomaly

        anomaly = _anomaly(session, "plane", "warn", f"Refused tool {call.name!r}{{whose}}: {why}",
                           {"tool": call.name, "rule": "plane_unavailable"})
        engine.refuse(session, anomaly, admission.plane_unavailable(why), reacted="door")
