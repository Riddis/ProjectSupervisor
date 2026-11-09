"""M7 (docs/releases/2.5.0/CONTRIBUTOR_RULING.md §3.5): one fixed-name job replaces the six-and-growing
`package …` matrix contexts in branch protection. `all_gates` (rendered name `all gates`) needs `[packages,
package]`, runs `if: always()` so a failed dependency does not simply skip it, and its one step's shell asserts
`packages` succeeded and `package` either succeeded or was legitimately skipped (no package in the repository,
`packages.outputs.count == '0'`). T4-24's protection PATCH requires the context by this file's own
`REQUIRED_CONTEXT_NAME` - it does not choose the name."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
REQUIRED_CONTEXT_NAME = "all gates"  # the exact string T4-24's branch-protection PATCH must require


def load_job():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "all_gates" in jobs, "M7: no job named all_gates in .github/workflows/ci.yml"
    return jobs["all_gates"]


def test_all_gates_needs_packages_and_package():
    job = load_job()
    assert job["needs"] == ["packages", "package"]


def test_all_gates_name_is_the_fixed_required_context_string():
    job = load_job()
    assert job["name"] == REQUIRED_CONTEXT_NAME


def test_all_gates_runs_even_when_a_needed_job_failed():
    job = load_job()
    assert job["if"] == "always()"


def run_step(packages_result: str, package_result: str, packages_count: str) -> subprocess.CompletedProcess:
    job = load_job()
    run_text = None
    for step in job["steps"]:
        if "run" in step:
            run_text = step["run"]
            break
    assert run_text is not None, "M7: all_gates has no run step to check"
    script = (
        run_text
        .replace("${{ needs.packages.result }}", packages_result)
        .replace("${{ needs.package.result }}", package_result)
        .replace("${{ needs.packages.outputs.count }}", packages_count)
    )
    assert "${{" not in script, f"an unreplaced expression survived substitution: {script!r}"
    bash = shutil.which("bash")
    assert bash, "bash is required to exercise the aggregator's own shell logic"
    # GitHub Actions' own default for a bash `run:` step (docs.github.com, "Default shell"): errexit and pipefail,
    # so the first failing `test` already stops the script - reproduced here rather than a bare `bash -c`.
    return subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                           capture_output=True, text=True)


@pytest.mark.parametrize(
    "packages_result,package_result,packages_count,expect_zero",
    [
        ("success", "success", "9", True),           # every needed job actually succeeded
        ("success", "skipped", "0", True),            # no package in the repo: a legitimate skip
        ("failure", "success", "9", False),           # packages itself failed
        ("success", "failure", "9", False),            # a matrix leg failed
        ("success", "skipped", "9", False),            # skipped while packages still lists some: not legitimate
        ("success", "cancelled", "9", False),
    ],
)
def test_the_failure_condition(packages_result, package_result, packages_count, expect_zero):
    result = run_step(packages_result, package_result, packages_count)
    if expect_zero:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
