"""Reading what a provider's rate-limit headers said. Pure parsing, no SDKs.

Every header name and value here is either copied from a real recorded
response (the Anthropic ones — see the conformance kit's
``fixtures/anthropic/1.5.0/messages-sync-plain.json``) or is **synthetic**:
hand-built. In particular **no recorded 429 exists** — Wave G forbids
provoking one against a real API — so every 429 and every ``Retry-After``
case in this file is synthetic, written by hand to the HTTP specification and
the two providers' documented header names.

The bar the whole module has to clear is in the last section: nothing here may
raise, whatever it is handed, and anything it cannot read must come back as
``None`` rather than as a guess.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from runbound.quota import (
    MAX_COOLDOWN_S,
    Quota,
    headers_of,
    parse_reset,
    parse_retry_after,
    read_quota,
)

#: A fixed wall clock, so an absolute reset instant has a knowable delay.
NOW = 1_789_000_000.0


def iso(offset_s: float, suffix: str = "Z") -> str:
    """``NOW + offset_s`` as an ISO-8601 instant, Anthropic's spelling."""
    moment = datetime.fromtimestamp(NOW + offset_s, tz=timezone.utc)
    text = moment.replace(microsecond=0).isoformat()
    return text.replace("+00:00", "") + suffix if suffix else text


def http_date(offset_s: float) -> str:
    """``NOW + offset_s`` as an RFC 7231 HTTP-date."""
    from email.utils import format_datetime

    return format_datetime(datetime.fromtimestamp(NOW + offset_s, tz=timezone.utc))


#: The twelve rate-limit headers a real Anthropic response carries, verbatim
#: from the recorded plain fixture except for the reset instants, which are
#: written against this file's fixed clock so the delays are checkable.
ANTHROPIC_HEADERS = {
    "anthropic-ratelimit-input-tokens-limit": "10000000",
    "anthropic-ratelimit-input-tokens-remaining": "10000000",
    "anthropic-ratelimit-input-tokens-reset": iso(60),
    "anthropic-ratelimit-output-tokens-limit": "2000000",
    "anthropic-ratelimit-output-tokens-remaining": "2000000",
    "anthropic-ratelimit-output-tokens-reset": iso(60),
    "anthropic-ratelimit-requests-limit": "10000",
    "anthropic-ratelimit-requests-remaining": "9999",
    "anthropic-ratelimit-requests-reset": iso(30),
    "anthropic-ratelimit-tokens-limit": "12000000",
    "anthropic-ratelimit-tokens-remaining": "12000000",
    "anthropic-ratelimit-tokens-reset": iso(60),
    "content-type": "application/json",
    "request-id": "req_011Cf28Yu2ARnT7uYNWgHLit",
}

#: OpenAI's four, with the Go-style durations it states resets as. Synthetic:
#: there is no recorded OpenAI fixture yet (no key at record time).
OPENAI_HEADERS = {
    "x-ratelimit-limit-requests": "10000",
    "x-ratelimit-limit-tokens": "2000000",
    "x-ratelimit-remaining-requests": "9998",
    "x-ratelimit-remaining-tokens": "1999000",
    "x-ratelimit-reset-requests": "6m0s",
    "x-ratelimit-reset-tokens": "20ms",
}


# --- the real Anthropic header set ------------------------------------------


def test_the_twelve_anthropic_headers_are_read_as_anthropic():
    assert read_quota(ANTHROPIC_HEADERS, now=NOW).source == "anthropic"


def test_remaining_is_the_smallest_count_across_every_bucket():
    quota = read_quota(ANTHROPIC_HEADERS, now=NOW)

    # requests: 9999. Tokens: millions. The tightest bucket is the one that
    # will refuse the next call.
    assert quota.remaining == 9999


def test_reset_is_the_earliest_of_the_buckets():
    quota = read_quota(ANTHROPIC_HEADERS, now=NOW)

    assert quota.reset_s == pytest.approx(30.0, abs=1.0)


def test_a_plain_anthropic_response_carries_no_retry_after():
    assert read_quota(ANTHROPIC_HEADERS, now=NOW).retry_after_s is None


def test_header_names_are_matched_case_insensitively():
    shouted = {name.upper(): value for name, value in ANTHROPIC_HEADERS.items()}

    assert read_quota(shouted, now=NOW).remaining == 9999


def test_a_spent_anthropic_bucket_reads_as_zero_remaining():
    spent = dict(ANTHROPIC_HEADERS)
    spent["anthropic-ratelimit-tokens-remaining"] = "0"

    assert read_quota(spent, now=NOW).remaining == 0


# --- ISO-8601 resets --------------------------------------------------------


def test_an_iso_reset_with_a_trailing_z_parses_on_this_python():
    # datetime.fromisoformat rejects "Z" before 3.11; the SDK supports 3.10+.
    assert parse_reset("2026-09-13T21:15:04Z", now=NOW) is not None


