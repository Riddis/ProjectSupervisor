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

The fix: a prefix exists only when `argv[1]` is THIS PROJECT'S OWN SCRIPT — a regular file contained in the
project root (not merely a path that happens to exist anywhere on disk) that is not itself a program the OS
would run (a `.exe`/`.com` image on Windows, the executable bit on POSIX). Both halves are needed and neither
is a list of spellings: containment alone leaves the project's own virtualenv interpreter
(`<root>/.venv/Scripts/python.exe`, the ordinary Python layout) looking exactly like a script, and the
image test alone would trust any file that exists anywhere. Together they are two questions about the command
the job declared, and they need no depth-specific case: whatever sits at argv[0], the rule reads argv[1], and a
launcher chain's second token is another launcher — a program, refused by the same question that refuses the
first, however many levels deep.

Residual, recorded rather than claimed closed (`docs/releases/2.1.0/F14-command-prefix-rule.md`): on Windows an
extensionless executable image copied inside the project would still read as a script, since Windows names an
image by its extension and nothing in the file's own mode says otherwise."""
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
    # The ordinary virtualenv layout, which the rule reads at argv[0] and must not cost a job: a program at
    # argv[0] may live anywhere, inside the project included — it is argv[1] that has to be this job's script.
    assert diagnosis._command_prefix(layout, [str(_in_project_interpreter(layout)), str(script), "ordinary"]) == 2


def _in_project_interpreter(layout, copy_a_real_one: bool = False):
    """The ordinary Python layout: a real interpreter INSIDE the project root. `_command_prefix` reads only the
    resolved name and the file, so a placeholder file is enough for the unit tests; the foreground rows copy
    `sys.executable` itself so the launcher really does exec it."""
    scripts = layout.root / ".venv" / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    interpreter = scripts / "python.exe"
    if copy_a_real_one:
        shutil.copy2(sys.executable, interpreter)
    else:
        interpreter.write_bytes(b"MZ")
    return interpreter


