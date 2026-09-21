"""Shared helpers for spike/ladder test scenarios.

``spike_detection``, ``on_spike`` and the nine tuning knobs under it are
real ``GuardrailConfig`` fields — ``spiking_config``/
``apply_spike_overrides`` below build (or mutate) a config with them
directly, through the real dataclass constructor and ``validate()``, never
by hand-setting an *effective*, already-merged attribute the way a value
:meth:`~runbound.engine.Engine._effective_config` resolves would be. A test
that wants to prove the plane's own *tightening* of a locally-configured
value (the code-only / plane-only / code-tightened-by-plane triple) still
builds the wire body with :func:`spike_controls_body` and runs it through a
real :class:`~runbound.engine.Engine`, exactly as it always did — that part
is unchanged, and lives in ``tests/test_plane_only_controls.py``.
"""

from __future__ import annotations

from typing import Any

from runbound.config import GuardrailConfig

#: init()-era keyword -> the Controls "spike" bundle's own field name.
_BUNDLE_FIELD = {
    "spike_limit_calls": "limit_calls",
    "spike_cooldown_seconds": "cooldown_seconds",
    "spike_max_strikes": "max_strikes",
    "spike_warmup_calls": "warmup_calls",
    "spike_min_duration_s": "min_duration_s",
    "spike_min_output_tokens": "min_output_tokens",
    "spike_window": "window",
    "spike_factor": "factor",
    "spike_confirm": "confirm",
}

#: What every one of the nine tuning keywords defaults to locally -- kept in
#: step by hand with ``runbound.config.GuardrailConfig`` and
#: ``runbound.controls_merge._SPIKE_DEFAULTS`` (this module stays a test
#: helper, not a product import surface, so it does not import that private
#: name across a package boundary pytest may not always resolve the same
#: way two different invocation styles do).
SPIKE_DEFAULTS: dict = {
    "mode": "notify",
    "limit_calls": 5,
    "cooldown_seconds": 300.0,
    "max_strikes": 3,
    "warmup_calls": 4,
    "min_duration_s": 2.0,
    "min_output_tokens": 500,
    "window": 50,
    "factor": 10.0,
    "confirm": 2,
}


def _bundle(on_spike: str | None, **spike_kwargs: Any) -> dict:
    """The old ``init()`` keywords, translated to the wire's own names."""
    bundle = dict(SPIKE_DEFAULTS)
    if on_spike is not None:
        bundle["mode"] = on_spike
    for old_name, value in spike_kwargs.items():
        bundle[_BUNDLE_FIELD[old_name]] = value
    return bundle


def spike_controls_body(on_spike: str | None = None, **spike_kwargs: Any) -> dict:
    """``{"spike_enabled": True, "spike": {...}}`` for a fake plane's
    ``controls_body["controls"]`` — what a plane's own Controls row looks
    like on the wire when it turns spike detection on and states some (or
    all) of its tuning knobs."""
    return {"spike_enabled": True, "spike": _bundle(on_spike, **spike_kwargs)}


def spiking_config(on_spike: str | None = None, **kwargs: Any) -> GuardrailConfig:
    """A :class:`GuardrailConfig` with spike detection configured directly
    — the nine tuning keywords (``spike_confirm=``, ``spike_window=``, and
    the rest) are real fields, so this is a thin, real
    constructor call plus :meth:`~runbound.config.GuardrailConfig.validate`,
    not a merge. ``spike_detection`` defaults ``True`` already; pass it
    explicitly (``spike_detection=False``) to build a config with spike
    detection off.
    """
    if on_spike is not None:
        kwargs.setdefault("on_spike", on_spike)
    config = GuardrailConfig(**kwargs)
    config.validate()
    return config


def apply_spike_overrides(config: GuardrailConfig, on_spike: str | None = None, **spike_kwargs: Any) -> None:
    """Set spike/ladder fields directly on an already-built ``config``, then
    re-validate — the in-place counterpart of :func:`spiking_config`, for a
    test that already has a config object from elsewhere and wants to layer
    spike tuning onto it."""
    if on_spike is not None:
        config.on_spike = on_spike
    for name, value in spike_kwargs.items():
        setattr(config, name, value)
    config.validate()
