"""The one exception runbound raises on purpose."""

from .events import Anomaly
from .policy import Violation
from .responses import Refusal, refusal_for


class GuardrailTripped(Exception):
    """Raised to stop a runaway agent when ``on_anomaly="raise"``.

    This is the only exception runbound lets escape into host code; every
    other internal failure is logged and swallowed. ``str(exc)`` is the
    anomaly's message and the full anomaly stays reachable as ``exc.anomaly``.
    """

    def __init__(self, anomaly: Anomaly) -> None:
        super().__init__(anomaly.message)
        self.anomaly = anomaly

    @property
    def refusal(self) -> Refusal:
        """What the app should tell the caller: the customer's own words.

        Resolved lazily, on each access, from whatever plane profile and
        local ``GuardrailConfig.refusals`` are in effect *right now* — a
        profile the customer changes on the plane reaches an exception that
        was raised (and is still being handled) moments earlier, exactly as
        it reaches the next request. See :mod:`runbound.responses`.
        """
        return refusal_for(self.anomaly)


class CircuitOpen(GuardrailTripped):
    """Raised instead of calling a provider whose circuit runbound opened.

    A subclass of :class:`GuardrailTripped`, so a host that already catches
    that keeps working unchanged — the call simply fails fast instead of
    joining the retry storm. A host that wants to fall back to another provider
    catches this one and reads ``exc.provider`` (``"openai"``,
    ``"anthropic"``): runbound says *this one is down*, and the routing
    decision stays where it belongs, with the application.

    Raised before the provider is called, and only under
    ``on_provider_failure="open"``.
    """

    def __init__(self, anomaly: Anomaly, provider: str | None = None) -> None:
        super().__init__(anomaly)
        if provider is None:
            details = anomaly.details if isinstance(anomaly.details, dict) else {}
            provider = details.get("provider")
        self.provider = provider


class PolicyViolation(GuardrailTripped):
    """Raised when a tool call breaks the customer's own action policy.

    A subclass of :class:`GuardrailTripped`, so a host that already catches
    that keeps working unchanged, while a host that wants to tell "the agent
    tried something it may not do" apart from "the agent ran away" can catch
    this instead. The rule that was broken is on ``exc.violation``; ``str(exc)``
    is still the anomaly's message.
    """

    def __init__(self, anomaly: Anomaly, violation: Violation) -> None:
        super().__init__(anomaly)
        self.violation = violation
