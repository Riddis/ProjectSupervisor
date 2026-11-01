"""Round-4 review F8 (docs/reviews/RELEASE_REVIEW_2.1.0_round4.md lines 37-79, answer 2 at lines 162-176): F5's
fix refused the thirteen exact SHELL_REPARSING pairs and any argv[1] starting with "-", then granted the plain
two-token prefix to anything else argv[1] names on disk. Two command forms the process adapter actually runs
still re-parse an argument this interface believed was opaque, past that same two-token prefix, turning a
declared path parameter into executable code without ever moving argv[0] or argv[1]:

- PowerShell's own default, flagless mode (no `-Command`, no `-File`) binds everything after its own name to
  the same re-parsed script-text parameter its explicit `-Command` option already used — SHELL_REPARSING only
  matched the flagged form, so the flagless one fell through to the generic "is argv[1] a file" test and
  passed it.
- Windows resolves a `.bat`/`.cmd` target by re-invoking it through its own command processor even when the
  caller never asks for a shell (`subprocess.Popen(argv, ...)`, no `shell=True`) — the "executable" position at
  argv[0] is itself a reparsing wrapper selected by file extension, not by name, so the generic test never
  looked at the one thing that mattered.

The fix (`diagnosis._command_prefix`, `POWERSHELL_EXES`, `REPARSED_EXECUTABLE_SUFFIXES`) establishes, for each
executable, whether ANY invocation this interface grants a prefix for is provably safe — never whether a
filesystem lookup happens to succeed — before it ever reaches the generic file-existence test."""
import json
import os
import shutil
import subprocess

import pytest

from supervisor import diagnosis, tasks
from supervisor.adapters.base import Result
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from tests.test_diagnosis_job_fix import diag_rec, process_task


# Platform-independent unit tests of `_command_prefix` itself: no subprocess, so both the Windows and the ubuntu
# leg of G3 exercise the same layout contract directly.

def test_command_prefix_refuses_powershell_in_its_own_default_mode_whatever_argv1_is(layout):
    script = layout.root / "plain.ps1"
    script.write_text("Write-Output 11\n", encoding="utf-8")
    # No -Command, no -File: PowerShell's own flagless default still re-parses argv[2:] as further script text,
    # the same invariant its explicit -Command form (already in SHELL_REPARSING) is refused for.
    assert diagnosis._command_prefix(layout, ["powershell.exe", str(script), "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["pwsh", str(script), "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["PowerShell.EXE", str(script)]) is None   # case-insensitive, like argv[0] everywhere else


def test_command_prefix_refuses_a_native_batch_executable_at_argv0(layout):
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    (layout.root / "data.txt").write_text("data", encoding="utf-8")
    # Windows substitutes cmd.exe for the "executable" position itself when argv[0] is a .bat/.cmd target, even
    # with shell=False: no index past argv[0] is safe, whatever argv[1] happens to be.
    assert diagnosis._command_prefix(layout, [str(script), "data.txt", "ordinary"]) is None
    bat = layout.root / "plain.bat"
    bat.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, [str(bat), "data.txt", "ordinary"]) is None
    assert diagnosis._command_prefix(layout, [str(layout.root / "Plain.CMD"), "data.txt"]) is None   # case-insensitive extension too


# Foreground reproductions of the reviewer's exact probe (runs/T2-63/T2-63.a1/independent.py, observations
# `powershell_implicit_command` and `batch_executable`): the proposal must be refused before anything is stored
# or launched, so the real child's stdout never changes.

@pytest.mark.skipif(os.name != "nt", reason="launches powershell.exe")
def test_powershell_implicit_command_mode_is_refused_before_storage_or_launch(layout):
    exe = shutil.which("powershell.exe")
    assert exe, "powershell.exe must be on PATH for this test"
    script = layout.root / "plain.ps1"
    script.write_text("Write-Output 11\n", encoding="utf-8")
    original = [exe, str(script), "ordinary"]
    process_task(layout, "T", original, owned=["only/this"], fix_params={"input": {"index": 2, "kind": "path"}})
    assert diagnosis._command_prefix(layout, original) is None     # no eligible index at all

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "repair job", "guidance": "retry",
                                                                      "job_fix": {"params": {"input": ";echo(22)"}}}))
    assert out.get("job_fix", {}).get("applied") == {}
    assert "params.input" in out.get("job_fix", {}).get("dropped", [])

    task = tasks.load(layout, "T")
    job = (task.model_extra or {}).get("job")
    assert job["argv"] == original                                 # the stored command is still the original one
    assert task.owned_paths == ["only/this"]

    # the exact command the process adapter would launch next must also be the unchanged, original one
    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == original
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], capture_output=True, text=True, timeout=15)
    assert child.returncode == 0 and child.stdout.strip() == "11"   # the original script ran, not the proposed statement

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)


@pytest.mark.skipif(os.name != "nt", reason="launches a native batch executable")
def test_batch_executable_at_argv0_is_refused_before_storage_or_launch(layout):
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    (layout.root / "data.txt").write_text("data", encoding="utf-8")
    original = [str(script), "data.txt", "ordinary"]
    process_task(layout, "T", original, owned=["only/this"], fix_params={"input": {"index": 2, "kind": "path"}})
    assert diagnosis._command_prefix(layout, original) is None     # no eligible index at all

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "repair job", "guidance": "retry",
                                                                      "job_fix": {"params": {"input": "&echo(22"}}}))
    assert out.get("job_fix", {}).get("applied") == {}
    assert "params.input" in out.get("job_fix", {}).get("dropped", [])

    task = tasks.load(layout, "T")
    job = (task.model_extra or {}).get("job")
    assert job["argv"] == original                                 # the stored command is still the original one
    assert task.owned_paths == ["only/this"]

    # the exact command the process adapter would launch next must also be the unchanged, original one
    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == original
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], capture_output=True, text=True, timeout=15)
    assert child.returncode == 0 and child.stdout.strip() == "11"   # the original script ran, not the proposed statement

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)
