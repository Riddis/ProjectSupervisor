"""Round-5 review F10 (docs/reviews/RELEASE_REVIEW_2.1.0_round5.md lines 40-90, answer 2 at lines 177-186,
answer 6 at lines 220-229): F8's fix lowercased the basename of argv[0] and refused it when it ended in
`.bat`/`.cmd` or named PowerShell, applied the exact SHELL_REPARSING pairs and the leading-"-" refusal, then a
file-existence test, and granted the plain two-token prefix to everything else. Three command forms the process
adapter actually runs passed all four checks and still handed the index-2 value to a command processor, because
the suffix test read the literal text a job wrote, not the name Win32 CreateProcess actually resolves, and only
ever looked at argv[0]:

- An argv[0] of `<abs>/plain.cmd` with a trailing space — Win32 path resolution strips the trailing space when
  CreateProcess resolves the target and still routes it through cmd.exe as a batch file; the old suffix test's
  literal `rsplit("/")...lower()` kept the space, so `endswith((".bat", ".cmd"))` was false.
- The same with a trailing dot — same mechanism; the job never even ran its own batch file before the proposal
  (the trailing dot made `plain.cmd.` "not recognized" to cmd.exe directly), yet still ran the proposed statement
  once it was appended.
- A launcher (`env.exe`) whose argv[1] is the batch file — env execs argv[1] as the real program, re-opening the
  F8 mechanism one slot to the right; the old suffix test only ever inspected argv[0].

The fix (`diagnosis._resolved_name`, `_reparses_regardless_of_argv`, `WSL_EXES`) reads the executable name Win32
actually resolves for BOTH argv[0] and argv[1] — trailing spaces/dots stripped, an `::$DATA` alternate-data-stream
spelling folded to the plain name — before the suffix test runs, and applies the same re-parsing-family test to
whichever slot a reparsing executable occupies, so a launcher moved one slot to the right is refused the same way
argv[0] already was. wsl.exe (unobservable on this host, but not thereby exempt) joins PowerShell as a whole
executable family with no established-safe invocation."""
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


def _find_env_exe() -> str | None:
    """Locate a real `env.exe` the way the review's probe used one: Git for Windows ships it under
    `usr/bin`, a directory its own installer does not put on PATH even when `cmd`/`mingw64/bin` (holding
    `git.exe`) is there. Falling back to `git.exe`'s own location keeps this test independent of whichever
    directories a given machine's PATH happens to list."""
    found = shutil.which("env.exe") or shutil.which("env")
    if found:
        return found
    git_exe = shutil.which("git.exe") or shutil.which("git")
    if git_exe:
        git_root = os.path.dirname(os.path.dirname(git_exe))  # .../Git/cmd or .../Git/mingw64/bin -> .../Git
        for candidate in (os.path.join(git_root, "usr", "bin", "env.exe"),
                          os.path.join(os.path.dirname(git_root), "usr", "bin", "env.exe")):
            if os.path.isfile(candidate):
                return candidate
    for program_files in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                           os.environ.get("ProgramW6432")):
        if not program_files:
            continue
        candidate = os.path.join(program_files, "Git", "usr", "bin", "env.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


# Platform-independent unit tests of `_command_prefix` itself: no subprocess, no filesystem dependency for the
# refusal (the review's own caveat: `cmd /r`/`cmd /d` are refused today only because their argv[1] happens not to
# resolve to a file — the same reliance F5 removed for option-lookalike arguments. These new checks are a pure
# string read of what Windows itself resolves, not a filesystem lookup).

def test_command_prefix_refuses_a_trailing_space_or_dot_cmd_spelling_at_argv0(layout):
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    (layout.root / "data.txt").write_text("data", encoding="utf-8")
    # No file need exist under either spelling: the refusal reads the name Windows resolves, not the filesystem.
    assert diagnosis._command_prefix(layout, [str(script) + " ", "data.txt", "ordinary"]) is None
    assert diagnosis._command_prefix(layout, [str(script) + ".", "data.txt", "ordinary"]) is None
    assert diagnosis._command_prefix(layout, [str(script) + "::$DATA", "data.txt", "ordinary"]) is None
    # case-insensitive, like the plain spelling already is
    assert diagnosis._command_prefix(layout, [str(layout.root / "Plain.CMD") + " ", "data.txt"]) is None


def test_command_prefix_refuses_a_launcher_whose_argv1_is_a_reparsed_batch_file(layout):
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    (layout.root / "data.txt").write_text("data", encoding="utf-8")
    env_exe = _find_env_exe() or "env.exe"
    # The launcher itself (argv[0]) is an ordinary, unlisted executable; only argv[1] is the reparsed target.
    assert diagnosis._command_prefix(layout, [env_exe, str(script), "ordinary"]) is None
    assert diagnosis._command_prefix(layout, [env_exe, str(script) + " ", "ordinary"]) is None
    bat = layout.root / "plain.bat"
    bat.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, [env_exe, str(bat), "ordinary"]) is None


