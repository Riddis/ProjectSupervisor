"""G5: `supervisor plugin check llm_endpoint` in a temporary project that uses this plugin. The check proves
conformance only; the other tests prove behaviour."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import supervisor
from supervisor import testing

# The framework root this test itself is running against (this process's own sys.path, however it got there),
# so the subprocess checks the same code - never whatever "supervisor" an ambient site-packages happens to have.
FRAMEWORK_ROOT = str(Path(supervisor.__file__).resolve().parents[1])


def test_supervisor_plugin_check_passes(tmp_path):
    layout = testing.project(tmp_path, plugins=["llm_endpoint"], routing={"plugins": {"llm_endpoint": {}}})
    pythonpath = os.pathsep.join([FRAMEWORK_ROOT, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env = {**os.environ, "SUPERVISOR_ROOT": str(layout.root), "PYTHONIOENCODING": "utf-8", "PYTHONPATH": pythonpath}
    result = subprocess.run([sys.executable, "-m", "supervisor", "plugin", "check", "llm_endpoint"],
                            cwd=str(layout.root), env=env, capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