def test_command_prefix_refuses_an_in_project_interpreter_at_argv1(layout):
    """Containment is necessary and NOT sufficient (this task's own review): a project that keeps its
    interpreter in its own virtualenv has a real program at `<root>/.venv/Scripts/python.exe`, contained and a
    regular file, which a containment test cannot tell apart from the script beside it. An executable image at
    argv[1] means argv[0] is a launcher and the real command starts further right, so there is no eligible
    index — whatever the interpreter's own `-c` text or script slot would otherwise have offered."""
    interpreter = _in_project_interpreter(layout)
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["env.exe", str(interpreter), "-c", "print(11)", "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["env.exe", str(interpreter), str(script), "ordinary"]) is None
    relative = str(interpreter.relative_to(layout.root))
    assert diagnosis._command_prefix(layout, ["env.exe", relative, str(script), "ordinary"]) is None
    assert diagnosis._command_prefix(layout, ["env.exe", relative + " ", str(script)]) is None   # stripped spelling


def test_command_prefix_refuses_an_in_project_interpreter_spelled_with_a_trailing_path_component(layout):
    """F15 (2.1.0 round-7 review, `docs/reviews/RELEASE_REVIEW_2.1.0_round7.md` lines 48, 51-115):
    `_resolved_name` is a lexical basename test — it reads the last "/"-separated component of the LITERAL text
    a job wrote — but Win32 normalises a trailing "\\.", "/.", or a bare trailing separator away before it ever
    stats the file, so the SAME real interpreter `test_command_prefix_refuses_an_in_project_interpreter_at_argv1`
    already refuses was granted a prefix under any of these spellings: the resolved name read "" (neither
    ".exe" nor ".com", so `_is_executable_image_name` said no), while `is_file()`/`contained()` read the file
    Win32 actually resolves and said yes (review `r7_commands.txt:12-18`, all `prefix: 2`, `injected_ran: true`
    under `env.exe`). One rule, not five spellings: every trailing-component trick Win32 discards before it
    runs the program must be refused the same way the plain spelling is — including one the review did not
    name (`slash-dot-slash`, below), to prove the fix reads what the OS resolves rather than one more spelling
    added to a list."""
    interpreter = _in_project_interpreter(layout)
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    inside = str(interpreter)
    spellings = [
        ("trailing-backslash-dot", inside + "\\."),                                             # review's :12
        ("trailing-slash-dot", inside + "/."),                                                   # review's :13
        ("trailing-backslash", inside + "\\"),                                                   # review's :16
        ("trailing-slash", inside + "/"),                                                        # review's :17
        ("dotdot-reenter-then-dot", inside + "\\.." + "\\" + interpreter.name + "\\."),           # review's :15
        ("slash-dot-slash-not-in-the-review", inside + "/./"),                                   # not reviewed
    ]
    for label, spelling in spellings:
        assert diagnosis._command_prefix(layout, ["env.exe", spelling, "-c", "print(11)", "ordinary"]) is None, label
        assert diagnosis._command_prefix(layout, ["env.exe", spelling, str(script), "ordinary"]) is None, label


@pytest.mark.skipif(os.name == "nt", reason="the executable bit is POSIX's own answer to the same question")
def test_command_prefix_refuses_an_in_project_executable_bit_at_argv1(layout):
    """The same question on a platform where an interpreter carries no extension to read: `<root>/.venv/bin/
    python` is contained and a regular file, and only its mode says it is a program rather than a script."""
    bin_dir = layout.root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    interpreter = bin_dir / "python"
    interpreter.write_text("#!/bin/sh\necho 11\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["env", str(interpreter), "plain.py", "ordinary"]) == 2  # no bit yet
    interpreter.chmod(0o755)
    assert diagnosis._command_prefix(layout, ["env", str(interpreter), "plain.py", "ordinary"]) is None


def test_a_launcher_chain_is_refused_at_any_depth_not_only_two_levels(layout):
    """The rule reads argv[1], whatever argv[0] is, so it generalises to any depth of launcher chaining without
    a depth-specific branch: the second token of a chain is itself a program, refused by the same question that
    refuses the first, whether it lives outside the project or inside it."""
    outside = sys.executable
    inside = str(_in_project_interpreter(layout))
    assert diagnosis._command_prefix(layout, [outside, outside, "ordinary"]) is None             # one launcher
    assert diagnosis._command_prefix(layout, [outside, outside, outside, "ordinary"]) is None    # two launchers
    assert diagnosis._command_prefix(layout, [outside, inside, inside, "ordinary"]) is None      # in-project ones
    assert diagnosis._command_prefix(layout, [inside, inside, inside, inside, "ordinary"]) is None   # three deep


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
    a value at index 3 literally) also lose their prefix under this rule: their argv[1] is the interpreter — a
    program, not this job's script — the same property that refuses every other row above, and it holds whether
    that interpreter lives outside the project or in its own virtualenv. Chosen deliberately over the recursive
    alternative (`docs/releases/2.1.0/F14-command-prefix-rule.md` records why): a diagnosis can no longer
    correct a parameter for a job declared behind any launcher, not only an unsafe one — the smaller, uniform
    rule, not a special case for "this launcher happens to be followed by a trusted interpreter"."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, [sys.executable, sys.executable, str(script), "ordinary"]) is None
    inside = str(_in_project_interpreter(layout))
    assert diagnosis._command_prefix(layout, ["env.exe", inside, str(script), "ordinary"]) is None


# ---- Foreground reproductions of the reviewer's exact four rows (runs/T2-79/T2-79.a1/r6_commands.py,
# r6_commands.txt:9,13,14,15): the proposal must be refused before anything is stored or launched, so the real
# child's stdout never changes.

def _run_prepared(layout, task, attempt):
    spec = ProcessAdapter().prepare(layout, task, attempt)
    assert spec["argv"] == (task.model_extra or {})["job"]["argv"]   # the exact next-launch command
    return spec, subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], capture_output=True, text=True,
                                 timeout=15, stdin=subprocess.DEVNULL)


def _refused_before_storage_or_launch(layout, original, fix_params, value, before_stdout=None):
    """`before_stdout=None` means "whatever this command prints when it is left alone": the baseline is taken
    by running the unfixed command first, so a row whose program cannot start on a given host still proves the
    refusal and that nothing the diagnosis proposed ever ran, instead of failing on a hardcoded baseline."""
    process_task(layout, "T", original, owned=["only/this"], fix_params=fix_params)
    assert diagnosis._command_prefix(layout, original) is None     # no eligible index at all
    if before_stdout is None:
        before_stdout = _run_prepared(layout, tasks.load(layout, "T"), "T.a9")[1].stdout.strip()

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

    spec, child = _run_prepared(layout, task, "T.a10")
    assert spec["argv"] == original                                 # the exact next-launch command is unchanged
    assert child.stdout.strip() == before_stdout
    assert "22" not in child.stdout

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)


