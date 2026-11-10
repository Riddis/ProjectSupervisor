"""docs/reviews/RELEASE_REVIEW_2.5.0.md P3-5: `ruff check` over the Python files this release range touched must
report no finding on a line the range added. Scoped to those files, never to the whole tree, because `ruff check .`
from the worktree root cannot even parse `supervisor/plugin_template/pyproject.toml` (a scaffold with literal Jinja
placeholders) - `VERIFY_LOOP_RECORD.md` section 4's own standing gap, carried forward to T4-30's list, not this
test's job to close.

"Added" rests on `git blame` against BASE_SHA for every line ruff currently flags, never on a cached line number:
T4-41..T4-47 already moved several of the six findings round 1 first named, by appending further commits to the
same range (`c4e6f963..HEAD`, HEAD still moving while the tree stays 2.5.0) - trusting a round's own line numbers
is exactly how a later insertion would silently stop checking the line it meant to pin.

Self-retires once the tree's `__version__` moves past 2.5.0, the same shape as `test_changelog_entries_2_5_0.py`:
an ordinary post-release commit touching one of these files for an unrelated reason should not turn a required
gate red for a scope this file was never meant to cover again. Delete this file at that point."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "c4e6f963d80989d4e5044f4327f0ca8aefce6a50"  # v2.4.0, the branch point


def _current_version() -> str | None:
    match = re.search(
        r'__version__\s*=\s*"([^"]+)"',
        (ROOT / "supervisor" / "__init__.py").read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


pytestmark = pytest.mark.skipif(
    _current_version() != "2.5.0",
    reason="test_ruff_lines_this_range_added.py is specific to the 2.5.0 release range and self-retires once the "
           "tree's __version__ moves past 2.5.0 - delete this file at that point",
)

_LINE_RE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")
_FINDING_RE = re.compile(r"^([^:]+):(\d+):(\d+): (\S+)")


def _ruff_command() -> list[str] | None:
    """The worktree's own `ruff.exe`/`ruff` by path when its `.venv` has one (the dev extras install it there,
    and a direct path sidesteps whatever interpreter is actually driving this pytest session); `python -m ruff`
    through the running interpreter only once a cheap `--version` probe proves it is actually importable; `None`
    otherwise, so a tree with neither is skipped rather than silently "passing" because ruff itself could not run
    (an empty, unparsed stdout from a failed launch looks identical to a clean ruff run unless this is checked)."""
    for candidate in (ROOT / ".venv" / "Scripts" / "ruff.exe", ROOT / ".venv" / "bin" / "ruff"):
        if candidate.exists():
            return [str(candidate)]
    probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
    if probe.returncode == 0:
        return [sys.executable, "-m", "ruff"]
    return None


def _ranged_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", f"{BASE_SHA}..HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"cannot read the range {BASE_SHA}..HEAD (e.g. a shallow clone where the base sha is "
            f"unreachable): {result.stderr.strip()}"
        )
    return [p for p in result.stdout.splitlines() if p.endswith(".py") and (ROOT / p).is_file()]


def _lines_added_by_the_range(path: str) -> set[int]:
    """Every line `path` currently holds whose last `git blame` commit descends from BASE_SHA (and is not BASE_SHA
    itself) - the range's own definition of "added", never a round's own cached line number."""
    blame = subprocess.run(
        ["git", "-C", str(ROOT), "blame", "--porcelain", path],
        capture_output=True, text=True,
    )
    if blame.returncode != 0:
        pytest.skip(f"cannot blame {path}: {blame.stderr.strip()}")
    commits_by_line: dict[int, str] = {}
    for entry in blame.stdout.splitlines():
        match = _LINE_RE.match(entry)
        if match:
            commits_by_line[int(match.group(2))] = match.group(1)
    added = set()
    checked: dict[str, bool] = {}
    for line_no, sha in commits_by_line.items():
        if sha not in checked:
            if sha == BASE_SHA:
                checked[sha] = False
            else:
                anc = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", BASE_SHA, sha])
                checked[sha] = anc.returncode == 0
        if checked[sha]:
            added.add(line_no)
    return added


def test_ruff_check_reports_no_finding_on_a_line_this_range_added():
    files = _ranged_python_files()
    if not files:
        pytest.skip("no Python file in the range")
    ruff_command = _ruff_command()
    if ruff_command is None:
        pytest.skip("no ruff.exe in the worktree's own .venv and ruff is not importable from this interpreter")
    proc = subprocess.run(
        [*ruff_command, "check", "--output-format", "concise", *files],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    added_lines = {f: _lines_added_by_the_range(f) for f in files}
    flagged_on_added_lines = []
    for line in proc.stdout.splitlines():
        match = _FINDING_RE.match(line)
        if not match:
            continue
        raw_path, line_no = match.group(1), int(match.group(2))
        path = raw_path.replace("\\", "/")
        if line_no in added_lines.get(path, set()):
            flagged_on_added_lines.append(line)
    assert flagged_on_added_lines == [], "\n".join(flagged_on_added_lines) + "\n\nfull output:\n" + proc.stdout
