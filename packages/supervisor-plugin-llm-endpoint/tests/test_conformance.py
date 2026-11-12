"""G5: `supervisor plugin check llm_endpoint` in a temporary project that uses this plugin. The check proves
conformance only; the other tests prove behaviour."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import supervisor
from supervisor import testing

# The framework root this test itself is running against (this process's own sys.path, however it got there),
# so the subprocess checks the same code - never whatever "supervisor" an ambient site-packages happens to have.
FRAMEWORK_ROOT = str(Path(supervisor.__file__).resolve().parents[1])
PACKAGE_SRC = Path(__file__).resolve().parents[1] / "supervisor_plugin_llm_endpoint"
DIST_INFO_NAME = "supervisor_plugin_llm_endpoint-1.0.0.dist-info"


def _editable_layout(root: Path) -> tuple[str, str]:
    """A from-scratch stand-in for `pip install -e .`, built fresh under `root` on every run: a copy of this
    package's source (importable without touching the real tree) plus a distribution-metadata folder carrying
    only the `supervisor.plugins` entry point, so `importlib.metadata` can find it. Built here rather than
    relying on the package's own `*.egg-info` (gitignored, a leftover of a previous `pip install -e .` that a
    fresh clone or worktree never has) so this test proves the subprocess's own PYTHONPATH, not whatever install
    state happens to be lying around. Returns (src dir, metadata dir) to put on PYTHONPATH."""
    src = root / "src"
    shutil.copytree(PACKAGE_SRC, src / PACKAGE_SRC.name)
    meta = root / "meta"
    dist_info = meta / DIST_INFO_NAME
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: supervisor-plugin-llm-endpoint\nVersion: 1.0.0\n", encoding="utf-8")
    (dist_info / "entry_points.txt").write_text(
        f"[supervisor.plugins]\nllm_endpoint = {PACKAGE_SRC.name}\n", encoding="utf-8")
    return str(src), str(meta)


def test_supervisor_plugin_check_passes(tmp_path):
    layout = testing.project(tmp_path / "project", plugins=["llm_endpoint"],
                              routing={"plugins": {"llm_endpoint": {}}})
    src, meta = _editable_layout(tmp_path / "install")
    pythonpath = os.pathsep.join([meta, src, FRAMEWORK_ROOT, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env = {**os.environ, "SUPERVISOR_ROOT": str(layout.root), "PYTHONIOENCODING": "utf-8", "PYTHONPATH": pythonpath}
    result = subprocess.run([sys.executable, "-m", "supervisor", "plugin", "check", "llm_endpoint"],
                            cwd=str(layout.root), env=env, capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
