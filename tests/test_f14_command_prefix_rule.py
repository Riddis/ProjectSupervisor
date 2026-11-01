"""Round-6 review F14 (docs/reviews/RELEASE_REVIEW_2.1.0_round6.md lines 41, 43-103, answer 2 at lines 126-139,
answer 7 at lines 205-215): F10's fix moved the re-parsing-family test to argv[1] and nothing else, so every
other branch of `_command_prefix` still reads a fixed slot — the SHELL_REPARSING pair at (argv[0], argv[1])
only, the leading-"-" refusal at argv[1] only, and the file-existence test on `layout.root / argv[1]`, which an
absolute path OUTSIDE the project root satisfies. When argv[0] is a launcher (env.exe, which execs argv[1] as
the real program), the real command begins one slot to the right: a launcher's own argv[1] (the program it
execs) is never the script a job declared, and granting a prefix on nothing but "does this resolve to a file
on disk" let a diagnosis run as a second launcher level (a reparsed .cmd two slots right of the trusted
executable), as a -c/-command script text one slot right, or install an alternate in-project script at the
real script's own slot — all while the branches granted a prefix to a form they do not understand (a launcher
is not an interpreter, and python.exe is not a script).

The fix: `argv[1]` must now be a REGULAR FILE CONTAINED IN THE PROJECT ROOT, not merely a path that happens to
exist anywhere on disk. This is a statement about the command the job declared (where the script a job runs
must live), not a fifth list of spellings, and it generalises to a launcher chained to any depth: whatever sits
at argv[0], the rule only ever reads argv[1], so a launcher whose own argv[1] is another executable — however
many launchers deep — is refused unless that second token is itself a file inside the project, exactly the
same way a bare `env <launcher>` is."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from supervisor import diagnosis, tasks
from supervisor.adapters.base import Result
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from tests.test_diagnosis_job_fix import diag_rec, process_task
from tests.test_f10_executable_resolution import _find_env_exe


# ---- Platform-independent unit tests of `_command_prefix` itself: the rule is a pure containment read, no
# subprocess, so both the Windows and the ubuntu leg of G3 exercise it directly.

def test_command_prefix_refuses_an_argv1_that_exists_but_is_outside_the_project_root(layout):
    """`sys.executable` is a real file (is_file() true) but never inside a disposable test project: existence
    alone must no longer grant a prefix — the review's own gap, satisfied by "an absolute path outside the
    project root" (`layout.root / argv[1]`'s old, unconditional `.is_file()` test)."""
    assert diagnosis._command_prefix(layout, [sys.executable, sys.executable, "ordinary"]) is None


def test_command_prefix_still_accepts_an_argv1_contained_in_the_project_root(layout):
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["python", str(script), "ordinary"]) == 2
    assert diagnosis._command_prefix(layout, ["python", "plain.py", "ordinary"]) == 2       # relative spelling too


def test_a_launcher_chain_is_refused_at_any_depth_not_only_two_levels(layout):
    """The rule reads only argv[1], whatever argv[0] is, so it generalises to any depth of launcher chaining
    without a depth-specific branch: a second launcher outside the root is refused the same way one is, and a
    third makes no difference either, because the SECOND token is still the one examined."""
    outside = sys.executable
    assert diagnosis._command_prefix(layout, [outside, outside, "ordinary"]) is None            # one launcher
    assert diagnosis._command_prefix(layout, [outside, outside, outside, "ordinary"]) is None    # two launchers