def _skip_if_missing(*paths):
    for p in paths:
        if not p:
            pytest.skip("a required executable is not available on this host")


def _find_bash_exe() -> str | None:
    """Git for Windows ships its own `bash.exe` under both `bin` and `usr/bin`; Windows' own
    `System32\\bash.exe` (the WSL launcher) is also on PATH on most hosts and can resolve first on one where
    the default WSL distribution cannot start, so this derives Git's bash from `git.exe`'s own location
    (the same derivation `_find_env_exe` uses for `env.exe`) rather than trusting `shutil.which`'s PATH order."""
    git_exe = shutil.which("git.exe") or shutil.which("git")
    if git_exe:
        git_root = os.path.dirname(os.path.dirname(git_exe))  # .../Git/cmd or .../Git/mingw64/bin -> .../Git
        for base in (git_root, os.path.dirname(git_root)):
            for sub in ("bin", os.path.join("usr", "bin")):
                candidate = os.path.join(base, sub, "bash.exe")
                if os.path.isfile(candidate):
                    return candidate
    for program_files in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                           os.environ.get("ProgramW6432")):
        if not program_files:
            continue
        for sub in ("bin", os.path.join("usr", "bin")):
            candidate = os.path.join(program_files, "Git", sub, "bash.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


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
    bash_exe = _find_bash_exe()
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


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over a real interpreter copied into the project")
def test_launcher_over_an_in_project_interpreter_script_slot_is_refused_before_storage_or_launch(layout):
    """`r6_commands.txt:15` with the one change this task's own review asked for: the interpreter the launcher
    execs is a REAL copy of `sys.executable` living in the project's own virtualenv, so the row cannot pass
    merely because argv[1] sits outside the root. The declared `kind: path` value is an in-project script, which
    passes every check a proposed value faces; only the prefix stands between it and the next launch."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    interpreter = _in_project_interpreter(layout, copy_a_real_one=True)
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    (layout.root / "other.py").write_text("print(22)\n", encoding="utf-8")
    original = [env_exe, str(interpreter), str(script), "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 2, "kind": "path"}}, "other.py")


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over a real interpreter copied into the project")
def test_launcher_over_an_in_project_interpreter_dash_c_text_is_refused_before_storage_or_launch(layout):
    """`r6_commands.txt:14` with the same change: a real in-project interpreter at argv[1] and the job's own
    inline `-c` script text declared at index 3."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    interpreter = _in_project_interpreter(layout, copy_a_real_one=True)
    original = [env_exe, str(interpreter), "-c", "print(11)", "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "print(22)")


# ---- F15 (round-7 review, lines 48, 51-115): the five spellings of a real copy of this host's interpreter the
# review measured granted and injected (`r7_commands.txt:12-23`), through the real route — plus one spelling not
# in the review's five, to prove the fix is a rule about which file the image question reads, not a sixth list.

_F15_ODD_SPELLINGS_OF_AN_IN_PROJECT_INTERPRETER = [
    ("trailing-backslash-dot", lambda inside, name: inside + "\\."),
    ("trailing-slash-dot", lambda inside, name: inside + "/."),
    ("trailing-backslash", lambda inside, name: inside + "\\"),
    ("trailing-slash", lambda inside, name: inside + "/"),
    ("dotdot-reenter-then-dot", lambda inside, name: inside + "\\.." + "\\" + name + "\\."),
    ("slash-dot-slash-not-in-the-review", lambda inside, name: inside + "/./"),
]


@pytest.mark.skipif(os.name != "nt", reason="Win32's normalisation of a trailing \\., /., \\, / is Windows-only")
@pytest.mark.parametrize("label,build", _F15_ODD_SPELLINGS_OF_AN_IN_PROJECT_INTERPRETER)
def test_launcher_over_an_oddly_spelled_in_project_interpreter_is_refused_before_storage_or_launch(layout, label, build):
    """The same real copy of `sys.executable`
    (`test_launcher_over_an_in_project_interpreter_dash_c_text_is_refused_before_storage_or_launch`'s own
    `interpreter`), spelled with a trailing path-component trick Win32 discards before it stats: no eligible
    index, the job's stored argv unchanged, `diagnosis_job_fixed` logs the drop, and the real foreground child
    never sees the proposed text — it is launched with the ORIGINAL, unfixed command and its stdout stays `11`
    (review `r7_commands.txt:19-23`: before this fix, all five read `prefix: 2`, `stored_argv_changed: true`,
    `injected_ran: true`, stdout `11` then `22`)."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    interpreter = _in_project_interpreter(layout, copy_a_real_one=True)
    spelling = build(str(interpreter), interpreter.name)
    original = [env_exe, spelling, "-c", "print(11)", "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "print(22)")


# ---- F16 (round-7 review, lines 49, 117-156): `cmd.exe` is listed in `SHELL_REPARSING` only under the exact
# pair `(cmd.exe, /c)`/`(cmd.exe, /k)`, but `cmd.exe`'s own parser honours `/c`/`/k` wherever they sit once a
# non-switch token precedes them — an in-root, non-image regular file at argv[1] (e.g. this job's own `plain.py`,
# never itself read by `cmd.exe`) passes both of F14's questions, so every one of the forms below was granted
# prefix 2 before this fix and `propose_job_fix` accepted any declared index from 2 up (`r7_commands.txt:24-28`).
# The rule closing it is the family's, at argv[0], exactly as `POWERSHELL_EXES` is refused — not a fifth/sixth
# list of flag spellings.

def test_command_prefix_refuses_the_cmd_family_at_argv0_whatever_argv1_is(layout):
    """`cmd.exe` has no invocation this interface has established safe (`cmd.exe <script>` is not one — `cmd.exe`
    does not run a script given at argv[1] at all), so the whole family is refused regardless of argv[1], the
    same shape `test_command_prefix_refuses_powershell_in_its_own_default_mode_whatever_argv1_is` already proves
    for `POWERSHELL_EXES`: an exact `/c`/`/k` flag, `/d` ahead of `/c`, a flagless invocation with an ordinary
    in-root file at argv[1] (the review's own bounding row — granted but harmless only because `cmd.exe` without
    `/c` starts interactively and exits on `stdin=DEVNULL`, not an exempt form), and a switch the review never
    probed (`/t:0e`, cmd.exe's colour switch) all return `None` — the rule does not read argv[1] at all once
    argv[0] is this family, so it needs no switch list to enumerate."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")       # never read by cmd.exe in any of these forms
    for exe in ("cmd.exe", "cmd", "CMD.EXE", "Cmd.Exe"):
        assert diagnosis._command_prefix(layout, [exe, str(script), "/c", "echo 11"]) is None
        assert diagnosis._command_prefix(layout, [exe, str(script), "/k", "echo 11"]) is None
        assert diagnosis._command_prefix(layout, [exe, str(script), "/d", "/c", "echo 11"]) is None
        assert diagnosis._command_prefix(layout, [exe, str(script), "ordinary"]) is None            # flagless bounding row
        assert diagnosis._command_prefix(layout, [exe, str(script), "/t:0e", "/c", "echo 11"]) is None   # not /c or /k alone


def test_command_prefix_refuses_the_cmd_family_even_through_a_launcher(layout):
    """The same two questions `_reparses_regardless_of_argv` already asks of argv[1] (F10's launcher-moves-the-
    reparse-one-slot-right mechanism) now also refuse `cmd.exe` as a launcher's own target, exactly as they
    already refuse `powershell.exe`/`wsl.exe` there."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["env.exe", "cmd.exe", str(script), "/c", "echo 11"]) is None


_F16_CMD_FAMILY_FORMS = [
    ("slash-c", lambda script: ["cmd.exe", script, "/c", "echo 11"], {"index": 3, "pattern": ".*"}, "echo 22"),
    ("slash-d-slash-c", lambda script: ["cmd.exe", script, "/d", "/c", "echo 11"], {"index": 4, "pattern": ".*"}, "echo 22"),
    ("flagless-value-carries-a-joined-slash-c",
     lambda script: ["cmd.exe", script, "ordinary"], {"index": 2, "pattern": ".*"}, "/cecho 22"),
    ("flagless-value-carries-a-spaced-slash-c",
     lambda script: ["cmd.exe", script, "ordinary"], {"index": 2, "pattern": ".*"}, "/c echo 22"),
    ("flagless-bounding-row-granted-but-harmless",
     lambda script: ["cmd.exe", script, "ordinary"], {"index": 2, "kind": "path"}, "&echo(22"),
    ("unprobed-switch-slash-t-then-slash-c",
     lambda script: ["cmd.exe", script, "/t:0e", "/c", "echo 11"], {"index": 4, "pattern": ".*"}, "echo 22"),
]


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="no cmd.exe on PATH")
@pytest.mark.parametrize("label,build,spec,value", _F16_CMD_FAMILY_FORMS)
def test_cmd_family_form_is_refused_before_storage_or_launch(layout, label, build, spec, value):
    """Each of the review's four reported forms (`r7_commands.txt:24-27`), its flagless bounding row (`:28`,
    accepted but harmless at the reviewed head only because `cmd.exe` without `/c` starts interactively and
    exits on `stdin=DEVNULL`), and one switch the review did not probe, through the real route
    (`diagnosis.ingest` -> `propose_job_fix` -> `commands.apply_job` -> `ProcessAdapter`): no eligible index,
    nothing stored, the refusal logged, and the real foreground child never runs with the proposed text — its
    stdout is whatever the ORIGINAL, unfixed command prints, taken as the baseline rather than hardcoded, since
    the flagless bounding row's baseline is cmd.exe's own interactive banner, not `11`."""
    cmd_exe = shutil.which("cmd.exe")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")     # never read by cmd.exe in any of these forms either
    original = build(str(script))
    original[0] = cmd_exe       # "cmd.exe" in the builder above is a placeholder; the real path varies by host
    _refused_before_storage_or_launch(layout, original, {"input": spec}, value)


# ---- F17 (round-8 review, docs/reviews/RELEASE_REVIEW_2.1.0_round8.md lines 49, 51-130): F15's own second
# question asks the IMAGE half of the realpath it resolves, never the FAMILY half, and only for argv[1] -- so the
# SAME trailing "\.", "/.", bare trailing separator, or "..\<name>\." re-entry F15 closed for the image question
# was still open wherever a FAMILY question reads argv[0] or argv[1] by its literal, unresolved text:
# `_reparses_regardless_of_argv` at argv[0] (every family: cmd.exe, powershell.exe, wsl.exe, an in-root
# `.bat`/`.cmd`) and the same question at a launcher's own argv[1] target (an in-root `.bat`/`.cmd`). Win32
# normalises each spelling away before CreateProcess ever opens the file and runs the shell or re-invokes the
# batch anyway (review `r8_commands.txt:30-34,36-46,48-52,54-57`).

_F17_REPARSING_FAMILY_SPELLINGS_WITH_A_TRAILING_PATH_COMPONENT = [
    ("cmd.exe-trailing-backslash-dot", "cmd.exe\\."),
    ("cmd.exe-trailing-slash-dot", "cmd.exe/."),
    ("cmd.exe-dotdot-reenter-then-dot", "cmd.exe\\..\\cmd.exe\\."),
    ("cmd-bare-trailing-backslash-dot", "cmd\\."),              # not in the review's seven or T2-88's/T2-89's regressions
    ("powershell.exe-trailing-backslash-dot", "powershell.exe\\."),
    ("wsl.exe-trailing-backslash-dot", "wsl.exe\\."),            # unobservable through a real child on this host (F10's, F16's own records); this unit test needs no subprocess
]


@pytest.mark.skipif(os.name != "nt", reason="Win32's normalisation of a trailing \\., /., \\..\\<name>\\. is Windows-only")
@pytest.mark.parametrize("label,spelling", _F17_REPARSING_FAMILY_SPELLINGS_WITH_A_TRAILING_PATH_COMPONENT)
def test_command_prefix_refuses_a_reparsing_family_at_argv0_spelled_with_a_trailing_path_component(layout, label, spelling):
    """`_resolved_name` at argv[0] is the same lexical basename read F15 already found insufficient at argv[1] —
    "" for a token ending in "\\.", "/.", or a "..\\<name>\\." re-entry — so the re-parsing-family question said
    "not a family" while Win32 normalises the trick away before CreateProcess ever opens the file and runs the
    shell anyway (review `r8_commands.txt:30-32,36-38,42,44`). `cmd\\.` is in neither the review's seven rows nor
    T2-88's or T2-89's own regressions, proving this is the family's rule, resting on the file Windows resolves,
    not one more spelling added to a list."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._resolved_name(spelling) == ""   # the gap: the pure-text question alone reads "not a family"
    assert diagnosis._command_prefix(layout, [spelling, str(script), "/c", "echo 11"]) is None, label


@pytest.mark.skipif(os.name != "nt", reason="Win32's normalisation of a trailing \\. is Windows-only")
def test_command_prefix_refuses_an_in_root_batch_file_at_argv0_spelled_with_a_trailing_path_component(layout):
    """The one re-parsing family whose text test is a suffix, not a name set (REPARSED_EXECUTABLE_SUFFIXES), has
    the same gap at argv[0]: an in-root `.cmd` spelled with a trailing `\\.` resolves to "" — no suffix left to
    match — while the batch is still the file cmd.exe would re-invoke (review `r8_commands.txt:48`)."""
    batch = layout.root / "plain.cmd"
    batch.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    spelling = str(batch) + "\\."
    assert diagnosis._resolved_name(spelling) == ""
    assert diagnosis._command_prefix(layout, [spelling, str(script), "ordinary"]) is None


_F17_LAUNCHER_CMD_TARGET_SPELLINGS = [
    ("trailing-backslash-dot", lambda batch: batch + "\\."),
    ("trailing-slash-dot", lambda batch: batch + "/."),
    ("trailing-backslash", lambda batch: batch + "\\"),
    ("trailing-slash", lambda batch: batch + "/"),
]


@pytest.mark.skipif(os.name != "nt", reason="Win32's normalisation of a trailing \\., /., \\, / is Windows-only")
@pytest.mark.parametrize("label,build", _F17_LAUNCHER_CMD_TARGET_SPELLINGS)
def test_command_prefix_refuses_a_launchers_cmd_target_spelled_with_a_trailing_path_component(layout, label, build):
    """F15's own second question asks the IMAGE half of the realpath it resolves, never the FAMILY half, so a
    launcher's `.cmd` target survived the same four tricks the image question used to miss (review
    `r8_commands.txt:54-57`: `_resolved_name` of the literal text is "", the realpath's own name `plain.cmd`,
    whose family question — never asked before this fix — says true)."""
    batch = layout.root / "plain.cmd"
    batch.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    spelling = build(str(batch))
    assert diagnosis._resolved_name(spelling) == ""
    assert diagnosis._command_prefix(layout, ["env.exe", spelling, "ordinary"]) is None, label


def test_command_prefix_refuses_a_launchers_bat_target_spelled_with_a_trailing_path_component(layout):
    """The same gap for `.bat`, not only `.cmd` — in neither the review's seven rows nor T2-88's or T2-89's own
    regressions, proving the fix reads `REPARSED_EXECUTABLE_SUFFIXES`' own suffix test, not one more extension
    added to a list."""
    batch = layout.root / "plain.bat"
    batch.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    spelling = str(batch) + "\\."
    assert diagnosis._resolved_name(spelling) == ""
    assert diagnosis._command_prefix(layout, ["env.exe", spelling, "ordinary"]) is None


def test_command_prefix_still_accepts_a_non_reparsing_program_at_argv0_spelled_with_a_trailing_path_component(layout):
    """The cost the chosen (resolution-based) shape avoids, named in this task's own record: a text refusal of
    an empty resolved name at argv[0] would also refuse `python.exe\\.` with its own in-root script — a correct
    grant today (review `r8_commands.txt:68`) — because `python.exe` is not a re-parsing family either way, and
    its realpath is still `python.exe`."""
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    assert diagnosis._resolved_name(sys.executable + "\\.") == ""
    assert diagnosis._command_prefix(layout, [sys.executable + "\\.", str(script), "ordinary"]) == 2


# ---- The real-route forms the round-8 review measured granted and injected (`r8_commands.txt:60-66`,
# `r8_extra.txt:3`), through the real route exactly as F15's and F16's own regressions are: no eligible index
# before anything runs, the stored job's argv unchanged, `diagnosis_job_fixed` logs the drop, and the real
# foreground child's stdout is byte-identical before and after — not merely free of the digits "22", which
# round 7's own `injected_ran` flag could misread from a lab-folder timestamp inside cmd.exe's banner.

@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="no cmd.exe on PATH")
def test_cmd_exe_at_argv0_spelled_with_a_trailing_backslash_dot_is_refused_before_storage_or_launch(layout):
    """r8_commands.txt:60: `cmd.exe\\.` at argv[0], an ordinary in-root script at argv[1] (never itself read by
    cmd.exe)."""
    cmd_exe = shutil.which("cmd.exe")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    original = [cmd_exe + "\\.", str(script), "/c", "echo 11"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "echo 22")


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="no cmd.exe on PATH")
def test_cmd_exe_at_argv0_spelled_with_a_trailing_slash_dot_flagless_is_refused_before_storage_or_launch(layout):
    """r8_commands.txt:61: `cmd.exe/.` flagless at argv[0], whose later value itself carries `/c`."""
    cmd_exe = shutil.which("cmd.exe")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    original = [cmd_exe + "/.", str(script), "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 2, "pattern": ".*"}}, "/c echo 22")


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="no cmd.exe on PATH")
def test_cmd_exe_at_argv0_spelled_with_a_dotdot_reentry_is_refused_before_storage_or_launch(layout):
    """r8_commands.txt:62: `cmd.exe\\..\\cmd.exe\\.` at argv[0]."""
    cmd_exe = shutil.which("cmd.exe")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    original = [cmd_exe + "\\..\\cmd.exe\\.", str(script), "/c", "echo 11"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "echo 22")


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="no powershell.exe on PATH")
def test_powershell_at_argv0_spelled_with_a_trailing_path_component_is_refused_before_storage_or_launch(layout):
    """r8_extra.txt:3: `powershell.exe\\.` at argv[0]."""
    ps = shutil.which("powershell.exe")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    original = [ps + "\\.", str(script), ";", "echo 11"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 3, "pattern": ".*"}}, "echo 22")


