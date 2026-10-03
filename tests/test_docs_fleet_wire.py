"""The fleet-mode page's counts are the client's: its endpoints and its event lanes.

A page that says "seven endpoints" and "five lanes" is a claim a reader checks
against the table beside it; this pins the words, the table and the code to
one another, so adding an endpoint or a lane without the page fails here.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PAGE = (_ROOT / "docs" / "guides" / "fleet-mode.md").read_text()
_NUMBERS = {"five": 5, "six": 6, "seven": 7, "eight": 8}


def _table_rows() -> list[str]:
    return [line for line in _PAGE.splitlines() if re.match(r"\| `(GET|POST) /v1/", line)]


def test_the_endpoints_the_client_calls_are_the_rows_of_the_page_and_the_number_it_states():
    source = (_ROOT / "runbound" / "plane.py").read_text()
    called = set(re.findall(r'"(/v1/[a-z]+)', source))
    listed = {re.search(r"/v1/[a-z]+", row).group(0) for row in _table_rows()}
    assert called == listed
    stated = {_NUMBERS[word] for word in re.findall(r"\b(\w+) endpoints\b", _PAGE) if word in _NUMBERS}
    assert stated == {len(called)}


def test_the_lanes_of_the_events_batch_are_the_ones_the_page_names_and_counts():
    source = (_ROOT / "runbound" / "export.py").read_text()
    body = re.search(r'batch = \{(.*?)\n        \}', source, re.S).group(1)
    lanes = set(re.findall(r'"(\w+)": (?:events|anomalies|\[wire)', body))
    assert lanes == {"events", "anomalies", "exits", "circuits", "changes"}
    row = next(line for line in _table_rows() if "/v1/events" in line)
    for lane in lanes:
        assert f"`{lane}`" in row
    assert "five lanes" in row
