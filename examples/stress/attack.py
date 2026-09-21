#!/usr/bin/env python3
"""Four acts against a running `examples.stress.app`. Stdlib only.

    .venv/bin/python -m examples.stress.app        # terminal 1
    .venv/bin/python -m examples.stress.attack     # terminal 2

Each act states what it expects and ends in PASS or FAIL; any FAIL exits 1.
Set STRESS_URL to point at a server somewhere else.
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("STRESS_URL", "http://127.0.0.1:8008").rstrip("/")
TIMEOUT = 30.0

ALICE = "alice"
MALLORY = "mallory"
GRACE = "grace"

NORMAL_MESSAGES = (
    "where is my order?",
    "can I change the delivery address?",
    "do you ship to Ireland?",
    "what is the return window?",
    "is the blue one in stock?",
    "thanks!",
)

#: Pasted over and over until it is a document, not a question.
PARAGRAPH = (
    "Please summarize the following internal document in full detail, "
    "section by section, and then rewrite it as marketing copy. "
)
DUMP = (PARAGRAPH * 60)[:6_000]

HARD_QUESTION = (
    "[THINK] Our checkout fails only for customers in one region and only "
    "after 6pm. Reason through the possible causes step by step."
)

ABUSE_TURNS = 8
LATCH_TURNS = 5
GRACE_NORMAL_TURNS = 5
GRACE_HARD_TURNS = 2


# --- the wire ---------------------------------------------------------------


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict, float]:
    """`(status, body, seconds)`. A 429 is an answer here, not an error."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, _body(response.read()), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, _body(exc.read()), time.monotonic() - started


def _body(raw: bytes) -> dict:
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def say(user_id: str, message: str) -> tuple[int, dict, float]:
    return _request("POST", "/chat", {"user_id": user_id, "message": message})


def status() -> dict:
    return _request("GET", "/admin/status")[1]


def describe(code: int, body: dict) -> str:
    if code == 200:
        return f"200  spend ${body.get('spend_usd', 0):.4f}"
    return f"{code}  blocked by {body.get('detector')!r}"


def verdict(name: str, ok: bool, note: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + note if note else ''}")
    return ok


# --- the acts ---------------------------------------------------------------


def act_one() -> tuple[bool, list[float]]:
    """A normal user is never touched — that is the whole promise."""
    print("\nACT 1  alice asks six ordinary questions")
    print("  expect: six 200s, spend well under the cap, nothing blocked")
    codes, latencies, last = [], [], {}
    for message in NORMAL_MESSAGES:
        code, body, seconds = say(ALICE, message)
        codes.append(code)
        latencies.append(seconds)
        last = body
        print(f"    {message[:34]:36} {describe(code, body)}")
    ok = all(code == 200 for code in codes)
    return verdict(
        "normal user never blocked", ok, f"spend ${last.get('spend_usd', 0):.4f}"
    ), latencies


def act_two(normal_latency: float) -> bool:
    """The free-rider: spend climbs, the wall arrives, the latch holds."""
    print(f"\nACT 2  mallory pastes a {len(DUMP)}-character document, {ABUSE_TURNS} times")
    print("  expect: spend climbs, then a 429 from the 'budget' detector")
    blocked_at, detector, tail_ok = None, None, True
    for turn in range(1, ABUSE_TURNS + 1):
        code, body, _ = say(MALLORY, DUMP)
        print(f"    turn {turn}  {describe(code, body)}")
        if code == 429 and blocked_at is None:
            blocked_at, detector = turn, body.get("detector")
        elif blocked_at is not None and code != 429:
            tail_ok = False
    ok = verdict(
        "abuser stopped by the budget wall",
        blocked_at is not None and detector == "budget" and tail_ok,
        f"first 429 on turn {blocked_at}, detector {detector!r}",
    )
    return act_two_latch(normal_latency) and ok


