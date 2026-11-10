"""P3-6 (docs/reviews/RELEASE_REVIEW_2.5.0.md): `test_ci_workflow_aggregator.py`'s six cases bind to the first
`bash` on PATH without checking it is a shell. On a host whose PATH puts a non-shell `bash` first (the WSL
launcher without a distro is one real example; this file fabricates a portable stand-in rather than depending on
that feature being installed), the two `expect_zero` cases fail for a reason unrelated to the aggregator - exactly
what this project's own verifier hit re-running T4-21.a3's gates command (`runs/T4-21/T4-21.a4/verification.json`).

Two process-level cases spawn the committed test as a child with a PATH set by this file, never the ambient one -
the re-runnable form a sandbox that denies a `PATH=... command` prefix needs, the same shape as the reviewer's own
`runs/T4-21/T4-21.a5/test_path_order_2_5_0.py`, a fabricated bash instead of the host's real WSL stub so the
result does not depend on what is installed on the machine running this suite. A third, in-process case drives the
no-fallback-available branch directly (monkeypatching discovery rather than the host's real Program Files layout,
which this workstation complicates with a duplicate all-caps `PROGRAMFILES` already on PATH)."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TARGET = "tests/test_ci_workflow_aggregator.py::test_the_failure_condition"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="a fabricated .cmd stand-in for a non-shell bash is a Windows-only PATH-order concern; "
           "shutil.which(\"bash\") already finds a real shell on Linux (G3 suite, ubuntu-latest)",
)


def _fabricate_non_shell_bash(bin_dir: Path) -> Path:
    """A `bash.cmd` that exits 1 for every invocation, whatever its arguments - a portable stand-in for a `bash`
    on PATH that is not actually a shell (the WSL launcher without a distro is one real-world instance)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "bash.cmd"
    script.write_text("@echo off\r\nexit /b 1\r\n", encoding="utf-8")
    return script


def _git_bash_dir() -> str | None:
    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_var)
        if not base:
            continue
        candidate = Path(base) / "Git" / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate.parent)
    return None


def _run_target(path_value: str, basetemp: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = path_value
    basetemp.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={basetemp}", TARGET],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def test_a_fabricated_non_shell_bash_first_on_path_does_not_fail_the_aggregator_cases(tmp_path):
    """The only `bash` this child's PATH offers cannot run a script - the six cases must pass under a real shell
    found elsewhere on the host, or skip naming the rejected path, never fail."""
    fake_bin = tmp_path / "fake_bash_bin"
    fake_bash = _fabricate_non_shell_bash(fake_bin)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    path_value = os.pathsep.join([str(fake_bin), str(system_root / "System32")])

    result = _run_target(path_value, tmp_path / "pytest_tmp_fake_first")
    (tmp_path / "fake_bash_first.txt").write_text(
        f"fabricated non-shell bash: {fake_bash}\nPATH given to the child: {path_value}\n\n"
        + result.stdout + result.stderr + f"\nEXIT_CODE={result.returncode}\n",
        encoding="utf-8",
    )

    assert "failed" not in result.stdout, result.stdout[-2000:]
    assert result.returncode == 0, result.stdout[-2000:]
    if "skipped" in result.stdout:
        assert str(fake_bash) in result.stdout, "a skip must name the rejected path"


def test_a_working_bash_first_on_path_still_gives_all_six_results(tmp_path):
    """Sanity twin of the case above: when the first `bash` on PATH is a real shell, discovery must not change
    the six cases' own outcome - same result before and after the fix."""
    git_bash_dir = _git_bash_dir()
    if git_bash_dir is None:
        pytest.skip("no Git for Windows bash.exe found on this host to exercise the working-bash-first case")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    path_value = os.pathsep.join([git_bash_dir, str(system_root / "System32")])

    result = _run_target(path_value, tmp_path / "pytest_tmp_working_first")
    (tmp_path / "working_bash_first.txt").write_text(
        f"working bash directory first: {git_bash_dir}\nPATH given to the child: {path_value}\n\n"
        + result.stdout + result.stderr + f"\nEXIT_CODE={result.returncode}\n",
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stdout[-2000:]
    assert "6 passed" in result.stdout, result.stdout[-2000:]


def test_discovery_skips_naming_the_rejected_path_when_nothing_works(monkeypatch, tmp_path):
    """Drives the no-fallback-available branch directly: `shutil.which` and the Git-for-Windows candidate list
    are monkeypatched in-process (not via a child env - this host already carries a duplicate all-caps
    `PROGRAMFILES` that defeats overriding the real one through a subprocess's env block), so the skip is forced
    regardless of what is actually installed here, and the rejected path must be named in the skip reason."""
    agg = importlib.import_module("tests.test_ci_workflow_aggregator")
    rejected = str(tmp_path / "not-a-real-shell" / "bash.cmd")
    monkeypatch.setattr(agg.shutil, "which", lambda name: rejected)
    monkeypatch.setattr(agg, "_GIT_FOR_WINDOWS_BASH", ())

    with pytest.raises(pytest.skip.Exception) as exc_info:
        agg._discover_bash()

    assert rejected in str(exc_info.value)
