"""No page and no example teaches a name the SDK no longer has, or the product it no longer is.

A removal with no shim fails an import at once, but prose does not: a Markdown page can keep
teaching ``init(max_steps=...)`` long after it raises ``TypeError``. This scans README.md, docs/
and examples/ (CHANGELOG.md is history and is not scanned) for:

- ``REMOVED_KEYWORDS``: ``init()`` keywords removed in S1-S4 (every ``RunboundConfig`` field of
  v0.11.2 that 1.0.0a2 does not have, as ``<name>=``) and ``@runbound.tool(repeatable=)``;
- ``REMOVED_NAMES``: the public names removed in S1 (API.md "Removed in S1", plus the exceptions
  and modules that left with them), as ``runbound.<name>`` or ``from runbound import <name>``;
- ``BANNED_WORDS``: words of the old product, case-insensitive.

Two removed fields are left out on purpose because their spelling is too generic to scan for
without false positives: ``callback=`` and ``api_key=``.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

#: ``RunboundConfig`` fields of v0.11.2 (``git show v0.11.2:runbound-sdk/runbound/config.py``)
#: that 1.0.0a2 does not have. ``test_removed_keywords_are_really_gone`` checks each one.
REMOVED_CONFIG_FIELDS = (
    "admission_output_tokens", "auto_wrap", "budget_admission", "budget_soft", "budget_usd",
    "budget_window", "circuit_cooldown_seconds", "circuit_failure_rate", "circuit_failure_threshold",
    "circuit_fleet", "circuit_half_open_calls", "circuit_min_calls", "circuit_mode", "circuit_posture",
    "circuit_reads_quota", "circuit_slow_call_seconds", "circuit_slow_rate", "circuit_window_seconds",
    "control_plane_cache_s", "control_plane_timeout_s", "coverage_check_seconds", "custom_prices",
    "envelope", "error_storm_limit", "estimate_tokens", "latch_ttl_seconds", "loop_alert_threshold",
    "loop_contain_threshold", "loop_hard_threshold", "loop_ignore_tools", "loop_max_period",
    "loop_shapes", "loop_stall_turns", "loop_threshold", "loop_window", "max_active_sessions",
    "max_call_seconds", "max_child_sessions", "max_cost_per_call_usd", "max_events",
    "max_inflight_calls", "max_session_depth", "max_session_lifetime_seconds", "max_session_seconds",
    "max_steps", "max_tokens_out_per_call", "max_total_tokens", "on_anomaly", "on_budget_soft",
    "on_loop", "on_plane_loss", "on_provider_failure", "on_spike", "on_trip", "on_unpriced_model",
    "postures", "refusals", "run_budget_usd", "run_max_total_tokens", "spike_confirm",
    "spike_cooldown_seconds", "spike_detection", "spike_factor", "spike_limit_calls",
    "spike_max_strikes", "spike_min_duration_s", "spike_min_output_tokens", "spike_warmup_calls",
    "spike_window", "stale_halt", "throttle_base_seconds", "throttle_max_seconds",
    "tokens_per_minute_limit", "unpriced_price_per_1m_usd",
)

#: Exact spellings that must not appear: each removed keyword as ``<name>=``, and ``repeatable=``
#: (``@runbound.tool(repeatable=True)`` became ``polling=True``).
REMOVED_KEYWORDS = tuple(f"{name}=" for name in REMOVED_CONFIG_FIELDS) + ("repeatable=",)

#: Functions removed in S1 (API.md "Removed in S1"), matched as ``runbound.<name>(`` or
#: ``from runbound import <name>``: a bare ``runbound.key_hash`` is also an attribute *name* (an
#: OpenTelemetry attribute key), which is not the removed function.
REMOVED_FUNCTIONS = (
    "wrap", "unpatch", "llm", "record_call", "budget", "circuit_state", "inflight_calls",
    "active_sessions", "posture", "envelope", "safe_mode", "enter_safe_mode", "exit_safe_mode",
    "fleet_status", "is_tripped", "clear", "session_status", "key_hash", "verify_webhook_signature",
)
#: Classes removed in S1 and the modules removed in S2, matched as ``runbound.<name>`` (word
#: boundary) or ``from runbound import <name>``. ``runbound.quota`` exists again (per-tool quotas).
REMOVED_TYPES_AND_MODULES = (
    "BudgetView", "Refusal", "CircuitOpen", "PostureState", "SafeModeViolation",
    "wrappers", "autowrap", "circuit", "pricing", "detectors", "ladder", "demo",
)
REMOVED_NAMES = REMOVED_FUNCTIONS + REMOVED_TYPES_AND_MODULES

#: Words of the old product (the S9 docs brief). Case-insensitive substrings.
BANNED_WORDS = ("runaway", "circuit", "spike", "budget", "posture", "safe mode")

_NAME_RE = re.compile(
    r"\brunbound\.(%s)\s*\(|\brunbound\.(%s)\b|\bfrom runbound import [^\n]*\b(%s)\b"
    % ("|".join(REMOVED_FUNCTIONS), "|".join(REMOVED_TYPES_AND_MODULES), "|".join(REMOVED_NAMES))
)


def _prose_files() -> list[Path]:
    """README.md, every page under docs/, and every file under examples/."""
    files = [_ROOT / "README.md"] if (_ROOT / "README.md").is_file() else []
    files += list((_ROOT / "docs").rglob("*.md"))
    examples = _ROOT / "examples"
    if examples.is_dir():
        files += [p for p in examples.rglob("*") if p.is_file() and p.suffix in (".py", ".md")]
    return sorted(files)


def _offenders(match) -> list[str]:
    out = []
    for path in _prose_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for hit in match(line):
                out.append(f"{path.relative_to(_ROOT)}:{number}: {hit!r}")
    return out


def test_no_doc_or_example_teaches_a_removed_keyword():
    assert _prose_files(), "expected README.md, docs/ or examples/ next to tests/"
    offenders = _offenders(lambda line: [k for k in REMOVED_KEYWORDS if k in line])
    assert not offenders, "removed init()/@tool keywords still taught:\n" + "\n".join(offenders)


def test_no_doc_or_example_names_a_removed_api():
    offenders = _offenders(lambda line: [m.group(0) for m in _NAME_RE.finditer(line)])
    assert not offenders, "names removed in S1 still taught:\n" + "\n".join(offenders)


def test_no_doc_or_example_uses_a_banned_word():
    offenders = _offenders(lambda line: [w for w in BANNED_WORDS if w in line.lower()])
    assert not offenders, "banned words (CHANGELOG.md only):\n" + "\n".join(offenders)


def test_removed_keywords_are_really_gone():
    """Every listed keyword makes ``RunboundConfig`` raise, so nothing still valid is banned."""
    import dataclasses

    import runbound

    fields = {f.name for f in dataclasses.fields(runbound.RunboundConfig)}
    assert not fields & set(REMOVED_CONFIG_FIELDS)
    for name in REMOVED_CONFIG_FIELDS:
        with pytest.raises(TypeError):
            runbound.RunboundConfig(**{name: None})


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_removed_names_are_really_gone(name):
    import importlib

    import runbound

    assert not hasattr(runbound, name), name
    if name in ("wrappers", "autowrap", "circuit", "pricing", "detectors", "ladder", "demo"):
        with pytest.raises(ImportError):
            importlib.import_module(f"runbound.{name}")
