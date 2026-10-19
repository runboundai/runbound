"""PS-DEDUPE-1: the span exporter's batch identity. Every post carries ``X-Runbound-Batch: <boot_id>.<exporter_id>.<seq>[.p<N>]``, minted once per batch; a batch that fails
retryably is an immutable in-flight unit (token + the exact bytes) resent first, byte for byte; a 413 splits it into fixed ordered parts; one flusher at a time.

No sleeps and no network: ``urlopen`` is replaced by a scripted endpoint that records each request's header and body.
"""

import gzip
import json
import threading
import urllib.error

import pytest

from runbound import otlp


class Endpoint:
    """Answers each post from a script of statuses (200 after the script runs out) and records ``(token, body bytes)``."""

    def __init__(self, *script):
        self.script, self.posts = list(script), []
        self.gate = None  # an Event that blocks a post, to hold one flusher inside its post

    def __call__(self, request, timeout=None):
        self.posts.append((request.get_header("X-runbound-batch"), request.data))
        if self.gate is not None:
            self.gate.wait(5)
        status = self.script.pop(0) if self.script else 200
        if status == "net":
            raise urllib.error.URLError("down")
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, None)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        return Response()

    def spans(self, index):
        body = json.loads(gzip.decompress(self.posts[index][1]))
        return [s["n"] for rs in body["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


@pytest.fixture
def endpoint(monkeypatch):
    fake = Endpoint()
    monkeypatch.setattr(otlp, "urlopen", fake)
    return fake


def exporter(**kw):
    return otlp.SpanExporter("http://127.0.0.1:9", "k", {}, "0", batch_size=kw.pop("batch_size", 2), **kw)


def feed(exp, *numbers):
    for n in numbers:
        exp.add({"n": n})


def test_every_post_carries_a_token_made_of_boot_exporter_and_a_sequence_from_zero(endpoint):
    exp = exporter()
    feed(exp, 1, 2, 3, 4, 5)
    exp.flush()
    tokens = [t for t, _ in endpoint.posts]
    assert len(tokens) == 3 and all(t for t in tokens)
    boot, ident, seq = tokens[0].split(".")
    assert boot == otlp._BOOT_ID and len(ident) == 8 and [t.split(".")[2] for t in tokens] == ["0", "1", "2"]
    assert {t.rsplit(".", 1)[0] for t in tokens} == {f"{boot}.{ident}"}


def test_a_retried_batch_is_resent_first_byte_for_byte_under_the_same_token_and_new_spans_wait(endpoint):
    endpoint.script = ["net"]
    exp = exporter()
    feed(exp, 1, 2)
    exp.flush()  # fails: the batch is now an in-flight unit
    feed(exp, 3, 4, 5)  # new spans arrive while it is stuck
    exp.flush()
    first, resent, after = endpoint.posts[0], endpoint.posts[1], endpoint.posts[2:]
    assert first == resent, "same token, same bytes"
    assert endpoint.spans(1) == [1, 2] and [endpoint.spans(i) for i in (2, 3)] == [[3, 4], [5]]
    assert all(t != first[0] for t, _ in after)


def test_the_drop_oldest_rule_never_touches_an_in_flight_unit(endpoint):
    endpoint.script = ["net"]
    exp = exporter(maxlen=3)
    feed(exp, 1, 2)
    exp.flush()
    feed(exp, 3, 4, 5, 6, 7, 8)  # the queue (max 3) overflows behind the stuck unit
    assert exp.dropped == 3 and exp.pending == 2 + 3
    exp.flush()
    assert endpoint.spans(1) == [1, 2], "the unit that was in flight is intact"


def test_stop_during_a_retry_and_the_exporter_thread_never_produce_two_bodies_for_one_token(endpoint):
    endpoint.script = ["net"]
    exp = exporter()
    feed(exp, 1, 2)
    exp.flush()  # in flight
    endpoint.gate = threading.Event()
    first = threading.Thread(target=exp.flush, args=(5,))
    first.start()
    for _ in range(2_000_000):  # the first flusher is inside its resend
        if len(endpoint.posts) >= 2:
            break
    second = threading.Thread(target=exp.stop, args=(0.2,))  # stop() arrives: it cannot flush while the other one holds the unit
    second.start()
    second.join(2)
    endpoint.gate.set()
    first.join(5)
    by_token = {}
    for token, body in endpoint.posts:
        by_token.setdefault(token, set()).add(body)
    assert all(len(bodies) == 1 for bodies in by_token.values()), "one body per token"
    assert len(endpoint.posts) == 2


def test_a_413_halves_a_unit_into_ordered_parts_with_stable_tokens_and_bytes(endpoint):
    endpoint.script = [413, "net", 200, 200, 200]
    exp = exporter(batch_size=8)
    feed(exp, *range(8))
    exp.flush()  # 413 -> two halves; the first half then fails retryably
    exp.flush()  # the first half is resent, then the second
    posts = endpoint.posts
    parent = posts[0][0]
    assert [t for t, _ in posts[1:]] == [f"{parent}.p0", f"{parent}.p0", f"{parent}.p1"]
    assert posts[1] == posts[2], "a part's retry is the same token and the same bytes"
    assert [endpoint.spans(i) for i in (1, 3)] == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert exp.sent == 8 and exp.dropped == 0


def test_a_part_refused_again_is_halved_again_with_a_further_suffix(endpoint):
    endpoint.script = [413, 413]  # the batch is too large; then its first half is too
    exp = exporter(batch_size=8)
    feed(exp, *range(8))
    exp.flush()
    parent = endpoint.posts[0][0]
    assert [t for t, _ in endpoint.posts] == [parent, f"{parent}.p0", f"{parent}.p0.p0", f"{parent}.p0.p1", f"{parent}.p1"]
    assert [endpoint.spans(i) for i in range(1, 5)] == [[0, 1, 2, 3], [0, 1], [2, 3], [4, 5, 6, 7]]
    assert exp.sent == 8 and exp.dropped == 0


def test_a_part_refused_at_the_depth_limit_and_a_single_span_are_dropped_once_with_one_truthful_warning(endpoint, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="runbound")
    endpoint.script = [413] * 5  # 32 -> 16 -> 8 -> 4 -> 2 spans, and the last is still refused at depth 4
    exp = exporter(batch_size=32)
    feed(exp, *range(32))
    exp.flush()
    assert exp.dropped == 2 and exp.sent == 30 and exp.pending == 0, "counted once; every other part went"
    refusals = [r.getMessage() for r in caplog.records if "too large" in r.getMessage()]
    final = [m for m in refusals if "cannot be split further" in m]
    assert len(final) == 1 and "dropping them" in final[0] and "already halved 4 times" in final[0], refusals
    assert all("halves" not in m for m in final), "the drop is not described as a split"
    caplog.clear()
    single = exporter()
    endpoint.script = [413]
    feed(single, 9)
    single.flush()
    assert single.dropped == 1 and single.sent == 0 and single.pending == 0
    assert any("a single span" in r.getMessage() and "dropping them" in r.getMessage() for r in caplog.records)


def test_new_spans_are_never_batched_into_a_part(endpoint):
    endpoint.script = [413, "net"]
    exp = exporter(batch_size=4)
    feed(exp, 1, 2, 3, 4)
    exp.flush()
    feed(exp, 5)
    exp.flush()
    parts = [endpoint.spans(i) for i in range(len(endpoint.posts)) if endpoint.posts[i][0].count(".") == 3]
    assert parts and all(5 not in p for p in parts) and endpoint.spans(len(endpoint.posts) - 1) == [5]


def test_two_exporters_in_one_process_never_share_a_token_and_a_restart_is_a_new_boot(endpoint):
    a, b = exporter(), exporter()
    feed(a, 1, 2)
    feed(b, 1, 2)
    a.flush()
    b.flush()
    assert endpoint.posts[0][0] != endpoint.posts[1][0]
    assert endpoint.posts[0][0].endswith(".0") and endpoint.posts[1][0].endswith(".0"), "both start at seq 0: only the exporter id tells them apart"
    restarted = exporter(boot_id="another-boot")
    feed(restarted, 1, 2)
    restarted.flush()
    assert endpoint.posts[2][0].startswith("another-boot.") and endpoint.posts[2][0] != endpoint.posts[0][0]


def test_a_build_that_disables_the_header_never_sends_it(endpoint):
    exp = exporter(batch_header=False)
    feed(exp, 1, 2)
    exp.flush()
    assert endpoint.posts[0][0] is None and endpoint.spans(0) == [1, 2]


def test_counters_keep_their_meaning(endpoint):
    endpoint.script = [200, 400, 200]
    exp = exporter()
    feed(exp, 1, 2, 3, 4, 5)
    exp.flush()
    assert exp.sent == 3 and exp.dropped == 2 and exp.pending == 0
