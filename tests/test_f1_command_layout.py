"""Round-2 review F1 (docs/reviews/RELEASE_REVIEW_2.1.0_round2.md lines 25-49): the trusted-script check at
the old `propose_job_fix` treated argv[1] as the script whenever it did not start with "-". On Windows,
`cmd.exe /c <script>` passes that check — "/c" is an interpreter option, not the script, and the actual script
is at argv[2] — so a predeclared `fix_params: {"input": {"index": 2, "kind": "path"}}` made the script itself
mutable: a diagnosis could select different executable code without ever touching argv[0] or argv[1].

The fix (`diagnosis._command_prefix`) reads the argv's own layout instead of assuming argv[1] is always the
script: a wrapper whose child re-parses its own command line (cmd.exe/cmd with /c or /k, among others —
SHELL_REPARSING) has NO eligible index at all, at any position, because the child's own parser — not an index —
decides where one argument ends and the next command begins (round-2 architecture ruling: a `kind: path` value
with no spaces and an "&" in it still makes cmd.exe run a second program, however far past the wrapper's own
prefix the declared index sits). The plain layout (argv[0] executable, argv[1] script) stays eligible from
index 2 on, decided by whether argv[1] is an actual file — not by whether it starts with "-" or "/", which
would refuse every POSIX absolute script path too."""
import json
import os
import subprocess
import sys

import pytest

from supervisor import diagnosis, tasks
from supervisor.adapters.base import Result
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from tests.test_diagnosis_job_fix import diag_rec, process_task

COMSPEC = os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe")


@pytest.mark.skipif(os.name != "nt", reason="launches cmd.exe via COMSPEC")
def test_windows_cmd_script_replacement_is_refused_before_storage_or_launch(layout):
    """The round-2 reviewer's probe (runs/T2-32/T2-32.a1/probes.py, observation
    `windows_script_replacement`): a disposable task runs `cmd.exe /c original.cmd`, owns only `only/this`, and
    declares `fix_params: {"input": {"index": 2, "kind": "path"}}`. A diagnosis proposes an in-project alternate
    script at that index. It must be dropped — never stored on the task's job, never the command the adapter
    next launches — so the alternate script's marker is never written."""
    original = layout.root / "original.cmd"
    original.write_text("@exit /b 0\n", encoding="ascii")
    marker = layout.root.parent / "outside-cmd-marker.txt"
    alternate = layout.root / "alternate.cmd"
    alternate.write_text('@echo probe only>"' + str(marker) + '"\n', encoding="ascii")
    comspec = COMSPEC
    process_task(layout, "T", [comspec, "/c", str(original)], owned=["only/this"],
                 fix_params={"input": {"index": 2, "kind": "path"}})
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "repair job", "guidance": "retry",
                                                                      "job_fix": {"params": {"input": str(alternate)}}}))
    assert out.get("job_fix", {}).get("applied") == {}
    assert "params.input" in out.get("job_fix", {}).get("dropped", [])

    task = tasks.load(layout, "T")
    job = (task.model_extra or {}).get("job")
    assert job["argv"] == [comspec, "/c", str(original)]          # the stored command is still the original one
    assert task.owned_paths == ["only/this"]

    # The exact command the process adapter would launch next must also be the unchanged, original one.
    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == [comspec, "/c", str(original)]
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], timeout=15)
    assert child.returncode == 0                                  # original.cmd's own harmless exit
    assert not marker.exists()                                    # the alternate script never ran

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)


def test_a_command_bearing_slot_is_refused_not_narrowed_to_a_different_index(layout):
    """A declaration that names the command-bearing slot itself (index 2, the actual script for a
    `cmd.exe /c <script>` layout) is refused outright for every value, including one that would otherwise pass
    every other check (a plain, in-project path) — proving the index is what is refused, not quietly
    reinterpreted as if it meant some other, safe slot."""
    comspec = COMSPEC
    script = layout.root / "original.cmd"
    script.write_text("@exit /b 0\n", encoding="ascii")
    safe_alternate = layout.root / "data" / "safe.cmd"
    safe_alternate.parent.mkdir(parents=True)
    safe_alternate.write_text("@exit /b 0\n", encoding="ascii")
    process_task(layout, "T", [comspec, "/c", str(script)],
                 fix_params={"input": {"index": 2, "kind": "path"}})
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "x", "guidance": "g",
                                                                      "job_fix": {"params": {"input": str(safe_alternate)}}}))
    assert out["job_fix"] == {"applied": {}, "dropped": ["params.input"]}
    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job["argv"] == [comspec, "/c", str(script)]