def test_in_root_batch_at_argv0_spelled_with_a_trailing_path_component_is_refused_before_storage_or_launch(layout):
    """r8_commands.txt:63: the in-root `.cmd` itself at argv[0], spelled `\\.` — the form whose declared program
    cannot even start under its own spelling, yet the value the diagnosis supplied still ran before this fix."""
    batch = layout.root / "plain.cmd"
    batch.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    script = layout.root / "plain.py"
    script.write_text("print(11)\n", encoding="utf-8")
    original = [str(batch) + "\\.", str(script), "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 2, "kind": "path"}}, "&echo(22")


_F17_LAUNCHER_CMD_TARGET_FOREGROUND = [
    ("trailing-backslash-dot", lambda batch: batch + "\\."),
    ("trailing-slash-dot", lambda batch: batch + "/."),
    ("trailing-backslash", lambda batch: batch + "\\"),
    ("trailing-slash", lambda batch: batch + "/"),
]


@pytest.mark.skipif(os.name != "nt", reason="launches env.exe over an in-root .cmd")
@pytest.mark.parametrize("label,build", _F17_LAUNCHER_CMD_TARGET_FOREGROUND)
def test_launcher_over_an_in_root_batch_target_spelled_with_a_trailing_path_component_is_refused_before_storage_or_launch(layout, label, build):
    """r8_commands.txt:64-66: `env.exe` over an in-root `.cmd` spelled `\\.`, `/.`, or a bare trailing separator
    at argv[1] — the slot F10's and F14's own containment question already reads."""
    env_exe = _find_env_exe()
    _skip_if_missing(env_exe)
    batch = layout.root / "plain.cmd"
    batch.write_text("@echo off\r\necho 11\r\n", encoding="utf-8")
    original = [env_exe, build(str(batch)), "ordinary"]
    _refused_before_storage_or_launch(layout, original, {"input": {"index": 2, "kind": "path"}}, "&echo(22")
