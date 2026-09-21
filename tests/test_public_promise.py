"""The front door's promise, checked the way a stranger reading it would.

README.md is the package's front door and was deliberately cut to a short
length so a reader finishes it; this is the guard that keeps it from
creeping back past that on the next edit. The promise sentence — what one
`init()` line buys you, with nothing else required — is meant to appear
word-for-word in two places: the README, and the docs page a reader lands on
next. A paraphrase in only one of the two is the kind of drift nothing else
in this suite would catch, since both are prose, not code either test can
import and compare structurally.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_README = _ROOT / "README.md"
_GETTING_STARTED = _ROOT / "docs" / "getting-started.md"

#: Exact wording required near the top of README.md and repeated verbatim on
#: at least one docs page. Never "no code modification" or "zero code
#: changes" anywhere in the public tree — both read as a stronger claim than
#: this SDK makes (a tool still needs `@runbound.tool` to be governed).
PROMISE_SENTENCE = (
    "Add one initialization line. No agent rewrites, decorators, or policy "
    "code required."
)

#: Overclaiming phrasing the promise must never be reworded into.
FORBIDDEN_PHRASES = ("no code modification", "zero code changes")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_readme_stays_under_two_hundred_lines():
    lines = _lines(_README)
    assert len(lines) < 200, f"README.md has grown to {len(lines)} lines (must stay under 200)"


def test_promise_sentence_appears_verbatim_in_readme_and_getting_started():
    readme_text = _README.read_text(encoding="utf-8")
    getting_started_text = _GETTING_STARTED.read_text(encoding="utf-8")

    # The sentence wraps across source lines in the Markdown; collapse
    # whitespace the same way a reader's eye would before comparing.
    def normalized(text: str) -> str:
        return " ".join(text.split())

    assert normalized(PROMISE_SENTENCE) in normalized(readme_text), (
        "README.md must contain the promise sentence verbatim"
    )
    assert normalized(PROMISE_SENTENCE) in normalized(getting_started_text), (
        "docs/getting-started.md must contain the promise sentence verbatim"
    )


def test_forbidden_overclaims_never_appear_in_the_public_tree():
    files = list((_ROOT / "docs").rglob("*.md")) + [_README]
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for phrase in FORBIDDEN_PHRASES:
            if phrase in text:
                offenders.append(f"{path.relative_to(_ROOT)}: {phrase!r}")
    assert not offenders, "overclaiming phrase(s) found:\n" + "\n".join(offenders)


def test_the_two_honest_limits_sit_within_twelve_lines_of_the_level_one_list():
    lines = _lines(_README)

    list_end = next(
        i for i, line in enumerate(lines) if line.strip() == "- local enforcement"
    )
    honest_limits = next(
        i for i, line in enumerate(lines) if line.startswith("Two honest limits")
    )

    assert honest_limits > list_end, "the honest limits must follow the Level 1 list"
    assert honest_limits - list_end <= 12, (
        f"the two honest limits sit {honest_limits - list_end} lines after the "
        "Level 1 list (must be within 12)"
    )
