"""Scaffolding (phase 3 step 10).

`supervisor plugin new <name>` writes `plugins/<name>.py` and its manifest `plugins/<name>.toml`, and lists
the name in project.yaml. `--package` creates the standard package instead, from `supervisor/plugin_template/`.
It holds `pyproject.toml` with the entry point, the Python package (setup(), the manifest as package data, a pydantic
config model), `tests/test_conformance.py`, which runs `supervisor plugin check`, a behaviour test, a README and
a CHANGELOG. Both pass G5 as generated, and the package's own suite passes as generated.

The package is not installed here. That would change the environment the suite runs in, so an installed
distribution is simulated with a .dist-info on a temporary path (as tests/test_discovery.py does). The CI job of
step 11 installs a generated package into a fresh environment for real."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supervisor import __version__, plugin_check, plugins, manifest, project, testing
from supervisor.cli import app

runner = CliRunner()
FRAMEWORK = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def test_a_local_plugin_is_scaffolded_with_its_manifest_and_passes_g5(tmp_path, monkeypatch):
    layout = testing.project(tmp_path, name="proj")
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    result = runner.invoke(app, ["plugin", "new", "notes"])
    assert result.exit_code == 0, result.output
    assert (layout.plugins / "notes.py").exists()
    m = manifest.load(layout.plugins / "notes.toml")
    assert m.identity == "notes" and m.supports()
    assert "notes" in [e if isinstance(e, str) else e.get("name") for e in project.load(layout)["plugins"]]
    testing.reset()
    found = plugin_check.run(layout, "notes")
    assert all(f.ok for f in found), {f.name: f.detail for f in found}


def scaffold_package(tmp_path, monkeypatch, identity: str = "notes") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plugin", "new", identity, "--package", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path / f"supervisor-plugin-{identity.replace('_', '-')}"


def test_the_package_has_the_standard_shape(tmp_path, monkeypatch):
    dist = scaffold_package(tmp_path, monkeypatch, "lab_notes")
    pkg = dist / "supervisor_plugin_lab_notes"
    for rel in ("pyproject.toml", "README.md", "CHANGELOG.md", "tests/test_conformance.py", "tests/test_lab_notes.py"):
        assert (dist / rel).is_file(), rel
    for rel in ("__init__.py", "config.py", "supervisor-plugin.toml"):
        assert (pkg / rel).is_file(), rel
    meta = tomllib.loads((dist / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["name"] == "supervisor-plugin-lab-notes"
    assert meta["project"]["entry-points"]["supervisor.plugins"] == {"lab_notes": "supervisor_plugin_lab_notes"}
    assert "supervisor-plugin.toml" in meta["tool"]["setuptools"]["package-data"]["supervisor_plugin_lab_notes"]
    m = manifest.load(pkg / "supervisor-plugin.toml")
    major, minor = (int(x) for x in __version__.split(".")[:2])
    assert m.identity == "lab_notes" and m.framework == f">={major}.{minor},<{major + 1}" and m.supports()
    assert m.config.section == "plugins.lab_notes" and m.config.schema == "supervisor_plugin_lab_notes.config:Settings"
    assert "{{" not in "".join(p.read_text(encoding="utf-8") for p in dist.rglob("*") if p.is_file())



def test_the_package_names_its_python_job_for_the_framework_repositorys_ci(tmp_path, monkeypatch):
    """1.14.0: a package kept under the framework repository's packages/ gets a CI job (tools/ci_package.py), which
    drives what `[tool.supervisor.ci]` names; the generated package names the python job its setup() registers."""
    dist = scaffold_package(tmp_path, monkeypatch, "lab_notes")
    meta = tomllib.loads((dist / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["tool"]["supervisor"]["ci"] == {"python_job": "lab_notes_hello"}


def test_the_package_ignores_its_build_output(tmp_path, monkeypatch):
    """1.13.0, a skill-trial finding (plugin-new, round 2): a build or an editable install in the package's
    repository left build/ and the egg-info untracked, so the scaffold ships a .gitignore for them."""
    dist = scaffold_package(tmp_path, monkeypatch, "lab_notes")
    ignored = (dist / ".gitignore").read_text(encoding="utf-8").split()
    assert {"build/", "dist/", "*.egg-info/", "__pycache__/"} <= set(ignored), ignored
def installed(site: Path, dist: Path, identity: str) -> None:
    """dist's package on `site`, with a .dist-info as pip would write (entry point and file list)."""
    package = f"supervisor_plugin_{identity}"
    shutil.copytree(dist / package, site / package)
    info = site / f"{package}-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {package}\nVersion: 0.1.0\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[supervisor.plugins]\n{identity} = {package}\n", encoding="utf-8")
    files = [p.relative_to(site).as_posix() for p in (site / package).rglob("*") if p.is_file()]
    (info / "RECORD").write_text("".join(f"{f},,\n" for f in files), encoding="utf-8")


def test_the_generated_package_passes_g5_and_its_own_suite(tmp_path, monkeypatch):
    dist = scaffold_package(tmp_path / "work", monkeypatch)
    site = tmp_path / "site"
    site.mkdir()
    installed(site, dist, "notes")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(site), str(FRAMEWORK)]))   # for the fresh-process probe
    layout = testing.project(tmp_path / "proj", plugins=["notes"], routing={"plugins": {"notes": {}}})
    found = plugin_check.run(layout, "notes")
    assert all(f.ok for f in found), {f.name: f.detail for f in found}
    assert plugins.load(layout).loaded == ["notes"]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(FRAMEWORK)])}
    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(dist / "tests")],
                           cwd=str(dist), env=env, capture_output=True, text=True, timeout=300, check=False)
    assert suite.returncode == 0, suite.stdout[-3000:] + suite.stderr[-2000:]
    assert " passed" in suite.stdout and "test_conformance" in (dist / "tests" / "test_conformance.py").name


@pytest.mark.parametrize("name", ["Bad-Name", "1st", "with space"])
def test_an_invalid_identity_is_refused(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plugin", "new", name, "--package", "--dir", str(tmp_path)])
    assert result.exit_code != 0 and not list(tmp_path.iterdir())


def test_an_existing_package_folder_is_not_overwritten(tmp_path, monkeypatch):
    dist = scaffold_package(tmp_path, monkeypatch)
    (dist / "README.md").write_text("mine", encoding="utf-8")
    result = runner.invoke(app, ["plugin", "new", "notes", "--package", "--dir", str(tmp_path)])
    assert result.exit_code != 0 and (dist / "README.md").read_text(encoding="utf-8") == "mine"
