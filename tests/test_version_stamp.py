"""Release hygiene: the framework's three version readings agree (CONTRIBUTOR_RULING.md §8, the "new finding" at
v2.4.0 - a tag whose tree still said 2.3.0, because the version files were not bumped with the CHANGELOG entry).
Pinning this against the newest tag is deliberately not asserted: on a release branch HEAD is ahead of the newest
tag by design, and a test that cannot pass on the branch it guards is a test that gets deleted."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b")


def _changelog_top_version() -> str:
    """The first version heading in CHANGELOG.md, skipping `## Unreleased`."""
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(1)
    raise AssertionError("CHANGELOG.md has no version heading")


def _dunder_version() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (ROOT / "supervisor" / "__init__.py").read_text(encoding="utf-8"),
                 re.MULTILINE)
    assert m, "supervisor/__init__.py has no __version__ = \"...\" line"
    return m.group(1)


def test_version_is_the_same_everywhere():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = pyproject["project"]["version"]
    assert _dunder_version() == pyproject_version
    assert _changelog_top_version() == pyproject_version
