"""1.13.0, skill-trial findings (rehearsal, maintenance-window, runtime-diagnose): `supervisor status` did not say which
project it read, so a session working on a copy could not confirm `SUPERVISOR_ROOT`, and no command said whether a
loop held `state/.lock`. The file stays after the loop stops, and its sidecar was the only clue. Status now prints
the project root and the lock's holder (or that it is free). It still writes nothing."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from typer.testing import CliRunner

from supervisor import testing
from supervisor.cli import app

FRAMEWORK = str(Path(__file__).resolve().parents[1])


def status(root, monkeypatch) -> str:
    monkeypatch.setenv("SUPERVISOR_ROOT", str(root))
    out = CliRunner().invoke(app, ["status"])
    assert out.exit_code == 0, out.output
    return out.output


def test_status_names_the_project_it_reads_and_a_free_lock(tmp_path, monkeypatch):
    lay = testing.project(tmp_path / "copy")
    out = status(lay.root, monkeypatch)
    assert f"project: {lay.root}" in out
    assert "lock: free" in out


def test_status_names_the_lock_holder(tmp_path, monkeypatch):
    lay = testing.project(tmp_path / "live")
    lay.state.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; from pathlib import Path; from supervisor.locks import FileLock\n"
         "with FileLock(Path(sys.argv[1]), owner='supervisor pid 4242'):\n"
         "    print('held', flush=True); time.sleep(30)\n", str(lay.lock)],
        stdout=subprocess.PIPE, text=True, env={**__import__("os").environ, "PYTHONPATH": FRAMEWORK})
    try:
        assert holder.stdout.readline().strip() == "held"
        out = status(lay.root, monkeypatch)
        assert "lock: held by supervisor pid 4242" in out, out
    finally:
        holder.kill()
        holder.wait(10)
    time.sleep(0.2)
    assert "lock: free" in status(lay.root, monkeypatch)
