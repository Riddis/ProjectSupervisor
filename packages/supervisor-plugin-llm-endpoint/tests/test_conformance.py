"""G5: `supervisor plugin check llm_endpoint` in a temporary project that uses this plugin. The check proves
conformance only; the other tests prove behaviour."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import tomllib

import supervisor
from supervisor import testing

# The framework root this test itself is running against (this process's own sys.path, however it got there),
# so the subprocess checks the same code - never whatever "supervisor" an ambient site-packages happens to have.
# Under CI's installed layout (tools/ci_package.py step 4) "supervisor" is itself an installed wheel, so this
# resolves to site-packages - harmless, since the subprocess would find the same install either way.
FRAMEWORK_ROOT = str(Path(supervisor.__file__).resolve().parents[1])
HERE = Path(__file__).resolve().parent
PACKAGE_SRC = HERE.parent / "supervisor_plugin_llm_endpoint"
PYPROJECT = HERE.parent / "pyproject.toml"


def _editable_layout(root: Path) -> tuple[str, str]:
    """A from-scratch stand-in for `pip install -e .`, built fresh under `root` on every run: a copy of this
    package's source (importable without touching the real tree) plus a distribution-metadata folder carrying
    only the `supervisor.plugins` entry point, so `importlib.metadata` can find it. Built here rather than
    relying on the package's own `*.egg-info` (gitignored, a leftover of a previous `pip install -e .` that a
    fresh clone or worktree never has) so this test proves the subprocess's own PYTHONPATH, not whatever install
    state happens to be lying around. Only called when the source tree ships beside `tests/`; the entry point
    name/value and version are read from pyproject.toml (as test_version.py does), not restated, so a drift there
    fails here too. Returns (src dir, metadata dir) to put on PYTHONPATH."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    version = project["version"]
    name, value = next(iter(project["entry-points"]["supervisor.plugins"].items()))
    src = root / "src"
    shutil.copytree(PACKAGE_SRC, src / PACKAGE_SRC.name)
    meta = root / "meta"
    dist_info = meta / f"{PACKAGE_SRC.name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: supervisor-plugin-llm-endpoint\nVersion: {version}\n", encoding="utf-8")
    (dist_info / "entry_points.txt").write_text(
        f"[supervisor.plugins]\n{name} = {value}\n", encoding="utf-8")
    return str(src), str(meta)


def test_supervisor_plugin_check_passes(tmp_path):
    layout = testing.project(tmp_path / "project", plugins=["llm_endpoint"],
                              routing={"plugins": {"llm_endpoint": {}}})
    parts = [FRAMEWORK_ROOT, os.environ.get("PYTHONPATH", "")]
    # The source tree ships beside tests/ in a worktree or fresh clone; once ci_package.py step 4 has copied
    # only tests/ next to an installed wheel, there is nothing to build a stand-in from, and the ambient install
    # already supplies the entry point.
    if PYPROJECT.is_file():
        src, meta = _editable_layout(tmp_path / "install")
        parts = [meta, src] + parts
    pythonpath = os.pathsep.join(p for p in parts if p)
    env = {**os.environ, "SUPERVISOR_ROOT": str(layout.root), "PYTHONIOENCODING": "utf-8", "PYTHONPATH": pythonpath}
    result = subprocess.run([sys.executable, "-m", "supervisor", "plugin", "check", "llm_endpoint"],
                            cwd=str(layout.root), env=env, capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
