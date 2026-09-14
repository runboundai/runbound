"""What a provider's rate-limit headers said, as plain numbers.

Both providers already tell every caller how much quota is left and when it
comes back — Anthropic on twelve ``anthropic-ratelimit-*`` headers, OpenAI on
``x-ratelimit-*`` — and a 429 adds ``Retry-After`` on top. This module turns
that text into three numbers a circuit breaker can act on, and nothing else.

Two limits are worth knowing before reading further.

**A plain successful call carries no headers at all.** Both SDKs hand back a
parsed model — ``anthropic.types.Message``, ``openai.types.Completion`` — with
no ``.headers`` anywhere on it. Headers survive only on an error (every
``APIStatusError`` keeps ``.response.headers``) or when the customer's own
call already went through ``with_raw_response`` / ``.parse()``.
:func:`headers_of` is the whole of that story: it reads what is already in
hand and never asks a client to make the call differently.

**Nothing here raises.** Every value a provider, a proxy or a gateway put on
the wire is somebody else's text; a field that cannot be read comes back as
``None``, and a :class:`Quota` of all ``None`` is the fail-open answer that
means "say nothing about this provider". Stdlib only, like the rest of the
SDK: no dependency reads a header for us.
"""

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

#: Never cool down longer than this from a header, whatever the header says.
#: A quota reset the provider states in days, or a proxy's nonsense value,
#: must not wedge an agent's provider shut for a day. The customer's own
#: `circuit_cooldown_seconds` is the floor; this is the ceiling.
MAX_COOLDOWN_S: float = 3600.0

#: How long one Go duration unit lasts, in seconds. OpenAI states resets this
#: way — ``"6m0s"``, ``"20ms"`` — and both micro spellings appear in the wild.
_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}

#: One ``<number><unit>`` term of a Go duration. Longer units first, so
#: ``"20ms"`` is twenty milliseconds and never twenty minutes.
_TERM = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")

#: Each provider's rate-limit vocabulary: the buckets it publishes and how it
#: spells the remaining and reset header for one of them. Read in order, and
#: the first vocabulary that yields anything at all names the source — a
#: response carries one provider's headers, not both.
_VENDORS = (
    (
        "anthropic",
        ("requests", "tokens", "input-tokens", "output-tokens"),
        "anthropic-ratelimit-{}-remaining",
        "anthropic-ratelimit-{}-reset",
    ),
    (
        "openai",
        ("requests", "tokens"),
        "x-ratelimit-remaining-{}",
        "x-ratelimit-reset-{}",
    ),
)


@dataclass(frozen=True)
class Quota:
    """What one response's headers said about what is left.

    ``remaining`` is the SMALLEST remaining count across every bucket that
    could be read (requests, tokens, input tokens, output tokens): the
    tightest one is the one that will refuse the next call. ``None`` means no
    bucket was readable — which is not the same as zero and must never open a
    circuit.
    """

    remaining: int | None = None
    reset_s: float | None = None  # seconds from now until the earliest reset, >= 0
    retry_after_s: float | None = None  # from Retry-After, >= 0
    source: str | None = None  # "anthropic" | "openai" | None


def headers_of(obj: Any) -> Mapping[str, str] | None:
    """Headers on something the customer's call already produced, or None.

    Tries, in order: a response object's own ``.headers``
    (``with_raw_response`` / ``.parse()``), an exception's
    ``.response.headers``, and an object's ``.http_response.headers``. Returns
    None for a plain parsed model, which is what both SDKs hand back from an
    ordinary call. Never raises, never asks a client for a raw response, never
    makes a call.
    """
    for path in (("headers",), ("response", "headers"), ("http_response", "headers")):
        found = _walk(obj, path)
        if found is not None:
            return found
    return None


