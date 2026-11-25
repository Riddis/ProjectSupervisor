"""A diagnosis `defer` now proposes a `plan`, never `new_task`/`wait_for`; `NEW_TASK_FIELDS`, `release_plan`,
`_release`, `_defer_question` and `job_not_applied` are gone from the code. This scans every module, tool,
template and document a caller could actually read for a live reference to one of them -- as opposed to a
test's own negative assertion, which must name a retired symbol to prove its absence and is excluded by name,
not by directory, so a stray new offender anywhere else still fails."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Directories a real reader (a plugin, an operator, another module) can reach: production code, documentation,
# scripts and the shipped examples/templates. `packages/` carries its own, unrelated `_wait_for_ready_pid` helper
# (an asyncio poll, nothing to do with a diagnosis defer) and is scanned too, under the identifier patterns only --
# never the loose substring patterns, which are not word-bounded.
SCAN_DIRS = ("supervisor", "docs", "tools", "examples", "plugins-catalog", "packages", "tests")

# Test files that legitimately name a retired symbol to prove it has no reader left; excluded by name, not by
# directory, so nothing else under tests/ gets a free pass.
ABSENCE_ASSERTIONS = {
    "test_diagnosis_plan.py",       # test_release_plan_and_new_task_fields_have_no_reader_left
    "test_r07_import_contract.py",  # asserts "job_not_applied" not in r
    "test_r11_typed_job.py",        # asserts "job_not_applied" not in r
    "test_retired_symbols_have_no_reader.py",         # this file
    "test_task_contract_matches_the_validator.py",    # asserts "new_task"/"wait_for" not in the doc
}

# Identifiers: never a live symbol anywhere outside the exclusions above.
IDENTIFIERS = ("NEW_TASK_FIELDS", "release_plan", "_defer_question", "job_not_applied")

# The retired defer shape's own field names, word-bounded so `_wait_for_ready_pid` (packages/) is not a false
# positive, and not preceded by a dot so `asyncio.wait_for` (an unrelated stdlib call, tests/) is not one either.
DEFER_WORDS = (re.compile(r"\bnew_task\b"), re.compile(r"(?<![.\w])wait_for\b"))


def _files():
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.name in ABSENCE_ASSERTIONS:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def test_no_retired_identifier_has_a_reader_left():
    offenders = []
    for path in _files():
        text = _read(path)
        if text is None:
            continue
        for name in IDENTIFIERS:
            if name in text:
                offenders.append(f"{path.relative_to(REPO)}: {name}")
    assert offenders == [], "retired symbol still referenced:\n" + "\n".join(offenders)


def test_no_reader_still_describes_a_defer_as_new_task_or_wait_for():
    offenders = []
    for path in _files():
        text = _read(path)
        if text is None:
            continue
        for pattern in DEFER_WORDS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(REPO)}: {pattern.pattern}")
    assert offenders == [], "the retired new_task/wait_for defer shape still has a reader:\n" + "\n".join(offenders)
