"""tools/ci_package.py: the CI of the plugin packages kept in this repository under packages/<distribution>/
(2.0.0, decision D2 of the boundary plan's phase 5). Each package is tested against the oldest framework release in
its manifest's range and against HEAD; a package's own tag (`<identity>-vX.Y.Z`) is never taken for a framework
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


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


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


def test_each_package_runs_against_its_oldest_framework_release_and_head(ci, repo):
    add_package(repo, "alpha", ">=1.2,<2")
    add_package(repo, "beta", ">=1.3,<2")            # the oldest release in range is 1.10.0 (by number, not name)
    (repo / "packages" / "README.md").write_text("what lives here\n", encoding="utf-8")
    assert ci.matrix(repo) == {"include": [
        {"package": "supervisor-plugin-alpha", "framework": "v1.2.0"},
        {"package": "supervisor-plugin-alpha", "framework": "HEAD"},
        {"package": "supervisor-plugin-beta", "framework": "v1.10.0"},
        {"package": "supervisor-plugin-beta", "framework": "HEAD"},
    ]}


def test_a_package_tag_is_never_its_oldest_framework(ci, repo):
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
    assert lines["count"] == "2" and json.loads(lines["matrix"])["include"][0]["framework"] == "v1.2.0"


def test_a_package_names_its_lane_python_job_and_config_for_ci(ci, repo):
    dist = add_package(repo, "alpha", ">=1.2,<2", ci_table=(
        '\n[tool.supervisor.ci]\nlane = "alpha"\npython_job = "alpha_sync"\n'
        '\n[tool.supervisor.ci.config]\nendpoint = "http://127.0.0.1:9"\n'))
    assert ci.ci_options(dist) == {"lane": "alpha", "python_job": "alpha_sync",
                                   "config": {"endpoint": "http://127.0.0.1:9"}}
    assert ci.ci_options(add_package(repo, "beta", ">=1.2,<2")) == {}


def test_a_folder_without_exactly_one_manifest_is_refused(ci, repo):
    dist = add_package(repo, "alpha", ">=1.2,<2")
    (dist / "tests" / "supervisor-plugin.toml").write_text("identity = 'fixture'\n", encoding="utf-8")
    assert ci.manifest(dist)["identity"] == "alpha"                 # a test fixture's manifest does not count
    (dist / "other").mkdir()
    (dist / "other" / "supervisor-plugin.toml").write_text("identity = 'two'\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="expected one"):
        ci.manifest(dist)
