"""R1 (docs/reviews/RELEASE_REVIEW_2.1.0.md): a manager diagnosis's `job_fix` must go through a declared
correction interface, not a free `argv`. supervisor/diagnosis.py's old `propose_job_fix` merged whatever `argv`
a diagnosis proposed straight into the job (JOB_FIX_FIELDS = {"process": ("argv",)}), so a read-only diagnosis
could make the process adapter launch executable code of its own choosing on the next delivery — reproduced by
the reviewer's probe (runs/T2-05/T2-05.a1/probes.py, observation `arbitrary_argv`). The fix lets a job declare,
ahead of time and only through the single writer (`task.set_job` / `commands.apply_job`, never a diagnosis), a
`fix_params` schema naming which argv positions (never index 0, the interpreter, or index 1, the script) a
diagnosis may later correct and what pattern a proposed value must match; `job_fix` itself may now only ever
name `params`, never `argv`."""
import json
import subprocess
import sys

from supervisor import diagnosis, tasks
from supervisor.adapters.base import Result
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from tests.test_diagnosis_job_fix import diag_rec, process_task
from tests.test_exception_route_and_checkpoints import run_until


def test_marker_writing_job_fix_is_refused_before_storage_or_launch(layout):
    """The reviewer's probe: a diagnosis proposes a corrected `argv` that actually writes a marker file outside
    the project. It must be dropped — never stored on the task's job, never the command the adapter next
    launches — while the task's `owned_paths` and the rest of the job stay exactly as they were."""
    outside = layout.root.parent / "outside-project-marker.txt"
    bad_argv = [sys.executable, "-c", "raise SystemExit(1)"]
    marker_argv = [sys.executable, "-c",
                   "from pathlib import Path; Path(" + repr(str(outside)) + ").write_text('probe only')"]
    process_task(layout, "T", bad_argv, owned=["only/this"])
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "repair job", "guidance": "retry",
                                                                      "job_fix": {"argv": marker_argv}}))
    assert out.get("job_fix", {}).get("applied") == {}
    assert "argv" in out.get("job_fix", {}).get("dropped", [])

    task = tasks.load(layout, "T")
    job = (task.model_extra or {}).get("job")
    assert job["argv"] == bad_argv                    # the stored command is still the original, unfixed one
    assert task.owned_paths == ["only/this"]

    # The exact command/env/cwd the process adapter would launch next must also be the unchanged, original one —
    # confinement cannot be disproved after the fact, so this is checked before anything runs.
    spec = ProcessAdapter().prepare(layout, task, "T.a10")
    assert spec["argv"] == bad_argv
    child = subprocess.run(ProcessAdapter().command(spec), cwd=spec["root"], timeout=15)
    assert child.returncode == 1                      # the original command ran (and failed, as it always did)
    assert not outside.exists()                       # the marker was never written

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "diagnosis_job_fixed" and "argv" in (e.get("dropped") or []) for e in events)


def test_stale_sha_correction_is_applied_through_params_runs_and_is_logged(layout):
    """The intended case the interface exists for: a release_checks-style job was launched with a stale --sha;
    the diagnosis's correction names only the declared `sha` parameter, is validated against its declared
    pattern, applied through the single writer (task.set_job's own commands.apply_job), and is what actually
    runs next."""
    script = layout.root / "check_sha.py"
    script.write_text(
        "import sys, os, json, pathlib\n"
        "sha = sys.argv[sys.argv.index('--sha') + 1]\n"
        "ok = sha == 'cafebabe00112233445566778899aabbccddeeff'\n"
        "pathlib.Path('T.txt').write_text('ok' if ok else 'bad')\n"
        "pathlib.Path(os.environ['SUPERVISOR_RUN_DIR'], 'result.json').write_text(json.dumps(\n"
        "    {'status': 'COMPLETE' if ok else 'FAILED', 'artifacts': ['T.txt']}))\n",
        encoding="utf-8")
    stale_sha = "0" * 40
    good_sha = "cafebabe00112233445566778899aabbccddeeff"
    argv = [sys.executable, str(script), "--sha", stale_sha]
    process_task(layout, "T", argv)
    tasks._update(layout, "T", lambda d: d["job"].update(fix_params={"sha": {"index": 3, "pattern": r"^[0-9a-f]{40}$"}}))

    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "its --sha is stale",
                                                                      "guidance": "the pinned commit moved",
                                                                      "job_fix": {"params": {"sha": good_sha}}}))
    assert out["job_fix"] == {"applied": {"params": {"sha": good_sha}}, "dropped": []}

    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job["argv"] == [sys.executable, str(script), "--sha", good_sha]        # only the declared slot moved
    assert tasks.load(layout, "T").owned_paths == []                              # job_fix never touches ownership

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "task_job_set" and e["job"]["argv"][3] == good_sha for e in events)
    assert any(e["event"] == "diagnosis_job_fixed" and e["applied"] == {"params": {"sha": good_sha}} for e in events)

    run_until(layout, lambda: tasks.load(layout, "T").status.value == "COMPLETE", timeout=30)
    assert (layout.root / "T.txt").read_text() == "ok"
