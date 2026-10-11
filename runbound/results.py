"""What a tool returned, made safe to hand back: redact, truncate, scan (RS-3 §2, §3).

Pure functions, no state, stdlib only. The order is fixed — **redact, truncate,
scan** — so a secret cannot straddle a cut and survive it, and the scan sees what
the model will see.

* :func:`redact` replaces the value of a key (exact, case-insensitive, recursive
  through dicts and lists) with ``"[REDACTED]"``. Strings have no keys, so a
  non-JSON result is not redacted in v1.
* :func:`truncate` keeps a JSON document *valid*: string leaves are cut at
  ``max_str``, lists and dicts at ``max_items``, nesting at ``max_depth``, each with
  a marker that states what was removed; then the serialized size is capped at
  ``max_chars`` with head and tail around a marker. A plain string is cut by the
  size cap alone.
* :func:`scan` tags text that matches one of 15 prompt-injection patterns. It
  reports pattern *names*, never the matched text, and **never blocks**: it is a
  regex, it misses paraphrase, other languages and encodings, and its tag is
  telemetry for the plane, not a boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

REDACTED = "[REDACTED]"

#: Sentry's ``DEFAULT_DENYLIST`` (33 keys, matched lowercased) plus four of ours.
DEFAULT_REDACT_KEYS = frozenset({
    "password", "passwd", "secret", "api_key", "apikey", "auth", "credentials", "mysql_pwd",
    "privatekey", "private_key", "token", "session", "authorization", "proxy-authorization",
    "cookie", "set_cookie", "x_api_key", "csrf_token", "sessionid",
    "csrftoken", "x_csrftoken", "x_forwarded_for", "aiohttp_session", "connect.sid", "csrf",
    "_csrf", "_csrf_token", "phpsessid", "_session", "symfony", "user_session", "_xsrf", "xsrf-token",
    "access_token", "refresh_token", "client_secret", "bearer",
})

#: Defaults for :class:`Limits`, between Codex's 10,000 tokens and Claude Code's 30,000 chars.
DEFAULT_MAX_CHARS = 16_000


@dataclass(frozen=True)
class Limits:
    """The truncation caps: the whole serialized result, a string leaf, a list or dict, nesting."""

    max_chars: int = DEFAULT_MAX_CHARS
    max_str: int = 200
    max_items: int = 20
    max_depth: int = 6


@dataclass
class Processed:
    """What the post chain did to one result."""

    result: Any
    truncated: bool = False
    valid_json: bool = True
    chars_in: int = 0
    chars_out: int = 0
    flags: tuple = ()
    flag_score: float = 0.0
    failed: list = field(default_factory=list)  # built-in steps that raised

    @property
    def flagged(self) -> bool:
        return self.flag_score >= FLAG_AT


def redact(value: Any, keys: frozenset | set, marker: str = REDACTED) -> Any:
    """``value`` with every dict value whose key is in ``keys`` (lowercased) replaced."""
    if isinstance(value, dict):
        return {
            key: (marker if isinstance(key, str) and key.lower() in keys else redact(item, keys, marker))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, keys, marker) for item in value]
    return value


def _cut(value: Any, limits: Limits, depth: int, stats: dict) -> Any:
    if isinstance(value, (dict, list)) and depth >= limits.max_depth:
        stats["depth"] += 1
        return "…[nested value truncated]"
    if isinstance(value, str):
        if len(value) > limits.max_str:
            stats["strings"] += 1
            return value[: limits.max_str] + f"…[truncated {len(value) - limits.max_str} chars]"
        return value
    if isinstance(value, list):
        out = [_cut(item, limits, depth + 1, stats) for item in value[: limits.max_items]]
        if len(value) > limits.max_items:
            stats["items"] += len(value) - limits.max_items
            out.append(f"…[{len(value) - limits.max_items} more items truncated]")
        return out
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= limits.max_items:
                stats["items"] += len(value) - limits.max_items
                out["_runbound_truncated"] = f"{len(value) - limits.max_items} more keys"
                break
            out[key] = _cut(item, limits, depth + 1, stats)
        return out
    return value


def _head_tail(text: str, max_chars: int, note: str) -> str:
    half = max_chars // 2
    return text[:half] + f"\n…[truncated {len(text) - max_chars} chars{note}]…\n" + text[-half:]


def truncate(value: Any, limits: Limits = Limits()) -> "tuple[Any, dict]":
    """``value`` cut to ``limits``; returns ``(value, info)``.

    ``info`` has ``truncated``, ``valid_json`` and the chars before and after. A JSON
    value stays a JSON value (same types, markers inside it) unless its serialized
    form still exceeds ``max_chars``, in which case it becomes a head-and-tail
    *string* whose marker says the structure is no longer guaranteed.
    """
    if isinstance(value, (dict, list)):
        stats = {"strings": 0, "items": 0, "depth": 0}
        before = len(json.dumps(value, ensure_ascii=False, default=str))
        cut = _cut(value, limits, 0, stats)
        text = json.dumps(cut, ensure_ascii=False, default=str)
        if len(text) > limits.max_chars:
            return (
                _head_tail(text, limits.max_chars, " of JSON; structure no longer guaranteed"),
                {"truncated": True, "valid_json": False, "chars_in": before, "chars_out": limits.max_chars},
            )
        return cut, {"truncated": any(stats.values()), "valid_json": True, "chars_in": before, "chars_out": len(text)}
    if isinstance(value, str):
        if len(value) > limits.max_chars:
            return (
                _head_tail(value, limits.max_chars, ""),
                {"truncated": True, "valid_json": True, "chars_in": len(value), "chars_out": limits.max_chars},
            )
        return value, {"truncated": False, "valid_json": True, "chars_in": len(value), "chars_out": len(value)}
    return value, {"truncated": False, "valid_json": True, "chars_in": 0, "chars_out": 0}


# --- the scan -----------------------------------------------------------------------

_CASE_SENSITIVE = ("override_chatml", "override_llama", "hidden_chars", "base64_blob")

PATTERNS = {
    "ignore_previous": r"ignore (all |any )?(the )?(previous|prior|above|earlier|preceding)( \w+)? (instructions?|prompts?|directions?)",
    "disregard_rules": r"(disregard|forget|bypass|override) (all |any |your )?(previous |prior |above |system )?(instructions?|rules|guidelines|programming)",
    "you_are_now": r"\byou are now\b.{0,60}\b(assistant|ai|bot|model|dan|unrestricted)",
    "act_without_limits": r"\b(act|behave|respond) as (if you (are|were)|a|an)\b.{0,40}\b(no|without) (restrictions|limits|rules)",
    "fake_turn": r"(?m)^\s*(system|assistant)\s*:\s",
    "override_chatml": r"<\|(im_start|im_end|endoftext|system|user|assistant)\|>",
    "override_llama": r"\[/?INST\]|<<\s*/?SYS\s*>>",
    "markdown_header": r"(?m)^#{2,3}\s*(instruction|system prompt)\b",
    "markdown_image_exfil": r"!\[[^\]]*\]\(https?://[^)\s]*\?[^)\s]+\)",
    "exfil_verb": r"\b(send|post|forward|email|upload|exfiltrate)\b[^.\n]{0,80}\b(to|at)\s+https?://",
    "tool_call_forgery": r"[\"']?(tool_calls?|function_call|tool_use)[\"']?\s*:\s*[\[{]",
    "hidden_chars": "[​-‏⁠﻿‪-‮]",
    "hidden_html": r"<!--[^>]{0,400}\b(ignore|assistant|instruction|system)\b[^>]*-->|display\s*:\s*none[^>]{0,200}\b(ignore|assistant|instruction)\b",
    "addressed_to_ai": r"\b(important|attention|note)\s*:?\s*(to|for) (the )?(ai|assistant|llm|model|agent)\b|\bthe (assistant|ai|agent) (must|should|shall)\b",
    "base64_blob": r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/])",
}

_REGEX = {
    name: re.compile(pattern, 0 if name in _CASE_SENSITIVE else re.IGNORECASE)
    for name, pattern in PATTERNS.items()
}

#: Weight 0.5 patterns collide with legitimate text (YAML keys, Markdown headings, ZWJ
#: in emoji, tool-calling JSON, long base64) and only count with another.
WEIGHTS = {
    name: (0.5 if name in ("fake_turn", "markdown_header", "tool_call_forgery", "hidden_chars", "base64_blob") else 1.0)
    for name in PATTERNS
}

#: A result is flagged once the weights of the patterns that hit sum to this.
FLAG_AT = 1.0


def scan(text: str) -> "tuple[tuple, float]":
    """``(pattern names that matched, their summed weight)``; never the matched text."""
    hits = tuple(name for name, rx in _REGEX.items() if rx.search(text))
    return hits, sum(WEIGHTS[name] for name in hits)


def text_of(value: Any) -> str:
    """The text a scan reads: the string itself, or a JSON dict/list's serialization."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def process(value: Any, limits: Limits, keys: frozenset, *, scan_it: bool = True) -> Processed:
    """Run redact, truncate and scan over one result; a step that raises is recorded, not raised.

    A failed redaction is the caller's to act on (``failed == ["redact"]``): it must
    withhold the output. A failed truncation falls back to the plain size cap on the
    serialized text; a failed scan leaves the result untagged and says so.
    """
    out = Processed(value)
    try:
        value = redact(value, keys)
    except Exception:
        out.failed.append("redact")
        return out
    try:
        value, info = truncate(value, limits)
        out.truncated, out.valid_json = info["truncated"], info["valid_json"]
        out.chars_in, out.chars_out = info["chars_in"], info["chars_out"]
    except Exception:
        out.failed.append("truncate")
        try:
            text = text_of(value)
            out.truncated, out.valid_json = len(text) > limits.max_chars, False
            value = text[: limits.max_chars]
        except Exception:
            value = "Output withheld: post-hook truncate failed"
            out.failed.append("truncate-fallback")
    out.result = value
    if scan_it:
        try:
            out.flags, out.flag_score = scan(text_of(value))
        except Exception:
            out.failed.append("scan")
    return out