def test_a_future_iso_reset_is_the_delay_from_the_injected_now():
    assert parse_reset(iso(45), now=NOW) == pytest.approx(45.0, abs=1.0)


def test_an_iso_reset_already_in_the_past_clamps_to_zero():
    assert parse_reset(iso(-500), now=NOW) == 0.0


def test_an_iso_reset_with_an_explicit_offset_parses():
    moment = datetime.fromtimestamp(NOW + 90, tz=timezone(timedelta(hours=5, minutes=30)))
    assert parse_reset(moment.replace(microsecond=0).isoformat(), now=NOW) == pytest.approx(
        90.0, abs=1.0
    )


def test_an_iso_reset_without_a_zone_is_read_as_utc():
    naive = datetime.fromtimestamp(NOW + 120, tz=timezone.utc).replace(
        tzinfo=None, microsecond=0
    )
    assert parse_reset(naive.isoformat(), now=NOW) == pytest.approx(120.0, abs=1.0)


# --- Go-style durations, which is how OpenAI states a reset ------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("6m0s", 360.0),
        ("1s", 1.0),
        ("20ms", 0.02),
        ("1m30s", 90.0),
        ("2h", 7200.0),
        ("1h2m3s", 3723.0),
        ("500ns", 0.0000005),
        ("1.5s", 1.5),
    ],
)
def test_go_durations_parse(value, seconds):
    assert parse_reset(value, now=NOW) == pytest.approx(seconds)


def test_openais_four_headers_are_read_as_openai():
    quota = read_quota(OPENAI_HEADERS, now=NOW)

    assert quota.source == "openai"
    assert quota.remaining == 9998  # the smaller of 9998 requests and 1999000 tokens
    assert quota.reset_s == pytest.approx(0.02)  # the earlier of 6m0s and 20ms


def test_a_plain_number_of_seconds_is_a_reset_too():
    assert parse_reset("45", now=NOW) == 45.0
    assert parse_reset(12.5, now=NOW) == 12.5


def test_a_negative_plain_reset_clamps_to_zero():
    assert parse_reset(-30, now=NOW) == 0.0


# --- Retry-After ------------------------------------------------------------


def test_retry_after_as_a_number_of_seconds():
    # Synthetic: no recorded 429 exists.
    assert parse_retry_after("7", now=NOW) == 7.0
    assert parse_retry_after(7, now=NOW) == 7.0


def test_retry_after_as_an_http_date():
    assert parse_retry_after(http_date(7), now=NOW) == pytest.approx(7.0, abs=1.0)


def test_a_retry_after_date_in_the_past_clamps_to_zero():
    assert parse_retry_after(http_date(-60), now=NOW) == 0.0


def test_a_negative_retry_after_clamps_to_zero():
    assert parse_retry_after("-5", now=NOW) == 0.0


def test_read_quota_picks_retry_after_out_of_a_synthetic_429():
    # Synthetic 429 headers: hand-built, because Wave G forbids provoking a
    # real one and no fixture contains it.
    headers = dict(ANTHROPIC_HEADERS)
    headers["anthropic-ratelimit-requests-remaining"] = "0"
    headers["retry-after"] = "7"

    quota = read_quota(headers, now=NOW)

    assert quota.remaining == 0
    assert quota.retry_after_s == 7.0
    assert quota.source == "anthropic"


def test_retry_after_alone_says_nothing_about_a_vendor():
    quota = read_quota({"retry-after": "7"}, now=NOW)

    assert quota == Quota(retry_after_s=7.0)


# --- the ceiling ------------------------------------------------------------


def test_the_ceiling_is_an_hour():
    assert MAX_COOLDOWN_S == 3600.0


def test_reading_does_not_apply_the_ceiling_itself():
    """The ceiling belongs to the breaker: the reader reports what it read."""
    assert parse_reset("48h", now=NOW) == pytest.approx(172_800.0)


# --- garbage, in every field, from every direction --------------------------


class Hostile:
    """An object that raises on every way of getting at it."""

    def __getitem__(self, name):  # pragma: no cover - raising is the point
        raise RuntimeError("no")

    def items(self):  # pragma: no cover - raising is the point
        raise RuntimeError("no")

    def get(self, name, default=None):  # pragma: no cover - raising is the point
        raise RuntimeError("no")


@pytest.mark.parametrize(
    "headers",
    [None, "", "soon", 0, 7, [], ["retry-after", "7"], [("retry-after", "7")], Hostile(), object()],
)
def test_garbage_headers_say_nothing_and_raise_nothing(headers):
    assert read_quota(headers, now=NOW) == Quota()


