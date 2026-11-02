"""The release rule for a promoted package: the manifest's `version`, the `[project] version` and the newest
`CHANGELOG.md` heading must all name the same released version, and that version must be at least 1.0.0 - never
a scaffold's 0.x (the literal string is not asserted; a later release changes the value, not the rule). Read
without importing anything when the source tree ships beside `tests/` (no framework or plugin install needed);
once `tools/ci_package.py` step 4 has copied only `tests/` next to an installed wheel, there is no sibling
`pyproject.toml` or package folder to read, so the installed module and its metadata - both built from that same
source - stand in for them."""
from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path

import tomllib

DISTRIBUTION = "supervisor-plugin-review-rounds"
PACKAGE = "supervisor_plugin_review_rounds"
HERE = Path(__file__).resolve().parent
PYPROJECT = HERE.parent / "pyproject.toml"
CHANGELOG = HERE.parent / "CHANGELOG.md"


def _manifest_path() -> Path:
    sibling = HERE.parent / PACKAGE / "supervisor-plugin.toml"
    if sibling.is_file():
        return sibling
    return Path(import_module(PACKAGE).__file__).resolve().parent / "supervisor-plugin.toml"


def _project_version() -> str:
    if PYPROJECT.is_file():
        return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    return metadata.version(DISTRIBUTION)


def test_version_is_1_0_0_or_later_and_agrees_with_the_manifest_and_changelog():
    manifest_version = tomllib.loads(_manifest_path().read_text(encoding="utf-8"))["version"]
    project_version = _project_version()
    assert manifest_version == project_version
    assert tuple(int(p) for p in manifest_version.split(".")[:3]) >= (1, 0, 0)
    if CHANGELOG.is_file():
        heading = next(line for line in CHANGELOG.read_text(encoding="utf-8").splitlines()
                       if line.startswith("## "))
        assert heading.strip() == f"## {project_version}"
