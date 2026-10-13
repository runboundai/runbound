"""Mutable per-session state: the call counters and the run/session id."""

import threading
import time

from .events import Anomaly, Event


class SessionState:
    """Counters for one agent run.

    Agents run tools from threads, so every mutation happens under
    ``self.lock`` (reentrant, so a caller may hold it across a ``record()``
    when it needs a consistent multi-attribute read).

    ``key`` and ``tags`` identify a keyed session (one run, one job, one
    caller) and are ``None``/empty for the default session that guards a whole
    process. ``session_id`` is the run id traces carry.

    ``tool_calls`` counts *attempts* per tool name; a refused attempt is counted,
    because it was made. ``max_calls`` is measured against :meth:`counted_calls`
    instead: calls that cleared the chain, so a refused attempt never uses it up.

    ``admitted_actions``, ``executed_actions`` and ``refused_actions`` are the
    outcome of an attempt once admission and the policy have both had their
    say. ``max_actions_per_run`` is measured against ``executed_actions``: a
    refused attempt must not itself count as having run.

    ``tripped_by`` is the anomaly that latched this session
    (``on_violation="block_and_latch"``); while set, every tool is refused.
    """

    def __init__(
        self,
        session_id: str,
        key: str | None = None,
        tags: dict | None = None,
    ) -> None:
        self.session_id = session_id
        self.key = key
        self.tags: dict = dict(tags) if tags else {}
        self.event_count = 0
        self.requested_actions = 0
        self.admitted_actions = 0
        self.executed_actions = 0
        self.refused_actions = 0
        self.tool_calls: dict[str, int] = {}
        self._cleared: dict[str, int] = {}  # per tool: calls that cleared the whole chain (what max_calls counts)
        self._in_chain: dict[str, int] = {}  # per tool: calls inside the chain now, this one included
        self._repeats: dict[tuple, int] = {}  # (tool, argument digest) -> calls counted by a repetition control
        self._key_cleared: dict[tuple, int] = {}  # (tool, matched lines) -> calls that cleared the chain: what a conditional line's cap counts
        self._key_in_chain: dict[tuple, int] = {}
        self.total_errors = 0
        self.started_at = time.monotonic()
        self.tripped_by: Anomaly | None = None
        self.tripped_at: float | None = None
        self.lock = threading.RLock()
        self._steps = 0

    def mark_action_admitted(self) -> None:
        """Count one tool attempt that cleared admission and the policy."""
        with self.lock:
            self.admitted_actions += 1
            self.executed_actions += 1

    def enter_chain(self, tool: str) -> None:
        """A call starts the pre-hook chain: it holds one of ``tool``'s ``max_calls`` units until it leaves."""
        with self.lock:
            self._in_chain[tool] = self._in_chain.get(tool, 0) + 1

    def leave_chain(self, tool: str, cleared: bool) -> None:
        """The call left the chain; a refused or deferred one gives its unit back, a cleared one keeps it."""
        with self.lock:
            self._in_chain[tool] = max(0, self._in_chain.get(tool, 0) - 1)
            if cleared:
                self._cleared[tool] = self._cleared.get(tool, 0) + 1

    def counted_calls(self, tool: str) -> int:
        """What ``max_calls`` is measured against: calls that cleared the chain plus those inside it now.

        A call being judged counts itself; a call an earlier stage refused does not count at all.
        """
        with self.lock:
            return self._cleared.get(tool, 0) + self._in_chain.get(tool, 0)

    def hold_key(self, tool: str, key: str) -> None:
        """A call whose matching lines are ``key`` holds one unit of those lines' caps until it leaves the chain."""
        with self.lock:
            self._key_in_chain[(tool, key)] = self._key_in_chain.get((tool, key), 0) + 1

    def leave_key(self, tool: str, key: str, cleared: bool) -> None:
        with self.lock:
            k = (tool, key)
            self._key_in_chain[k] = max(0, self._key_in_chain.get(k, 0) - 1)
            if cleared:
                self._key_cleared[k] = self._key_cleared.get(k, 0) + 1

    def counted_key(self, tool: str, key: str) -> int:
        """Calls of ``tool`` that matched exactly these lines and cleared the chain, plus those inside it now (this one included)."""
        with self.lock:
            return self._key_cleared.get((tool, key), 0) + self._key_in_chain.get((tool, key), 0)

    def reserve_repeat(self, tool: str, digest: str, limit: int) -> bool:
        """Count one more call of ``tool`` with this argument digest, unless the session already made ``limit`` of them. Atomic: strict in one process."""
        with self.lock:
            key = (tool, digest)
            if self._repeats.get(key, 0) >= limit:
                return False
            self._repeats[key] = self._repeats.get(key, 0) + 1
            return True

    def repeated(self, tool: str, digest: str) -> int:
        """How many calls of ``tool`` with this argument digest the session has counted (no change)."""
        with self.lock:
            return self._repeats.get((tool, digest), 0)

    def release_repeat(self, tool: str, digest: str) -> None:
        """Give one back: the call that reserved it was refused later in the chain or is only pending."""
        with self.lock:
            key = (tool, digest)
            if self._repeats.get(key, 0) > 0:
                self._repeats[key] -= 1

    def mark_action_refused(self) -> None:
        """Count one tool attempt that admission or the policy refused."""
        with self.lock:
            self.refused_actions += 1

    def next_step(self) -> int:
        """Allocate this session's next 1-based step number, atomically."""
        with self.lock:
            self._steps += 1
            return self._steps

    @property
    def current_step(self) -> int:
        """The step number last allocated: a ``tool_result`` shares its ``tool_call``'s."""
        return self._steps

    def record(self, event: Event) -> None:
        """Fold one event into the counters, atomically.

        ``event_count`` tracks the highest step seen, so several events sharing
        one step do not inflate it. Only named ``tool_call`` events raise that
        tool's entry in ``tool_calls``; ``tool_request`` events (a model asking
        for a tool) raise ``requested_actions``; ``tool_error`` raises
        ``total_errors``.
        """
        with self.lock:
            self.event_count = max(self.event_count, event.step)
            if event.kind == "tool_error":
                self.total_errors += 1
            if event.kind == "tool_call" and event.tool_name:
                self.tool_calls[event.tool_name] = self.tool_calls.get(event.tool_name, 0) + 1
            if event.kind == "tool_request":
                self.requested_actions += 1
