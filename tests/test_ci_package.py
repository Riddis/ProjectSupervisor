"""tools/ci_package.py: the CI of the plugin packages kept in this repository under packages/<distribution>/
(2.0.0, decision D2 of the boundary plan's phase 5). Each package is tested against the oldest framework release in
its manifest's range, the newest *released* framework in that range (2.5.0's M2, deduplicated with the oldest when
the two coincide) and against HEAD; a package's own tag (`<identity>-vX.Y.Z`) is never taken for a framework
release. The `run` step (wheels, a fresh environment, the installed proof, the suite) runs in CI on a package
scaffolded there; here, the matrix and the options it reads."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def ci():
    spec = importlib.util.spec_from_file_location("ci_package_under_test", REPO / "tools" / "ci_package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo: Path, *args: str, when: str | None = None) -> None:
    import os
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def add_package(repo: Path, identity: str, framework: str, ci_table: str = "") -> Path:
    dist = repo / "packages" / f"supervisor-plugin-{identity}"
    pkg = dist / f"supervisor_plugin_{identity}"
    pkg.mkdir(parents=True)
    (dist / "pyproject.toml").write_text(f'[project]\nname = "supervisor-plugin-{identity}"\nversion = "1.0.0"\n{ci_table}',
                                         encoding="utf-8")
    (pkg / "supervisor-plugin.toml").write_text(
        f'identity = "{identity}"\nversion = "1.0.0"\nframework = "{framework}"\n', encoding="utf-8")
    (dist / "tests").mkdir()
    return dist


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "fw"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "README.md").write_text("fw\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    for name in ("v1.1.0", "v1.2.0", "v1.10.0", "alpha-v1.0.0", "v1-v1.5.0"):
        git(root, "tag", name)
    return root


def test_each_package_runs_against_its_oldest_newest_and_head(ci, repo):
    add_package(repo, "alpha", ">=1.2,<2")           # in range: v1.2.0 (oldest), v1.10.0 (newest) - two releases
    add_package(repo, "beta", ">=1.3,<2")            # in range: only v1.10.0 - oldest and newest coincide
    (repo / "packages" / "README.md").write_text("what lives here\n", encoding="utf-8")
    assert ci.matrix(repo) == {"include": [
        {"package": "supervisor-plugin-alpha", "framework": "v1.2.0"},
        {"package": "supervisor-plugin-alpha", "framework": "v1.10.0"},
        {"package": "supervisor-plugin-alpha", "framework": "HEAD"},
        {"package": "supervisor-plugin-beta", "framework": "v1.10.0"},
        {"package": "supervisor-plugin-beta", "framework": "HEAD"},
    ]}


def test_a_package_whose_oldest_in_range_is_already_the_newest_has_no_duplicate(ci, repo):
    add_package(repo, "delta", ">=1.10,<2")          # only v1.10.0 is in range: oldest and newest are one release
    assert ci.matrix(repo) == {"include": [
        {"package": "supervisor-plugin-delta", "framework": "v1.10.0"},
        {"package": "supervisor-plugin-delta", "framework": "HEAD"},
    ]}


def test_a_package_tag_is_never_its_oldest_or_newest_framework(ci, repo):
    add_package(repo, "gamma", ">=1.4,<1.9")         # only the package tag v1-v1.5.0 looks like it is in range
    assert ci.matrix(repo) == {"include": [{"package": "supervisor-plugin-gamma", "framework": "HEAD"}]}


def test_without_packages_the_matrix_is_empty(ci, repo):
    assert ci.matrix(repo) == {"include": []}


def test_the_github_output_carries_the_matrix_and_its_size(ci, repo, monkeypatch, tmp_path):
    add_package(repo, "alpha", ">=1.2,<2")
    monkeypatch.setattr(ci, "ROOT", repo)
    out = tmp_path / "github_output"
    assert ci.main(["matrix", "--github-output", str(out)]) == 0
    lines = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    assert lines["count"] == "3" and json.loads(lines["matrix"])["include"][0]["framework"] == "v1.2.0"


def test_a_package_names_its_lane_python_job_and_config_for_ci(ci, repo):
    dist = add_package(repo, "alpha", ">=1.2,<2", ci_table=(
        '\n[tool.supervisor.ci]\nlane = "alpha"\npython_job = "alpha_sync"\n'
        '\n[tool.supervisor.ci.config]\nendpoint = "http://127.0.0.1:9"\n'))
    assert ci.ci_options(dist) == {"lane": "alpha", "python_job": "alpha_sync",
                                   "config": {"endpoint": "http://127.0.0.1:9"}}
    assert ci.ci_options(add_package(repo, "beta", ">=1.2,<2")) == {}


def commit_time(repo: Path, ref: str, path: str | None = None) -> str:
    args = ["git", "-C", str(repo), "log", "-1", "--format=%ct", ref]
    if path is not None:
        args += ["--", path]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def test_the_epoch_is_the_packages_own_last_commit_time_not_the_repos(ci, repo):
    dist = add_package(repo, "alpha", ">=1.2,<2")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add alpha", when="2026-01-01T00:00:00")
    pkg_epoch = commit_time(repo, "HEAD", str(dist.relative_to(repo)))
    (repo / "README.md").write_text("fw, touched again\n", encoding="utf-8")   # a later commit outside the package
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "touch something outside the package", when="2026-06-01T00:00:00")
    assert commit_time(repo, "HEAD") != pkg_epoch                             # the repo's own last commit moved on
    assert ci.commit_epoch(repo, "HEAD", str(dist.relative_to(repo))) == pkg_epoch


def test_the_framework_epoch_is_the_whole_refs_commit_time(ci, repo):
    assert ci.commit_epoch(repo, "v1.2.0") == commit_time(repo, "v1.2.0")


def test_a_folder_without_exactly_one_manifest_is_refused(ci, repo):
    dist = add_package(repo, "alpha", ">=1.2,<2")
    (dist / "tests" / "supervisor-plugin.toml").write_text("identity = 'fixture'\n", encoding="utf-8")
    assert ci.manifest(dist)["identity"] == "alpha"                 # a test fixture's manifest does not count
    (dist / "other").mkdir()
    (dist / "other" / "supervisor-plugin.toml").write_text("identity = 'two'\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="expected one"):
        ci.manifest(dist)
