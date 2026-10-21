"""Every install line the docs, the README and the examples give installs the 1.x SDK.

1.0.0aN are PRE-releases: a plain ``pip install runbound`` (or ``pip install "runbound[otel]"``) installs 0.11.2, which ignores console rules, and uv does the same unless it is told
``--prerelease allow``. A line a new user copies has to say ``--pre`` (or pin a 1.x version). Prose that explains the trap, not an instruction, is allowed by name."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "README.md", ROOT / "docs", ROOT / "examples", ROOT / "runbound" / "otel.py"]
INSTALL = re.compile(r"(?:uv\s+)?pip3?\s+install\b([^`\n]*)")
#: Sentences that explain why a plain install is wrong (they quote the plain command on purpose).
EXPLAINS = ("still installs 0.11.2", "installs today", "pip install runbound` installs", "without --pre you get 0.11.2")


def sources():
    for entry in FILES:
        yield from ([entry] if entry.is_file() else sorted(p for p in entry.rglob("*") if p.suffix in (".md", ".py")))


def offenders() -> list:
    found = []
    for path in sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in INSTALL.finditer(line):
                command = match.group(1)
                if "runbound" not in command or "runbound-" in command or "-e " in command or any(text in line for text in EXPLAINS):
                    continue
                if "--pre" in command or "--prerelease" in command or re.search(r"runbound[^\s\"']*\s*(==|>=)\s*1\.", command) or re.search(r"runbound\[[^\]]*\](==|>=)1\.", command):
                    continue
                if re.search(r"""["']?runbound\.""", command):
                    continue
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}")
    return found


def test_every_runbound_install_line_asks_for_the_pre_release_or_pins_a_1x_version():
    assert not offenders(), "an install line that would install 0.11.2:\n" + "\n".join(offenders())