def test_command_prefix_refuses_wsl_exe_whatever_argv1_is(layout):
    script = layout.root / "plain.sh"
    script.write_text("echo 11\n", encoding="utf-8")
    # wsl.exe without --exec hands its whole tail to the default distribution's own shell — the same invariant
    # SHELL_REPARSING's sh/bash "-c" entries are refused for, unobservable on a host whose default distribution
    # never starts but not thereby exempt (review lines 72-74).
    assert diagnosis._command_prefix(layout, ["wsl.exe", "./plain.sh", "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["wsl", str(script), "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["WSL.EXE", str(script)]) is None


def test_command_prefix_still_accepts_the_plain_layout_the_trailing_characters_never_touch(layout):
    """The normalisation must not widen what it refuses: a script with no trailing space/dot/ADS, under an
    ordinary interpreter, still yields the plain two-token prefix."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["python", str(script), "ordinary"]) == 2


# Foreground reproductions of the reviewer's exact probe (runs/T2-70/T2-70.a1/r5_commands.py, rows
# env-exe-launcher, batch-trailing-dot, batch-trailing-space): the proposal must be refused before anything is
# stored or launched, so the real child's stdout never changes.

def _refused_before_storage_or_launch(layout, original, value, before_stdout):
    process_task(layout, "T", original, owned=["only/this"], fix_params={"input": {"index": 2, "kind": "path"}})
    assert diagnosis._command_prefix(layout, original) is None     # no eligible index at all

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "repair job", "guidance": "retry",
                                                                      "job_fix": {"params": {"input": value}}}))
    assert out.get("job_fix", {}).get("applied") == {}
    assert "params.input" in out.get("job_fix", {}).get("dropped", [])

    task = tasks.load(layout, "T")
    job = (task.model_extra or {}).get("job")
    assert job["argv"] == original                                 # the stored command is still the original one
    assert task.owned_paths == ["only/this"]

    # the exact command the process adapter would launch next must also be the unchanged, original one
    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == original
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], capture_output=True, text=True,
                            timeout=15)
    # the trailing-dot form never resolved to its own batch file either, before or after the refused proposal
    assert child.stdout.strip() == before_stdout
    assert "22" not in child.stdout

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe and a native batch executable")
def test_env_exe_launcher_whose_argv1_is_the_batch_file_is_refused_before_storage_or_launch(layout):
    env_exe = _find_env_exe()
    if not env_exe:
        pytest.skip("no env.exe found (PATH, git.exe's own usr/bin, or a Program Files Git install)")
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    original = [env_exe, str(script), "ordinary"]
    _refused_before_storage_or_launch(layout, original, "&echo(22", "11")


@pytest.mark.skipif(os.name != "nt", reason="launches a native batch executable")
def test_trailing_dot_cmd_spelling_at_argv0_is_refused_before_storage_or_launch(layout):
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    (layout.root / "data.txt").write_text("data", encoding="utf-8")
    original = [str(script) + ".", "data.txt", "ordinary"]
    # The trailing-dot spelling never resolved to the batch file at all, before or after the refused proposal.
    _refused_before_storage_or_launch(layout, original, "&echo(22", "")


@pytest.mark.skipif(os.name != "nt", reason="launches a native batch executable")
def test_trailing_space_cmd_spelling_at_argv0_is_refused_before_storage_or_launch(layout):
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    (layout.root / "data.txt").write_text("data", encoding="utf-8")
    original = [str(script) + " ", "data.txt", "ordinary"]
    _refused_before_storage_or_launch(layout, original, "&echo(22", "11")