def read_quota(headers: Any, now: float | None = None) -> Quota:
    """Parse one response's rate-limit headers. Never raises.

    ``now`` is a wall-clock epoch (``time.time()``), injectable for tests,
    because Anthropic states resets as absolute instants and the only way to
    turn one into a delay is to subtract the wall clock. Every field that
    cannot be parsed is None; a Quota of all-None is the fail-open answer and
    means "say nothing about this provider".

    ``headers`` is normally the mapping :func:`headers_of` returned, but a
    response or an exception is accepted too and has its headers taken off it
    first, so a caller holding one object need not unwrap it by hand.
    """
    try:
        table = _table(headers)
        if not table:
            return Quota()
        moment = _wall(now)
        remaining: int | None = None
        reset_s: float | None = None
        source: str | None = None
        for vendor, buckets, remaining_name, reset_name in _VENDORS:
            pairs = [
                (
                    _parse_count(table.get(remaining_name.format(bucket))),
                    parse_reset(table.get(reset_name.format(bucket)), moment),
                )
                for bucket in buckets
            ]
            if all(count is None and reset is None for count, reset in pairs):
                continue
            remaining, reset_s, source = *_binding(pairs), vendor
            break
        return Quota(
            remaining=remaining,
            reset_s=reset_s,
            retry_after_s=parse_retry_after(table.get("retry-after"), moment),
            source=source,
        )
    except Exception:
        return Quota()


def cooldown_for(reset_s: float | None, fallback: float) -> float:
    """How long a header may hold a circuit shut: the reset, capped, else ``fallback``.

    ``None`` and any non-positive or unreadable reset fall back to the
    customer's own configured cooldown, which is the floor;
    :data:`MAX_COOLDOWN_S` is the ceiling, because a reset stated in days — by
    a provider, or by a proxy inventing one — must not wedge an agent's
    provider shut for a day.
    """
    try:
        wanted = None if reset_s is None else float(reset_s)
    except Exception:
        wanted = None
    if wanted is None or not (wanted > 0.0):
        return fallback
    return min(wanted, MAX_COOLDOWN_S)


def parse_retry_after(value: Any, now: float | None = None) -> float | None:
    """Seconds, or an HTTP-date, or None. Negative clamps to 0.0."""
    try:
        seconds = _parse_number(value)
        if seconds is not None:
            return max(0.0, seconds)
        if not isinstance(value, str) or not value.strip():
            return None
        moment = parsedate_to_datetime(value.strip())
        if moment is None:
            return None
        return max(0.0, _epoch(moment) - _wall(now))
    except Exception:
        return None


def parse_reset(value: Any, now: float | None = None) -> float | None:
    """Seconds until a reset stated as an ISO-8601 instant, a duration or a number.

    Handles Anthropic's absolute instants (``"2026-09-13T21:15:04Z"``, whose
    trailing ``Z`` older Pythons reject), OpenAI's Go-style durations
    (``"6m0s"``, ``"1s"``, ``"20ms"``) and a plain number of seconds. A reset
    already in the past clamps to 0.0. Unparseable is None.

    The one-hour ceiling is deliberately *not* applied here: this reports what
    the header said, and clamping it is the breaker's decision — see
    :data:`MAX_COOLDOWN_S` and ``CircuitBreaker.note_quota``.
    """
    try:
        seconds = _parse_number(value)
        if seconds is not None:
            return max(0.0, seconds)
        if not isinstance(value, str) or not value.strip():
            return None
        duration = _parse_duration(value)
        if duration is not None:
            return max(0.0, duration)
        instant = _parse_instant(value)
        if instant is None:
            return None
        return max(0.0, instant - _wall(now))
    except Exception:
        return None


# --- reading someone else's objects -----------------------------------------


def _walk(obj: Any, path: tuple[str, ...]) -> Mapping[str, str] | None:
    """Follow ``path`` of attributes off ``obj`` to a headers mapping, or None."""
    try:
        current = obj
        for name in path:
            current = getattr(current, name, None)
            if current is None:
                return None
        return current if _is_mapping(current) else None
    except Exception:
        return None


def _is_mapping(obj: Any) -> bool:
    """Does ``obj`` behave like a headers mapping — ``items()`` and ``get()``?"""
    try:
        return callable(getattr(obj, "items", None)) and callable(getattr(obj, "get", None))
    except Exception:
        return False


