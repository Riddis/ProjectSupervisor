"""M4 (docs/releases/2.5.0/CONTRIBUTOR_RULING.md §3.4, after M7): the package job proves every leg on both
platforms, the axis `docs/releases/packages/CI_PLATFORM.md` section 5 names as the low-cost option once M7 has
made the context count affordable. `tools/ci_package.py matrix()` is unchanged - still `{"include": [...]}`
(tests/test_ci_package.py) - this is a workflow-only change: an `os` axis crossed with the dynamic leg list.

A plain `os: [...]` array crossed with an `include:` list whose entries carry no `os` key does not multiply in
GitHub Actions (each such entry merges into *every* existing combination instead, last one winning) - so the
dynamic list is read into a second named array axis (`leg`), which genuinely cross-multiplies with `os`."""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def load_package_job():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["package"]


def test_the_os_axis_matches_g3_suites():
    job = load_package_job()
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    assert job["runs-on"] == "${{ matrix.os }}"


def test_the_dynamic_matrix_is_a_second_named_axis_not_the_special_include_key():
    """A top-level `include:` key next to a static `os:` array does not cross-multiply in GitHub Actions - an
    include entry with none of the static axis's own keys (here, no `os`) is merged into every existing os
    combination instead of creating one job per (os, entry) pair. Naming the dynamic axis anything other than
    `include` (here `leg`) is what makes two plain arrays cross-multiply, which is the documented, unambiguous
    case."""
    job = load_package_job()
    matrix = job["strategy"]["matrix"]
    assert "include" not in matrix
    assert matrix["leg"] == "${{ fromJSON(needs.packages.outputs.matrix).include }}"


def test_the_run_step_reads_the_leg_axis():
    job = load_package_job()
    run_text = job["steps"][-1]["run"]
    assert "${{ matrix.leg.package }}" in run_text
    assert "${{ matrix.leg.framework }}" in run_text
    assert "${{ matrix.package }}" not in run_text
    assert "${{ matrix.framework }}" not in run_text
