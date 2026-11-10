"""M7 (docs/releases/2.5.0/CONTRIBUTOR_RULING.md §3.5): one fixed-name job replaces the six-and-growing
`package …` matrix contexts in branch protection. `all_gates` (rendered name `all gates`) needs `[packages,
package]`, runs `if: always()` so a failed dependency does not simply skip it, and its one step's shell asserts
`packages` succeeded and `package` either succeeded or was legitimately skipped (no package in the repository,
`packages.outputs.count == '0'`). T4-24's protection PATCH requires the context by this file's own
`REQUIRED_CONTEXT_NAME` - it does not choose the name."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
REQUIRED_CONTEXT_NAME = "all gates"  # the exact string T4-24's branch-protection PATCH must require

# P3-6: a `bash` on PATH is not necessarily a shell (the WSL launcher without a distro exits 1 for every script,
# printing a UTF-16 "WSL is not installed" message - exactly what runs/T4-21/T4-21.a4/verification.json hit when
# System32 came before Git for Windows on the verifying host's PATH). These are Git for Windows' own well-known
# install locations, tried only as a fallback when the first `bash` PATH offers cannot run one.
_GIT_FOR_WINDOWS_BASH = (r"Git\bin\bash.exe", r"Git\usr\bin\bash.exe")


def _can_run_a_script(bash_path: str) -> bool:
    try:
        probe = subprocess.run([bash_path, "--noprofile", "--norc", "-c", "exit 0"],
                                capture_output=True, text=True, timeout=10, check=False)
    except OSError:
        return False
    return probe.returncode == 0


def _discover_bash() -> str:
    """The first `bash` PATH offers, smoke-tested with `exit 0`; Git for Windows' own `bash.exe` as a fallback
    when it cannot run that; a named skip, never a failure, when nothing on this host can."""
    found = shutil.which("bash")
    assert found, "bash is required to exercise the aggregator's own shell logic"
    if _can_run_a_script(found):
        return found
    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_var)
        if not base:
            continue
        for rel in _GIT_FOR_WINDOWS_BASH:
            candidate = str(Path(base) / rel)
            if Path(candidate).is_file() and _can_run_a_script(candidate):
                return candidate
    pytest.skip(f"the first bash on PATH ({found}) cannot run a shell script and no working Git for Windows "
                f"bash.exe was found as a fallback - rejected: {found}")


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
    bash = _discover_bash()
    # GitHub Actions' own default for a bash `run:` step (docs.github.com, "Default shell"): errexit and pipefail,
    # so the first failing `test` already stops the script - reproduced here rather than a bare `bash -c`.
    return subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                           capture_output=True, text=True, check=False)


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