def _table(headers: Any) -> dict[str, Any]:
    """``headers`` as a lowercase-keyed dict, or empty when it cannot be read.

    Accepts the mapping itself, or a response or exception it can be taken off
    — the same three places :func:`headers_of` looks.
    """
    mapping = headers if _is_mapping(headers) else headers_of(headers)
    if mapping is None:
        return {}
    table: dict[str, Any] = {}
    try:
        pairs = mapping.items()
    except Exception:
        return {}
    try:
        for key, value in pairs:
            try:
                table[str(key).strip().lower()] = value
            except Exception:
                continue
    except Exception:
        return table
    return table


# --- turning one header value into a number ---------------------------------


def _parse_number(value: Any) -> float | None:
    """``value`` as a finite number of seconds, or None if it is not one.

    ``bool`` is excluded on purpose: ``True`` is an ``int`` in Python and a
    header that arrived as one is a bug somewhere, not a one-second delay.
    """
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            number = float(text)
        else:
            return None
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _parse_count(value: Any) -> int | None:
    """One remaining-quota header as a whole number, never below zero.

    A count below zero is a proxy's arithmetic, not a provider's: it still
    means the bucket is spent, so it reads as 0 rather than as unreadable.
    """
    number = _parse_number(value)
    if number is None:
        return None
    try:
        return max(0, int(number))
    except Exception:
        return None


def _parse_duration(value: str) -> float | None:
    """A Go-style duration (``"1h2m3s"``, ``"20ms"``) in seconds, or None.

    Every character has to belong to a term: a string that only *starts* like
    a duration is not one, and guessing at the rest would invent a cooldown.
    """
    text = value.strip().lower()
    sign = 1.0
    if text[:1] in ("+", "-"):
        sign = -1.0 if text[0] == "-" else 1.0
        text = text[1:]
    total = 0.0
    position = 0
    for match in _TERM.finditer(text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * _UNITS[match.group(2)]
        position = match.end()
    if position == 0 or position != len(text):
        return None
    return sign * total


def _parse_instant(value: str) -> float | None:
    """An ISO-8601 instant as a wall-clock epoch, or None.

    The trailing ``Z`` is rewritten to ``+00:00`` because
    ``datetime.fromisoformat`` rejects it before Python 3.11 and the SDK
    supports 3.10. An instant with no zone at all is read as UTC — which is
    what both providers mean by one.
    """
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except Exception:
        return None
    return _epoch(moment)


def _epoch(moment: datetime) -> float:
    """A datetime as a wall-clock epoch, treating a naive one as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _binding(pairs: list[tuple[int | None, float | None]]) -> tuple[int | None, float | None]:
    """The tightest bucket's count, and the reset *of that bucket*.

    Each bucket's count and reset belong together. Taking the smallest count
    from one bucket and the earliest reset from another pairs numbers that
    describe different things: OpenAI's real headers on 2026-09-14 said
    requests 9999 left, resetting in 8.64s, and tokens 199990 left, resetting
    in 3ms — so a spent requests bucket would have opened a circuit for 3ms
    instead of the 8.64s the provider actually asked for. Found by T174, the
    first time T144's reader saw a real OpenAI response.

    When several buckets share the smallest count, the *latest* of their
    resets wins: a call needs every spent bucket refilled, not just one. With
    no readable count at all the earliest reset is kept for information only
    — a ``remaining`` of None never opens a circuit.
    """
    counts = [count for count, _ in pairs if count is not None]
    if not counts:
        return None, _least(reset for _, reset in pairs)
    remaining = min(counts)
    resets = [reset for count, reset in pairs if count == remaining and reset is not None]
    return remaining, (max(resets) if resets else None)


def _least(values) -> Any:
    """The smallest value that is not None, or None when every one was."""
    found = [value for value in values if value is not None]
    return min(found) if found else None


def _wall(now: float | None) -> float:
    """The wall clock to measure an absolute instant against.

    Wall time, not monotonic: a header states when a reset happens in the
    provider's calendar, and only ``time.time()`` is in the same units.
    Everything downstream of here — the breaker's own windows and cooldowns —
    stays monotonic; this converts once, at the edge.
    """
    return time.time() if now is None else float(now)