@pytest.mark.parametrize("value", ["", "soon", None, [], {}, Hostile(), object(), "NaN"])
def test_garbage_in_every_value_says_nothing(value):
    headers = {
        "anthropic-ratelimit-requests-remaining": value,
        "anthropic-ratelimit-requests-reset": value,
        "anthropic-ratelimit-tokens-remaining": value,
        "anthropic-ratelimit-tokens-reset": value,
        "x-ratelimit-remaining-requests": value,
        "x-ratelimit-reset-requests": value,
        "retry-after": value,
    }

    assert read_quota(headers, now=NOW) == Quota()


@pytest.mark.parametrize("value", ["", "soon", None, [], Hostile(), object(), float("nan")])
def test_garbage_never_parses_as_a_reset_or_a_retry_after(value):
    assert parse_reset(value, now=NOW) is None
    assert parse_retry_after(value, now=NOW) is None


def test_one_unreadable_bucket_does_not_hide_a_readable_one():
    headers = {
        "anthropic-ratelimit-requests-remaining": "nonsense",
        "anthropic-ratelimit-tokens-remaining": "40",
        "anthropic-ratelimit-tokens-reset": "10s",
    }

    quota = read_quota(headers, now=NOW)

    assert quota.remaining == 40
    assert quota.reset_s == 10.0


def test_a_quota_with_nothing_readable_is_all_none():
    quota = read_quota({"content-type": "application/json"}, now=NOW)

    assert quota == Quota()
    assert quota.remaining is None and quota.reset_s is None
    assert quota.retry_after_s is None and quota.source is None


def test_a_missing_now_falls_back_to_the_wall_clock():
    # No injection: the delay is measured against time.time() and must still
    # land near the ten seconds the instant is away.
    soon = datetime.fromtimestamp(time.time() + 10, tz=timezone.utc)
    text = soon.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    assert parse_reset(text) == pytest.approx(10.0, abs=2.0)


# --- headers_of -------------------------------------------------------------


class Parsed:
    """A plain parsed model: what both SDKs hand back from an ordinary call."""

    def __init__(self) -> None:
        self.usage = object()


class Raw:
    """A ``with_raw_response`` / ``.parse()`` response, which does have them."""

    def __init__(self, headers) -> None:
        self.headers = headers


class Failure(Exception):
    """An ``APIStatusError``: no ``.headers``, but ``.response.headers``."""

    def __init__(self, headers) -> None:
        super().__init__("boom")
        self.status_code = 429
        self.response = Raw(headers)


class Legacy:
    """Something that keeps its response one level down under another name."""

    def __init__(self, headers) -> None:
        self.http_response = Raw(headers)


def test_a_plain_parsed_model_has_no_headers():
    # This is the whole limitation of the feature: an ordinary successful
    # anthropic.types.Message exposes no headers at all.
    assert headers_of(Parsed()) is None


def test_a_raw_response_hands_over_its_own_headers():
    assert headers_of(Raw(ANTHROPIC_HEADERS)) == ANTHROPIC_HEADERS


def test_an_exception_hands_over_its_responses_headers():
    assert headers_of(Failure(ANTHROPIC_HEADERS)) == ANTHROPIC_HEADERS


def test_an_http_response_attribute_is_the_third_place_looked():
    assert headers_of(Legacy(ANTHROPIC_HEADERS)) == ANTHROPIC_HEADERS


@pytest.mark.parametrize("obj", [None, 7, "text", [], object(), Hostile()])
def test_headers_of_never_raises_and_says_none(obj):
    assert headers_of(obj) is None


def test_something_whose_headers_are_not_a_mapping_says_none():
    assert headers_of(Raw("not-a-mapping")) is None
    assert headers_of(Raw(None)) is None


def test_headers_of_an_object_whose_attribute_explodes_says_none():
    class Exploding:
        @property
        def headers(self):  # pragma: no cover - raising is the point
            raise RuntimeError("no")

    assert headers_of(Exploding()) is None


def test_read_quota_accepts_something_headers_can_be_taken_off():
    """The convenience the engine leans on: hand it the response or the error."""
    assert read_quota(Failure(ANTHROPIC_HEADERS), now=NOW).remaining == 9999


# --- the mapping shapes a real SDK hands over -------------------------------


class HeaderMapping:
    """An httpx-style multi-dict: iterating ``items()`` is the only contract."""

    def __init__(self, pairs) -> None:
        self._pairs = list(pairs)

    def items(self):
        return list(self._pairs)

    def get(self, name, default=None):
        for key, value in self._pairs:
            if key.lower() == name.lower():
                return value
        return default


def test_an_httpx_style_headers_mapping_reads_the_same_as_a_dict():
    mapping = HeaderMapping(ANTHROPIC_HEADERS.items())

    assert read_quota(mapping, now=NOW) == read_quota(ANTHROPIC_HEADERS, now=NOW)


def test_headers_of_accepts_an_httpx_style_mapping():
    mapping = HeaderMapping([("retry-after", "7")])

    assert headers_of(Raw(mapping)) is mapping
