"""User-facing configuration and its validation.

Configuration is validated once, loudly, at ``runbound.init()`` time — a
misconfigured SDK should fail at startup, never mid-run inside a hot path.
"""

import logging
import os
import secrets
import socket
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field

from .capabilities import validate_class_rules
from .policy import ToolPolicy, coerce

_LOG = logging.getLogger("runbound")

#: The hosted control plane's URL, once there is one. ``None`` for now,
#: because there is no hosted plane yet — and a placeholder is worse than
#: nothing: a bare ``token`` with no ``control_plane_url`` logs one WARNING
#: and the process stays local. The day hosting goes live this one constant
#: changes and ``init(token=...)`` starts connecting on its own.
HOSTED_PLANE_URL = None

#: What ``on_halt`` may be. It no longer changes anything: a fleet halt refuses every tool whichever it is
#: (the enforcement floor), so the two values behave the same and the setting is kept only so existing code still runs.
ON_HALT_MODES = ("raise", "warn")

_WARNED: set = set()


def _warn_once(name: str, message: str) -> None:
    """Say ``message`` once per process: a deployment fact does not change per init()."""
    if name in _WARNED:
        return
    _WARNED.add(name)
    _LOG.warning(message)


@dataclass
class RunboundConfig:
    """Everything ``runbound.init()`` accepts.

    Connection: ``token`` (from the dashboard; ``RUNBOUND_TOKEN`` is read when
    unset), ``control_plane_url`` (a self-hosted plane; ``RUNBOUND_PLANE_URL``
    is read when unset), ``service`` (what the plane files this worker under),
    ``worker_id`` (default ``host:pid:suffix``), ``control_plane_poll_s`` (the
    heartbeat interval; the plane may override it with ``HelloReply.poll_s``),
    ``export_events`` and ``send_session_keys`` (telemetry, keys hashed unless
    opted in) and ``on_halt`` (``"raise"`` | ``"warn"``).

    Rules: ``tool_policy`` (a :class:`~runbound.policy.ToolPolicy` or its dict
    form), ``capabilities`` (class -> ``"allow"`` | ``"approve"`` | ``"deny"``),
    ``require_rules`` (every ``@runbound.tool`` must state a rule),
    ``max_actions_per_run`` (a cap on executed tool calls per session),
    ``pre_hooks`` and ``gate_hooks`` (hooks run for every tool, ahead of a tool's
    own; rewrite-capable before the checks, refuse-only after them; see
    :mod:`runbound.hooks`) ``post_hooks`` (run on every tool's result after redact, truncate and scan),
    ``redact_keys`` (more keys to redact, on top of the 37 defaults), ``result_limits``
    (``{"max_chars", "max_str", "max_items", "max_depth"}``: how far a result is
    truncated) ``consequential_grace_s`` (15 to 60, default 60: how long financial / destructive / privileged and
    unclassified tools keep running on the cache after the plane last answered) and ``cache_dir`` (where the last policy the plane served is kept so a restart enforces it before the first
    heartbeat; ``RUNBOUND_CACHE_DIR``, else the platform cache dir, else the temp dir) and ``approval_secret`` (the per-service secret that seals deferred approvals; ``RUNBOUND_APPROVAL_SECRET``
    when unset; never sent to the plane), ``on_deferred_result`` (called with the outcome of each deferred
    approval this worker executes), ``offline_execution_s`` (60 to 86400: how long consequential tools keep running on the last policy confirmation while the plane is away; it may only SHORTEN the window the plane serves), ``approval_bind`` (default False: ask the plane for approval requests that can be BOUND to one execution attempt, so a replay cannot run the body twice; a plane that does not know it files a legacy request), ``provide`` (``{type or parameter name: zero-argument callable}``: what supplies a tool parameter marked ``runbound.Injected`` when an approved deferred request runs) and ``otel_endpoint`` (an OTLP/HTTP collector base URL: spans are posted there as OTLP/JSON when no
    OpenTelemetry provider is configured, with ``token`` as the Bearer key) and ``hook_timeout_s`` (a user hook still running after
    this many seconds counts as raising; ``None`` waits forever).

    Local: ``on_event`` (called with every local event record) and
    ``max_sessions`` (keyed-session registry capacity, least recently used out).
    """

    token: str | None = field(default=None, repr=False)  # secret
    control_plane_url: str | None = None
    service: str = "default"
    worker_id: str | None = None
    control_plane_poll_s: float = 5.0
    export_events: bool = True
    send_session_keys: bool = False
    on_halt: str = "raise"
    tool_policy: ToolPolicy | dict | None = None
    capabilities: dict | None = None
    require_rules: bool = False
    max_actions_per_run: int | None = None
    pre_hooks: tuple | list = ()
    gate_hooks: tuple | list = ()
    hook_timeout_s: float | None = 5.0
    post_hooks: tuple | list = ()
    redact_keys: tuple | list = ()
    result_limits: dict | None = None
    otel_endpoint: str | None = None
    cache_dir: str | None = None
    consequential_grace_s: float = 60.0
    approval_secret: str | None = field(default=None, repr=False)  # secret
    on_deferred_result: Callable[[dict], None] | None = None
    provide: dict | None = None
    offline_execution_s: int | None = None
    approval_bind: bool = False
    on_event: Callable[[dict], None] | None = None
    max_sessions: int = 10_000
    _plane_mode: str | None = field(default=None, init=False, repr=False)

    def resolved_worker_id(self) -> str:
        """This process's name inside the fleet.

        ``worker_id`` when set, otherwise ``"<hostname>:<pid>:<6 hex chars>"``,
        computed once and cached so the id is stable for the process's life.
        Fail-open: an unreadable hostname is ``"unknown"``, no randomness is
        ``"000000"``.
        """
        if self.worker_id:
            return self.worker_id
        cached = getattr(self, "_resolved_worker_id", None)
        if cached is not None:
            return cached
        try:
            host = socket.gethostname() or "unknown"
        except Exception:
            host = "unknown"
        try:
            suffix = secrets.token_hex(3)
        except Exception:
            suffix = "000000"
        resolved = f"{host}:{os.getpid()}:{suffix}"
        self._resolved_worker_id = resolved
        return resolved

    def validate(self) -> None:
        """Raise ``ValueError`` if this configuration cannot be honored."""
        self._normalize_connection()
        if self.control_plane_url is not None and self.token is None:
            raise ValueError(
                "a self-hosted plane needs a token: control_plane_url is set "
                'but token is not (use token="" if that plane has no auth). '
                "A token on its own, with no url, is the hosted plane."
            )
        if not isinstance(self.control_plane_poll_s, (int, float)) or self.control_plane_poll_s <= 0:
            raise ValueError(
                f"control_plane_poll_s must be positive, got {self.control_plane_poll_s!r}"
            )
        if self.on_halt not in ON_HALT_MODES:
            raise ValueError(f"on_halt must be one of {ON_HALT_MODES}, got {self.on_halt!r}")
        if not isinstance(self.max_sessions, int) or self.max_sessions < 1:
            raise ValueError(f"max_sessions must be an int >= 1, got {self.max_sessions!r}")
        for name in ("max_actions_per_run",):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be an int >= 1 or None, got {value!r}")
        for name in ("export_events", "send_session_keys", "require_rules"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool, got {getattr(self, name)!r}")
        for name in ("pre_hooks", "gate_hooks", "post_hooks"):
            hooks = tuple(getattr(self, name) or ())
            for hook in hooks:
                if not callable(hook):
                    raise ValueError(f"{name} must be callables taking a ToolCall, got {hook!r}")
            setattr(self, name, hooks)
        self.redact_keys = tuple(self.redact_keys or ())
        if not all(isinstance(key, str) and key for key in self.redact_keys):
            raise ValueError(f"redact_keys must be non-empty strings, got {self.redact_keys!r}")
        if self.result_limits is not None:
            allowed = {"max_chars", "max_str", "max_items", "max_depth"}
            limits = self.result_limits
            if not isinstance(limits, dict) or set(limits) - allowed or not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in limits.values()
            ):
                raise ValueError(f"result_limits must be a dict of {sorted(allowed)} to ints >= 1, got {limits!r}")
        if self.otel_endpoint is not None and not (
            isinstance(self.otel_endpoint, str) and self.otel_endpoint.startswith(("http://", "https://"))
        ):
            raise ValueError(f"otel_endpoint must be an http(s) URL, got {self.otel_endpoint!r}")
        grace = self.consequential_grace_s
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 15 <= grace <= 60:
            raise ValueError(f"consequential_grace_s must be between 15 and 60 (it may not outlast a halt), got {grace!r}")
        if self.cache_dir is not None and not (isinstance(self.cache_dir, str) and self.cache_dir):
            raise ValueError(f"cache_dir must be a non-empty path, got {self.cache_dir!r}")
        if self.approval_secret is None:
            env_secret = os.environ.get("RUNBOUND_APPROVAL_SECRET")
            if env_secret and env_secret.strip():
                self.approval_secret = env_secret
        if self.approval_secret is not None and not (isinstance(self.approval_secret, str) and self.approval_secret):
            raise ValueError("approval_secret must be a non-empty string")
        if self.on_deferred_result is not None and not callable(self.on_deferred_result):
            raise ValueError(f"on_deferred_result must be callable, got {self.on_deferred_result!r}")
        if self.provide is not None:
            if not isinstance(self.provide, dict) or not all(isinstance(key, (str, type)) and callable(value) for key, value in self.provide.items()):
                raise ValueError("provide must be a dict of a parameter name or a type to a zero-argument callable that supplies it")
        if not isinstance(self.approval_bind, bool):
            raise ValueError(f"approval_bind must be True or False, got {self.approval_bind!r}")
        window = self.offline_execution_s
        if window is not None and (isinstance(window, bool) or not isinstance(window, int) or not 60 <= window <= 86400):
            raise ValueError(f"offline_execution_s must be an integer between 60 and 86400 (it may only shorten the window the plane serves), got {window!r}")
        timeout = self.hook_timeout_s
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ValueError(f"hook_timeout_s must be positive or None, got {timeout!r}")
        if self.on_event is not None and not callable(self.on_event):
            raise ValueError(f"on_event must be callable, got {self.on_event!r}")
        validate_class_rules(self.capabilities)
        self.tool_policy = coerce(self.tool_policy)
        if self.tool_policy is not None:
            self.tool_policy.validate()

    @property
    def plane_mode(self) -> str:
        """``"off"`` | ``"hosted"`` | ``"self_hosted"``: whether there is a plane.

        :meth:`validate` settles it. An unvalidated configuration derives the
        answer from the url the same way: blank is not a url.
        """
        if self._plane_mode is not None:
            return self._plane_mode
        return "self_hosted" if (self.control_plane_url or "").strip() else "off"

    def _normalize_connection(self) -> None:
        """Work out how — and whether — this process connects to a plane.

        ``token`` falls back to ``RUNBOUND_TOKEN`` and ``control_plane_url`` to
        ``RUNBOUND_PLANE_URL``; a blank value from either source is unset. A
        url with a token is **self_hosted**. A url from the environment with no
        token cannot raise (a base image's ENV must not take down a service
        that was never issued a token): it warns once and the process stays
        local. A bare token is **hosted**, which today means
        :data:`HOSTED_PLANE_URL` is ``None``: one WARNING, and the process
        stays local. ``control_plane_url=""`` pins a process local whatever
        the shell exports.
        """
        if self.token is None:
            env_token = os.environ.get("RUNBOUND_TOKEN")
            if env_token is not None and env_token.strip():
                self.token = env_token
        from_env = False
        if self.control_plane_url is None:
            self.control_plane_url = os.environ.get("RUNBOUND_PLANE_URL")
            from_env = self.control_plane_url is not None
        if self.control_plane_url is not None and not self.control_plane_url.strip():
            self.control_plane_url = None
            from_env = False
        if self.control_plane_url is not None:
            if self.token is None and from_env:
                _warn_once(
                    "env-url-no-token",
                    "runbound: RUNBOUND_PLANE_URL is set but there is no token "
                    "(RUNBOUND_TOKEN or init(token=...)); guarding locally.",
                )
                self.control_plane_url = None
            else:
                self._plane_mode = "self_hosted"
        elif self.token:
            self.control_plane_url = HOSTED_PLANE_URL
            if self.control_plane_url is None:
                _warn_once(
                    "token-no-plane",
                    "runbound: token is set but there is nowhere to send it yet "
                    "(no hosted plane); set control_plane_url for a self-hosted "
                    "plane. Rules and refusals are unaffected.",
                )
            else:
                self._plane_mode = "hosted"


#: The old name warns on its first use in a process, not on every access.
_ALIAS_WARNED = False


def deprecated_alias(name: str, module: str, stacklevel: int = 3) -> type:
    """``GuardrailConfig``, the former name of :class:`RunboundConfig`: the same class, one ``DeprecationWarning``."""
    global _ALIAS_WARNED
    if name != "GuardrailConfig":
        raise AttributeError(f"module {module!r} has no attribute {name!r}")
    if not _ALIAS_WARNED:
        _ALIAS_WARNED = True
        warnings.warn(
            "GuardrailConfig is renamed RunboundConfig; the old name will be removed in a future release",
            DeprecationWarning,
            stacklevel=stacklevel,
        )
    return RunboundConfig


def __getattr__(name: str) -> type:
    """PEP 562: ``runbound.config.GuardrailConfig`` still imports, with the warning."""
    return deprecated_alias(name, __name__)
