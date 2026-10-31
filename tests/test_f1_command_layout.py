"""Round-2 review F1 (docs/reviews/RELEASE_REVIEW_2.1.0_round2.md lines 25-49): the trusted-script check at
the old `propose_job_fix` treated argv[1] as the script whenever it did not start with "-". On Windows,
`cmd.exe /c <script>` passes that check — "/c" is an interpreter option, not the script, and the actual script
is at argv[2] — so a predeclared `fix_params: {"input": {"index": 2, "kind": "path"}}` made the script itself
mutable: a diagnosis could select different executable code without ever touching argv[0] or argv[1]. The fix
(`diagnosis._command_prefix`) reads the argv's own layout instead of assuming argv[1] is always the script: a
registered wrapper (cmd.exe/cmd with /c or /k) moves the trusted prefix to 3 slots so the real script at index 2
stays ineligible, and any other argv[1] that looks like an option (either "-" or "/" prefixed) has no eligible
index at all — refusing the whole command rather than narrowing it to some other, assumed-safe slot."""
import json
import os
import subprocess

from supervisor import diagnosis, tasks
from supervisor.adapters.base import Result
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from tests.test_diagnosis_job_fix import diag_rec, process_task

COMSPEC = os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe")


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


def test_an_argument_after_a_wrapped_scripts_trusted_prefix_is_still_correctable(layout):
    """The retained, intended case, now through the one registered wrapper layout: `cmd.exe /c <script> --sha
    <stale>` declares the `sha` parameter at index 4 — one slot after the wrapper's own trusted prefix (3, for
    `cmd.exe /c`: the executable, the switch and the script it names). The correction is applied through the
    single writer and logged; the script itself (index 2) never moves."""
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
    assert out["job_fix"] == {"applied": {"params": {"sha": good_sha}}, "dropped": []}

    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job["argv"] == [comspec, "/c", str(script), "--sha", good_sha]     # only the declared slot moved
    assert job["argv"][2] == str(script)                                     # the script itself never moved

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "task_job_set" and e["job"]["argv"][4] == good_sha for e in events)
    assert any(e["event"] == "diagnosis_job_fixed" and e["applied"] == {"params": {"sha": good_sha}} for e in events)

    # the corrected command is what the adapter would actually launch next
    spec = ProcessAdapter().prepare(layout, tasks.load(layout, "T"), "T.a10")
    assert spec["argv"] == [comspec, "/c", str(script), "--sha", good_sha]
