"""docs/reviews/RELEASE_REVIEW_2.5.0.md P2-3: the ## 2.5.0 heading T4-43 stamped must carry one entry per
user-visible change in c4e6f963..HEAD, not stay the placeholder sentence T4-43 left it with. This is a check on
presence and shape, not on prose: it asserts a stable keyword per change and that every feature-bearing path the
range touches is named by some entry, not that any particular sentence survives unchanged.

The range's own end, HEAD, keeps moving while the tree is still 2.5.0, so the module self-retires instead of
pinning a fixed endpoint: every test here is skipped once the tree's own `__version__` is no longer "2.5.0" (the
`pytestmark` below), so an ordinary post-release commit that touches a path this file has no keyword for cannot
turn a required gate red for a change this file was never meant to describe. Delete this file once that skip is
permanent."""
from __future__ import annotations

import re
import subprocess
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
    reason="test_changelog_entries_2_5_0.py is specific to the 2.5.0 release range and self-retires once the "
           "tree's __version__ moves past 2.5.0 - delete this file at that point",
)

_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+|Unreleased)(?:\s+—\s+\d{4}-\d{2}-\d{2})?\s*$")

# Paths this range touches that carry no behaviour of their own: the version-meta files T4-43's own stamp
# commit moved, and the test files every entry's own regression lives in.
_META_EXACT = {"CHANGELOG.md", "pyproject.toml", "supervisor/__init__.py"}
_META_PREFIXES = ("tests/",)

# Judged not user-visible: an editorial fix of two stale sentences in already-shipped skill files (f017fd1,
# "the catalog is built, not phase 6 - fix the two stale skill lines"). No command, no refusal and no shipped
# behaviour changed; only two cross-references were corrected.
_NOT_USER_VISIBLE = {
    ".claude/skills/plugin-release/SKILL.md",
    ".claude/skills/supervisor-setup/SKILL.md",
}

# One or more substrings, any of which being present in the ## 2.5.0 block counts as "this path is named".
_PATH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "CONTRIBUTING.md": ("CONTRIBUTING.md",),
    ".github/pull_request_template.md": ("pull-request template",),
    ".github/CODEOWNERS": ("CODEOWNERS",),
    ".github/workflows/ci.yml": ("ci.yml",),
    "plugins-catalog/index.yaml": ("plugins-catalog/index.yaml",),
    "plugins-catalog/README.md": ("plugins-catalog/README.md",),
    "tools/catalog_check.py": ("catalog_check.py",),
    ".claude/skills/catalog-review/SKILL.md": ("catalog-review",),
    "tools/ci_package.py": ("ci_package.py", "SOURCE_DATE_EPOCH"),
    "supervisor/boundary.py": ("supervisor/boundary.py", "supervisor.boundary.check()"),
    "supervisor/loop.py": ("supervisor/loop.py",),
    "supervisor/verify.py": ("supervisor/verify.py",),
    "supervisor/packets.py": ("supervisor/packets.py",),
}

# One stable keyword per user-visible change in the range, each naming something the change actually did -
# a function, an identifier, a log event, a job name - not a sentence this file pins.
_CHANGE_KEYWORDS = (
    "CONTRIBUTING.md",
    "pull-request template",
    "CODEOWNERS",
    "plugins-catalog/index.yaml",
    "catalog_check.py",
    "catalog-review",
    "SOURCE_DATE_EPOCH",
    "newest_in_range()",
    "all_gates",
    "ubuntu-latest",
    "G5 reference plugin",
    "residue_reverted",
    "sweep_stalled",
    "test_argv",
    "layout.root",
    "supervisor.boundary.check()",
)


def _ranged_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--name-only", "--format=", f"{BASE_SHA}..HEAD"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip(
            f"cannot read the range {BASE_SHA}..HEAD (e.g. a shallow clone where the base sha is "
            f"unreachable): {result.stderr.strip()}"
        )
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _changelog_text() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _block_2_5_0() -> str:
    lines = _changelog_text().splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING.match(line)]
    assert starts, "CHANGELOG.md has no version heading at all"
    block_start = next(i for i in starts if _HEADING.match(lines[i]).group(1) == "2.5.0")
    later = [i for i in starts if i > block_start]
    block_end = later[0] if later else len(lines)
    return "\n".join(lines[block_start + 1:block_end])


def test_the_2_5_0_block_is_not_the_placeholder_sentence():
    block = _block_2_5_0()
    assert block.strip(), "the ## 2.5.0 block is empty"
    assert "written here before it ships" not in block, (
        "the ## 2.5.0 block is still T4-43's placeholder sentence, not this release's own entries"
    )


def test_every_change_keyword_is_named_in_the_2_5_0_block():
    block = _block_2_5_0()
    missing = [kw for kw in _CHANGE_KEYWORDS if kw not in block]
    assert not missing, f"the ## 2.5.0 block names no entry for: {missing!r}"


def test_every_feature_bearing_path_in_the_range_is_named_by_some_entry():
    block = _block_2_5_0()
    files = _ranged_files()
    assert files, "git log --name-only over the range reported no files at all"

    feature_bearing = [
        f for f in files
        if f not in _META_EXACT
        and not any(f.startswith(p) for p in _META_PREFIXES)
        and f not in _NOT_USER_VISIBLE
    ]
    assert feature_bearing, "no feature-bearing path survived the exclusions - the filters are too wide"

    unmapped = [f for f in feature_bearing if f not in _PATH_KEYWORDS]
    assert not unmapped, f"no keyword mapping exists for: {unmapped!r} - add one to _PATH_KEYWORDS"

    unmentioned = [
        f for f in feature_bearing
        if not any(kw in block for kw in _PATH_KEYWORDS[f])
    ]
    assert not unmentioned, f"these feature-bearing paths are named by no ## 2.5.0 entry: {unmentioned!r}"


def test_not_user_visible_paths_are_still_in_the_ranges_own_file_list():
    files = set(_ranged_files())
    stale = _NOT_USER_VISIBLE - files
    assert not stale, f"these paths are no longer in the range at all, drop them from _NOT_USER_VISIBLE: {stale!r}"
