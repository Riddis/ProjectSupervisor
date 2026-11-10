"""2.5.0 P1-1 (docs/reviews/RELEASE_REVIEW_2.5.0.md, finding P1-1): the `g5_reference` job's last step scaffolds a
scratch package (`packages/supervisor-plugin-ci-pkg`, never committed) purely to prove `tools/ci_package.py run()`'s
path. Since T4-15's M1, `run()` resolves `SOURCE_DATE_EPOCH` for the package wheel through `commit_epoch(ROOT,
"HEAD", <package path>)`, which runs `git log -1 --format=%ct HEAD -- <path>` and raises `SystemExit` when that path
has no commit - exactly the uncommitted scratch package's case. `commit_epoch` is correct by its own contract
(docs/releases/2.5.0/CONTRIBUTOR_RULING.md §3.1); the fix is the workflow's: the job commits the scaffolded path in
the runner's own checkout (never pushed) before it calls `tools/ci_package.py run`.

Two assertions:
  - the committed workflow, parsed as YAML: the job commits the scaffolded package before any step invoking
    `tools/ci_package.py run` (named by step position, not by a line number);
  - `commit_epoch()` itself, against a disposable fixture repository: `SystemExit` while the scratch path is
    uncommitted, an integer epoch once it is committed. No wheel build, no network.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _g5_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["g5_reference"]


def test_g5_reference_commits_the_scaffolded_package_before_building_it():
    steps = _g5_job()["steps"]
    scaffold_idx = next(i for i, s in enumerate(steps)
                        if "plugin new ci_pkg --package --dir packages" in s.get("run", ""))
    run_idx = next(i for i, s in enumerate(steps) if "tools/ci_package.py run" in s.get("run", ""))
    commit_idx = next((i for i, s in enumerate(steps)
                       if "git" in s.get("run", "") and "commit" in s.get("run", "")
                       and "packages/supervisor-plugin-ci-pkg" in s.get("run", "")), None)
    assert commit_idx is not None, (
        f"step {scaffold_idx} (0-indexed) scaffolds packages/supervisor-plugin-ci-pkg but no later step "
        "commits it before the step invoking tools/ci_package.py run"
    )
    assert scaffold_idx <= commit_idx < run_idx, (
        f"the commit step (index {commit_idx}) must run between the scaffold step (index {scaffold_idx}) "
        f"and the step invoking tools/ci_package.py run (index {run_idx})"
    )


# ---------------------------------------------------------------------- commit_epoch, against a disposable fixture

@pytest.fixture
def ci():
    spec = importlib.util.spec_from_file_location("ci_package_g5_probe", REPO / "tools" / "ci_package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def fixture_repo(tmp_path):
    root = tmp_path / "fw"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("fw\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    return root


def test_commit_epoch_refuses_an_uncommitted_scratch_package_and_resolves_once_committed(ci, fixture_repo):
    pkg = fixture_repo / "packages" / "supervisor-plugin-ci-pkg"
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text('[project]\nname = "supervisor-plugin-ci-pkg"\nversion = "1.0.0"\n',
                                        encoding="utf-8")
    with pytest.raises(SystemExit, match="no commit found for SOURCE_DATE_EPOCH"):
        ci.commit_epoch(fixture_repo, "HEAD", "packages/supervisor-plugin-ci-pkg")

    _git(fixture_repo, "add", "packages/supervisor-plugin-ci-pkg")
    _git(fixture_repo, "-c", "user.name=ci", "-c", "user.email=ci@example.invalid",
         "commit", "-qm", "ci: scaffold the G5 reference package (not pushed)")

    epoch = ci.commit_epoch(fixture_repo, "HEAD", "packages/supervisor-plugin-ci-pkg")
    assert epoch.isdigit()
    assert int(epoch) > 0