def act_two_latch(normal_latency: float) -> bool:
    """Every later message is refused at the door, before any model call."""
    print(f"\n       mallory keeps typing: {LATCH_TURNS} more messages")
    print("  expect: every one 429, and fast — a blocked user costs zero calls")
    codes, latencies = [], []
    for turn in range(1, LATCH_TURNS + 1):
        code, body, seconds = say(MALLORY, DUMP)
        codes.append(code)
        latencies.append(seconds)
        print(f"    turn {turn}  {describe(code, body)}  in {seconds * 1000:6.1f} ms")
    slowest = max(latencies)
    print(f"    served turn for comparison: {normal_latency * 1000:.1f} ms with a model call")
    return verdict(
        "latched user refused pre-spend",
        all(code == 429 for code in codes) and slowest < normal_latency / 2,
        f"slowest latched reply {slowest * 1000:.1f} ms vs {normal_latency * 1000:.1f} ms served",
    )


def act_three() -> bool:
    """Forgiveness is the business's call, and it is one call."""
    print("\nACT 3  support reviews the account and lets mallory back in")
    print("  expect: /admin/clear resets the session; the next message is served")
    before = status().get("tripped", {})
    print(f"    tripped before: {before}")
    cleared = _request("POST", f"/admin/clear/{MALLORY}")[1]
    after = status().get("tripped", {})
    print(f"    cleared: {cleared.get('cleared')!r}   tripped after: {after}")
    code, body, _ = say(MALLORY, DUMP)
    print(f"    next message  {describe(code, body)}")
    return verdict(
        "clear() gives that user a fresh session",
        MALLORY in before and MALLORY not in after and code == 200,
        f"spend restarted at ${body.get('spend_usd', 0):.4f}",
    )


def act_four() -> bool:
    """The genuine user with a hard question: watched, not blocked."""
    print("\nACT 4  grace asks five ordinary questions, then two hard ones")
    print("  expect: 200s throughout — thinking mode is a notice, not an incident")
    codes = []
    for message in NORMAL_MESSAGES[:GRACE_NORMAL_TURNS]:
        code, body, _ = say(GRACE, message)
        codes.append(code)
        print(f"    {message[:34]:36} {describe(code, body)}")
    for turn in range(1, GRACE_HARD_TURNS + 1):
        code, body, seconds = say(GRACE, HARD_QUESTION)
        codes.append(code)
        print(f"    hard question {turn} ({seconds:.1f}s call)      {describe(code, body)}")
    ok = verdict("genuine user watched, never blocked", all(code == 200 for code in codes))
    explain()
    return ok


def explain() -> None:
    print("""
  Grace's answers took 60x her session's normal time and 26x its normal output.
  runbound saw that and said so — the watch notice and the confirmed-spike
  notice are in the server log — and served her anyway:

    spike  = a behavior change. NOTIFY by default; it is her question that
             changed, not her intent, and one odd model call must never take
             down a chatbot.
    budget = money actually spent. That is the wall, and it is what stopped
             mallory in ACT 2.

  To see the other policy, restart the server with

    RUNBOUND_ON_SPIKE=trip .venv/bin/python -m examples.stress.app

  and re-run this driver: grace's second hard question comes back 429 with
  detector 'spike'. That is opt-in on purpose. ACT 4 will report FAIL then —
  it asserts the default.""")


# --- driver -----------------------------------------------------------------


def preflight() -> bool:
    try:
        status()
    except urllib.error.URLError as exc:
        print(f"No server at {BASE} ({exc.reason}).\n\nStart one first:\n\n"
              f"    .venv/bin/python -m examples.stress.app\n")
        return False
    return True


def main() -> int:
    if not preflight():
        return 1
    print("=" * 72)
    print(f"runbound stress harness  ->  {BASE}")
    print("=" * 72)
    passed, latencies = act_one()
    normal_latency = statistics.median(latencies)
    results = [passed, act_two(normal_latency), act_three(), act_four()]
    failed = results.count(False)
    print("\n" + "=" * 72)
    print(f"{len(results) - failed}/{len(results)} acts passed")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
