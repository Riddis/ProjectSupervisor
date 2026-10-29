"""tools/ci_installed_extension.py, the installed-package proof behind gate G5 in CI (1.14.0 options).

An extension package's CI job (packages/<distribution>/, docs/EXTENSION_STANDARD.md) runs the tool against the
built wheel. A package whose config has a required field cannot load in an empty section (`--config <file>` gives
it one); a package without a lane proves itself on a python job it registers (`--python-job <name>`); and the
tool's temporary project goes in a folder of its own under the current directory (or `--workdir`), never the
system's temporary folder, and is removed when the run passes.

The package here is installed without pip: a module and its dist-info (with the entry point) on PYTHONPATH, which
is what an installed wheel leaves in site-packages."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "ci_installed_extension.py"

INIT = '''
from supervisor import ext


def count(root, run_dir, **params):
    label = ext.config(ext.Layout(root), "tally")["label"]
    ext.atomic_json(ext.Layout(root).root / "tally.json", {"label": label})
    return {"status": "COMPLETE", "artifacts": ["tally.json"], "notes": label}


def setup(registry):
    registry.python_job("tally_count", count)
'''

CONFIG = '''
from pydantic import BaseModel


class Settings(BaseModel):
    label: str                      # required: no default
'''

MANIFEST = '''
identity = "tally"
version = "1.0.0"
framework = ">=1.12,<2"

[provides]
python_jobs = ["tally_count"]

[config]
section = "extensions.tally"
schema = "tally.config:Settings"
'''


def installed(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    pkg = site / "tally"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(INIT, encoding="utf-8")
    (pkg / "config.py").write_text(CONFIG, encoding="utf-8")
    (pkg / "supervisor-extension.toml").write_text(MANIFEST, encoding="utf-8")
    info = site / "supervisor_ext_tally-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: supervisor-ext-tally\nVersion: 1.0.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text("[supervisor.extensions]\ntally = tally\n", encoding="utf-8")
    (info / "top_level.txt").write_text("tally\n", encoding="utf-8")
    return site


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    cwd = tmp_path / "ci"
    cwd.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(installed(tmp_path))]),
           "PYTHONIOENCODING": "utf-8"}
    env.pop("SUPERVISOR_ROOT", None)
    return subprocess.run([sys.executable, str(TOOL), "tally", *args], cwd=str(cwd), env=env, capture_output=True,
                          text=True, encoding="utf-8", check=False, timeout=300)


def test_a_package_with_a_required_config_field_loads_with_config_and_completes_its_python_job(tmp_path):
    cfg = tmp_path / "tally-config.yaml"
    cfg.write_text("label: counted\n", encoding="utf-8")
    proc = run(tmp_path, "--config", str(cfg), "--python-job", "tally_count")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "source=installed" in out and "python job tally_count: COMPLETE" in out and out.rstrip().endswith("OK")


def test_without_its_config_such_a_package_fails_to_load_and_says_so(tmp_path):
    proc = run(tmp_path, "--python-job", "tally_count")
    assert proc.returncode != 0 and "2 load" in proc.stdout and "label" in proc.stdout + proc.stderr


def test_an_unknown_python_job_fails(tmp_path):
    cfg = tmp_path / "tally-config.yaml"
    cfg.write_text("label: counted\n", encoding="utf-8")
    proc = run(tmp_path, "--config", str(cfg), "--python-job", "no_such_job")
    assert proc.returncode != 0 and "no_such_job" in proc.stdout


def test_the_temporary_project_lives_under_the_working_folder_and_is_removed_after_a_pass(tmp_path):
    cfg = tmp_path / "tally-config.yaml"
    cfg.write_text("label: counted\n", encoding="utf-8")
    before = set(Path(tempfile.gettempdir()).glob("g5-installed-*"))
    proc = run(tmp_path, "--config", str(cfg))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(Path(tempfile.gettempdir()).glob("g5-installed-*")) == before        # nothing in the system's temp
    assert f"project: {tmp_path / 'ci'}" in proc.stdout                             # under the current folder
    assert not any((tmp_path / "ci").glob("g5-installed-*"))                        # and gone after a pass


def test_a_failed_run_keeps_its_project_in_the_given_workdir(tmp_path):
    work = tmp_path / "work"
    proc = run(tmp_path, "--workdir", str(work), "--python-job", "tally_count")     # no config: fails to load
    assert proc.returncode != 0
    kept = list(work.glob("g5-installed-*"))
    assert len(kept) == 1 and (kept[0] / "project.yaml").is_file() and str(kept[0]) in proc.stdout