def test_command_prefix_refuses_argv1_that_is_a_reparsing_executable_even_inside_the_root(layout):
    """Containment is necessary but not sufficient: an in-root `.cmd`/`.bat` at argv[1] is still refused by the
    existing re-parsing-family test (F10), which runs before the new containment check reaches it — the two
    checks are independent properties of the command, not substitutes for each other."""
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["env.exe", str(script), "ordinary"]) is None


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="no cmd.exe on PATH")
def test_command_prefix_refuses_a_launcher_over_cmd_slash_c_now_that_argv1_is_checked_for_containment(layout):
    """The review's two accepted-but-harmless rows (`env.exe cmd.exe /c <abs>\\plain.cmd`, un-injected on this
    host only because the launcher's own quoting never handed `/c` through): `cmd.exe` itself, wherever it
    lives, is outside the project root, so this form is now refused outright rather than merely failing to
    inject for an unrelated reason."""
    cmd_exe = shutil.which("cmd.exe")
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["env.exe", cmd_exe, "/c", str(script), "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["env.exe", cmd_exe, "/C", str(script), "ordinary"]) is None


def test_the_two_launcher_controls_now_lose_their_prefix_too(layout):
    """`env.exe python plain.py` and `env.exe bash plain.sh` (the review's own controls, which previously passed
    a value at index 3 literally) also lose their prefix under this rule: their argv[1] (the interpreter) is an
    executable outside the project root, the same property that refuses every other row above. Chosen
    deliberately over the recursive alternative (`docs/releases/2.1.0/F14-command-prefix-rule.md` records why):
    a diagnosis can no longer correct a parameter for a job declared behind any launcher, not only an unsafe
    one — the smaller, uniform rule, not a special case for "this launcher happens to be followed by a trusted
    interpreter"."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, [sys.executable, sys.executable, str(script), "ordinary"]) is None


# ---- Foreground reproductions of the reviewer's exact four rows (runs/T2-79/T2-79.a1/r6_commands.py,
# r6_commands.txt:9,13,14,15): the proposal must be refused before anything is stored or launched, so the real
# child's stdout never changes.

def _refused_before_storage_or_launch(layout, original, fix_params, value, before_stdout):
    process_task(layout, "T", original, owned=["only/this"], fix_params=fix_params)
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

    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == original                                 # the exact next-launch command is unchanged
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], capture_output=True, text=True,
                            timeout=15, stdin=subprocess.DEVNULL)
    assert child.stdout.strip() == before_stdout
    assert "22" not in child.stdout

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)


def _skip_if_missing(*paths):
    for p in paths:
        if not p:
            pytest.skip("a required executable is not available on this host")


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over env.exe over a native batch executable")
def test_launcher_over_launcher_over_batch_file_is_refused_before_storage_or_launch(layout):
    """r6_commands.txt:9, `nested-env-env-batch`: the F10 mechanism one level deeper — a `.cmd` at argv[2] under
    two launchers, re-invoked through cmd.exe exactly as it was at argv[1] before F10."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    script = layout.root / "plain.cmd"
    script.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    original = [env_exe, env_exe, str(script), "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "kind": "path"}}, "&echo(22", "11")


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over bash.exe -c")
def test_launcher_over_bash_dash_c_script_text_is_refused_before_storage_or_launch(layout):
    """r6_commands.txt:13, `env-bash-dash-c-script-slot`: the F1/F5 leading-"-" rule one slot right — the job's
    own inline `-c` script text is a declared parameter past the old prefix 2."""
    env_exe = _find_env_exe()
    bash_exe = shutil.which("bash.exe") or shutil.which("bash")
    _skip_if_missing(env_exe, bash_exe)
    original = [env_exe, bash_exe, "-c", "echo 11", "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "echo 22", "11")


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over python -c")
def test_launcher_over_python_dash_c_script_text_is_refused_before_storage_or_launch(layout):
    """r6_commands.txt:14, `env-python-dash-c-script-slot`: the same rule for `python -c`."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    original = [env_exe, sys.executable, "-c", "print(11)", "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "print(22)", "11")


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over a plain python script")
def test_launcher_over_a_plain_interpreter_script_slot_is_refused_before_storage_or_launch(layout):
    """r6_commands.txt:15, `env-python-script-slot-replaced`: needs no re-parsing at all — under a launcher the
    real script sits at argv[2], which the old prefix 2 left mutable, and an in-project path value passed the
    declared `kind: path` check, installing an alternate script."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    (layout.root / "other.py").write_text("print(22)\n", encoding="utf-8")
    original = [env_exe, sys.executable, str(script), "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 2, "kind": "path"}}, "other.py", "11")
