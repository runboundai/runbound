"""The plane-link page's wire is the client's: its endpoints, its counts, its hello and its event lanes.

A page that says "seven endpoints" and "four lanes" is a claim a reader checks against the table
beside it; this pins the words, the table and the code to one another, so adding an endpoint, a
hello field or a lane without the page fails here.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PAGE = (_ROOT / "docs" / "guides" / "plane-link.md").read_text()
_NUMBERS = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11}

#: The modules that open a request to the control plane (OTLP spans go to ``otel_endpoint``, not the plane).
_SENDERS = ("plane.py", "approvals.py", "quota.py")


def _source(name: str) -> str:
    return (_ROOT / "runbound" / name).read_text()


def _normalise(path: str) -> str:
    """``/v1/approvals/{approval_id}?wait_s={wait}`` -> ``/v1/approvals/{id}``."""
    return re.sub(r"\{[^}]*\}", "{id}", path.split("?")[0])


def _called() -> dict:
    """``{path: (method or None, module)}`` for every ``"/v1/..."`` string literal the senders hold."""
    called: dict = {}
    for module in _SENDERS:
        source = _source(module)
        for match in re.finditer(r'(?:"(GET|POST)",\s*)?f?"(/v1/[^"]*)"', source):
            path = _normalise(match.group(2))
            called.setdefault(path, (match.group(1), module))
            if match.group(1):
                called[path] = (match.group(1), module)
    return called


def _table_rows() -> list:
    """``(method, path, sender)`` for each row of the endpoint table."""
    rows = []
    for line in _PAGE.splitlines():
        match = re.match(r"\| `(GET|POST) (/v1/[^`]*)` \| `([a-z_]+\.py)` \|", line)
        if match:
            rows.append(match.groups())
    return rows


def test_the_endpoints_the_client_calls_are_the_rows_of_the_page_and_the_number_it_states():
    called = _called()
    rows = _table_rows()
    assert {path for _, path, _ in rows} == set(called)
    assert len(rows) == len(called)  # one row per endpoint
    for method, path, sender in rows:
        known_method, module = called[path]
        assert sender == module, f"{path} is sent by {module}, the page says {sender}"
        if known_method is not None:
            assert method == known_method, f"{path} is a {known_method}, the page says {method}"
    stated = {_NUMBERS[word] for word in re.findall(r"\b(\w+) endpoints\b", _PAGE) if word in _NUMBERS}
    assert stated == {len(called)}


def test_the_policy_fetch_is_a_get_and_the_heartbeat_jitters_by_ten_percent():
    plane = _source("plane.py")
    assert re.search(r'self\._call\("GET", path', plane) and '"/v1/policy?"' in plane
    assert "random.uniform(0.9, 1.1)" in plane and "±10%" in _PAGE


def test_every_field_the_hello_carries_is_on_the_page():
    shared = _source("shared.py")
    body = re.search(r"def _hello_payload\(self\).*?return payload\n", shared, re.S).group(0)
    tools = re.search(r"def _tools_payload\(self\).*?return \{\}\n", shared, re.S).group(0)
    fields = set(re.findall(r'"(\w+)":', body)) | set(re.findall(r'payload\["(\w+)"\]', body))
    fields |= set(re.findall(r'"(\w+)":', tools))
    assert {"service", "worker_id", "policy_version_seen", "rows_hash", "quota_held", "renew_claims",
            "tools_hash", "tools"} <= fields
    section = _PAGE.split("## The hello", 1)[1].split("\n## ", 1)[0]
    for field in fields:
        assert f"`{field}`" in section, field
    assert "at most 20" in section and "executing[:20]" in body


def test_the_lanes_of_the_events_batch_are_the_ones_the_page_names_and_counts():
    source = _source("export.py")
    body = re.search(r"batch = \{(.*?)\n        \}", source, re.S).group(1)
    lanes = set(re.findall(r'"(\w+)": (?:events|anomalies|\[wire)', body))
    assert lanes == {"events", "anomalies", "exits", "changes"}
    keys = set(re.findall(r'"(\w+)":', body))
    row = next(line for line in _PAGE.splitlines() if line.startswith("| `POST /v1/events`"))
    for key in keys:
        assert f"`{key}`" in row, key
    stated = {_NUMBERS[word] for word in re.findall(r"\b(\w+) lanes\b", row) if word in _NUMBERS}
    assert stated == {len(lanes)}
    section = _PAGE.split("## The events batch", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `(\w+)` \|", section, re.M))
    assert listed == lanes


def test_the_page_does_not_name_a_lane_that_was_removed():
    assert "circuits" not in _PAGE
    assert '"circuits"' not in _source("export.py")