def test_a_wrapped_shells_argument_past_its_own_prefix_has_no_eligible_index_either(layout):
    """Architecture ruling (round-2 review F1): a wrapper whose child re-parses its own command line has NO slot
    that is provably safe to mutate, however far past the wrapper's own prefix the declared index sits — the
    child's own parser, not an index, decides where one argument ends and the next command begins. `cmd.exe /c
    <script> --sha <stale>` declaring `sha` at index 4 (one slot after the executable+switch+script) must still
    be refused outright, the same as declaring the script itself (index 2): registering this wrapper as a
    layout with a usable, mutable tail would let a `kind: path`-free value like an "&"-bearing argument run a
    second program through cmd.exe's own re-parsing, the invariant this interface exists to hold."""
    script = layout.root / "check_sha.cmd"
    script.write_text("@exit /b 0\n", encoding="ascii")
    comspec = COMSPEC
    stale_sha = "0" * 40
    good_sha = "cafebabe00112233445566778899aabbccddeeff"
    argv = [comspec, "/c", str(script), "--sha", stale_sha]
    process_task(layout, "T", argv, fix_params={"sha": {"index": 4, "pattern": r"^[0-9a-f]{40}$"}})

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "its --sha is stale",
                                                                      "guidance": "the pinned commit moved",
                                                                      "job_fix": {"params": {"sha": good_sha}}}))
    assert out["job_fix"] == {"applied": {}, "dropped": ["params.sha"]}

    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job["argv"] == argv                                                # nothing moved: the stale sha stays

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.sha" in (e.get("dropped") or []) for e in events)

    # the unchanged, stale command is what the adapter would actually launch next
    spec = ProcessAdapter().prepare(layout, tasks.load(layout, "T"), "T.a10")
    assert spec["argv"] == argv


# `_command_prefix` itself, platform-independent: no subprocess, no COMSPEC, so both the Windows and the ubuntu
# leg of G3 exercise the layout contract directly, not only through a Windows-only cmd.exe scenario.

def test_command_prefix_accepts_an_absolute_posix_script_path(layout):
    """A POSIX absolute script path starts with "/" too — the old "argv[1] starts with / or -" blacklist
    refused it outright, which would have made every Linux/macOS process job's parameters uncorrectable."""
    script = layout.root / "run.py"
    script.write_text("pass\n", encoding="utf-8")
    assert diagnosis._command_prefix(layout, ["/usr/bin/python3", str(script), "--mode", "fail"]) == 2


def test_command_prefix_refuses_a_script_slot_that_is_not_an_actual_file(layout):
    assert diagnosis._command_prefix(layout, ["/usr/bin/python3", "-c", "print(1)"]) is None
    assert diagnosis._command_prefix(layout, ["/usr/bin/env", "VAR=1"]) is None
    assert diagnosis._command_prefix(layout, ["/usr/bin/python3", "run"]) is None     # "run": no such file either


def test_command_prefix_refuses_every_shell_that_reparses_its_own_command_line(layout):
    script = layout.root / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    for argv in (["cmd.exe", "/c", str(script)], ["cmd.exe", "/k", str(script)], ["cmd", "/c", str(script)],
                 ["powershell.exe", "-Command", str(script)], ["pwsh", "-Command", str(script)],
                 ["/bin/sh", "-c", str(script)], ["/bin/bash", "-c", str(script)]):
        assert diagnosis._command_prefix(layout, argv) is None, argv


def test_command_prefix_refuses_too_few_tokens(layout):
    assert diagnosis._command_prefix(layout, ["/usr/bin/python3"]) is None
    assert diagnosis._command_prefix(layout, []) is None


# Round-3 review F5 (docs/reviews/RELEASE_REVIEW_2.1.0_round3.md lines 33-58): the file-existence check above
# treated ANY existing file named by argv[1] as proof of a trusted script, including a file that happens to
# share a name with an interpreter option. A project containing an ordinary file named "-c" made
# `_command_prefix` return 2 for `[python, '-c', <program>]`, exposing the inline program itself (index 2) as a
# correctable `fix_params` parameter — the exact regression round 2 already closed by rejecting an argv[1]
# starting with "-". The command's own grammar, not a filesystem collision, decides this: an argv[1] that is an
# interpreter option is never a script, whatever happens to exist on disk under that name.

