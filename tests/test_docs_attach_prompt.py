"""Every command in the "Attach with an agent" prompt is a real one (SDK-2).

The prompt a coding agent follows is parsed: each ``$ `` command line is run (``pip install`` is checked against the
package's own name; ``python -m runbound check`` is run on a guarded script and must exit 0), each code line it
tells the agent to write is executed or parsed, and the gateway base-URL forms are matched. A command added to the
prompt without a rule here fails.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from runbound.policy import CAPABILITIES

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "guides" / "attach-with-an-agent.md"
PROMPT = re.search(r"````text\n(.*?)\n````", PAGE.read_text(), flags=re.S).group(1)
def prompt_commands() -> list[str]:
    found = []
    for line in PROMPT.splitlines():
        match = re.match(r"\s*\d+\.\s+\$ (.+)$", line)
        if match:
            found.append(match.group(1).strip())
    return found


GUARDED = """
    import runbound
    runbound.init(budget_usd=5.0, on_anomaly="raise")

    @runbound.tool(effects={"financial"})
    def issue_refund(user, amount):
        return "refunded"
"""


@pytest.fixture
def entry(tmp_path):
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(GUARDED))
    return str(path)


def test_the_prompt_has_the_commands_this_test_knows():
    assert prompt_commands() == [
        "pip install runbound",
        "python -m runbound check <the agent's entry script>",
        "python -m runbound check --json <the agent's entry script>",
    ]


def test_pip_install_names_the_package_this_tree_publishes():
    name = re.search(r'^name = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), flags=re.M).group(1)
    install = [c for c in prompt_commands() if c.startswith("pip install")]
    assert [shlex.split(c)[2] for c in install] == [name]


@pytest.mark.parametrize("command", [c for c in prompt_commands() if c.startswith("python -m runbound")])
def test_each_check_command_runs_and_exits_zero_on_a_guarded_script(command, entry):
    argv = shlex.split(re.sub(r"<[^>]*>", entry, command))
    assert argv[:3] == ["python", "-m", "runbound"]
    result = subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_check_command_exists_and_documents_itself():
    result = subprocess.run([sys.executable, "-m", "runbound", "check", "--help"], capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0 and "Exit 0" in result.stdout


def test_the_code_the_prompt_tells_the_agent_to_write_runs():
    init_line = re.search(r"runbound\.init\([^)]*\)", PROMPT).group(0)
    decorator = re.search(r"@runbound\.tool\([^)]*\)", PROMPT).group(0)
    program = f"import runbound\n{init_line}\n{decorator}\ndef issue_refund(u, a):\n    return 'ok'\nassert runbound.check(file=__import__('io').StringIO())['guarded']\n"
    ast.parse(program)
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    assert result.returncode == 0, result.stderr


def test_the_classes_the_prompt_lists_are_the_sdks_own():
    listed = re.search(r"Use the classes ([a-z, ]+)\.", PROMPT).group(1).replace(" and ", ", ")
    assert tuple(part.strip() for part in listed.split(",")) == tuple(CAPABILITIES)


@pytest.mark.parametrize("line", [l.strip() for l in PROMPT.splitlines() if "_BASE_URL=" in l])
def test_the_gateway_base_url_forms_are_the_gateways_own(line):
    assert re.fullmatch(r"(OPENAI|ANTHROPIC)_BASE_URL=\$RUNBOUND_URL/g/\$APP_TOKEN/(openai/v1|anthropic)", line)


def test_getting_started_shows_the_check_example_verbatim_and_its_command_works_on_it(tmp_path):
    """The page's python block is the executed example, and the page's `python -m runbound check agent.py` runs on it."""
    text = (ROOT / "docs" / "getting-started.md").read_text()
    lines = (ROOT / "examples" / "docs" / "check_process.py").read_text().splitlines()
    region = "\n".join(lines[next(i for i, l in enumerate(lines) if l.startswith("# docs: ")) + 1: lines.index("# /docs")]).strip("\n")
    assert region in re.findall(r"```python\n(.*?)\n```", text, flags=re.S)
    command = re.search(r"```bash\n(python -m runbound check agent\.py)[^\n]*\n```", text)
    assert command, "the page should show the command form"
    (tmp_path / "agent.py").write_text(region + "\n")
    result = subprocess.run([sys.executable, "-m", "runbound", "check", "agent.py"], capture_output=True, text=True, cwd=str(tmp_path),
                            env={"PYTHONPATH": str(ROOT), "PATH": ""}, timeout=60)
    assert result.returncode == 0 and "issue_refund" in result.stdout, result.stdout + result.stderr