def test_command_prefix_refuses_an_interpreter_option_even_when_a_same_named_file_exists(layout):
    (layout.root / "-c").write_text("fixture, never executed", encoding="utf-8")
    (layout.root / "-m").write_text("fixture, never executed", encoding="utf-8")
    assert diagnosis._command_prefix(layout, [sys.executable, "-c", "print(1)"]) is None
    assert diagnosis._command_prefix(layout, [sys.executable, "-m", "json.tool"]) is None
    # the general invariant, not a longer exact-pair blacklist: an unrelated "-"-prefixed exe/option combination,
    # one SHELL_REPARSING was never meant to list, is refused the same way.
    assert diagnosis._command_prefix(layout, ["/usr/bin/ruby", "-e", "puts 1"]) is None


def test_an_option_lookalike_file_grants_no_prefix_so_the_proposal_is_refused_before_storage_or_launch(layout):
    """The round-3 reviewer's probe (runs/T2-47/T2-47.a2/extra.py, observation `option_file_lookalike`): a
    disposable project contains an ordinary file named "-c", a task runs `[python, '-c', 'print(11)']`, owns
    only `only/this`, and declares `fix_params: {"input": {"index": 2, "kind": "path"}}`. A diagnosis proposing
    only `print(22)` must be refused outright — never stored on the task's job, never the command the adapter
    next launches — so the inline program the child actually runs stays `print(11)`."""
    lookalike = layout.root / "-c"
    lookalike.write_text("fixture, never executed", encoding="utf-8")
    original = [sys.executable, "-c", "print(11)"]
    process_task(layout, "T", original, owned=["only/this"], fix_params={"input": {"index": 2, "kind": "path"}})
    assert diagnosis._command_prefix(layout, original) is None       # the lookalike file grants no prefix at all

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "repair job", "guidance": "retry",
                                                                      "job_fix": {"params": {"input": "print(22)"}}}))
    assert out.get("job_fix", {}).get("applied") == {}
    assert "params.input" in out.get("job_fix", {}).get("dropped", [])

    task = tasks.load(layout, "T")
    job = (task.model_extra or {}).get("job")
    assert job["argv"] == original                                  # argv[1] (the option) and argv[2] untouched
    assert task.owned_paths == ["only/this"]

    # the exact command the process adapter would launch next must also be the unchanged, original one
    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == original
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], capture_output=True, text=True, timeout=15)
    assert child.returncode == 0 and child.stdout.strip() == "11"    # the original inline program ran, not "22"

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "params.input" in (e.get("dropped") or []) for e in events)


def test_the_legitimate_stale_sha_correction_still_applies_through_the_single_writer(layout):
    """The lookalike fix must not narrow the legitimate case it sits alongside: a real absolute script path
    with a declared parameter (R1's own stale-sha positive case,
    tests/test_r1_job_fix_interface.py::test_stale_sha_correction_is_applied_through_params_runs_and_is_logged)
    is still corrected through commands.apply_job, in the same project that also contains an interpreter-option
    lookalike file."""
    (layout.root / "-c").write_text("fixture, never executed", encoding="utf-8")   # same disposable project
    script = layout.root / "check_sha.py"
    script.write_text("pass\n", encoding="utf-8")
    stale_sha = "0" * 40
    good_sha = "cafebabe00112233445566778899aabbccddeeff"
    argv = [sys.executable, str(script), "--sha", stale_sha]
    process_task(layout, "T", argv, fix_params={"sha": {"index": 3, "pattern": r"^[0-9a-f]{40}$"}})
    assert diagnosis._command_prefix(layout, argv) == 2              # the real script still yields its prefix

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "its --sha is stale",
                                                                      "guidance": "the pinned commit moved",
                                                                      "job_fix": {"params": {"sha": good_sha}}}))
    assert out["job_fix"] == {"applied": {"params": {"sha": good_sha}}, "dropped": []}
    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job["argv"] == [sys.executable, str(script), "--sha", good_sha]
