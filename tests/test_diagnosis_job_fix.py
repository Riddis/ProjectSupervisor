"""2.1.0 change 2: a manager diagnosis's `requeue` may propose a corrected job for a non-agent (process) job —
the one thing a bare requeue cannot repair, since escalation.dispatch_revision rebuilds the job from
scheduler.job_spec() unchanged (PHASE2.md §2, F-A). Applied through task.set_job (commands.apply_job) as the
single writer, logged, and bounded: only the fields diagnosis.JOB_FIX_FIELDS names for the job's own adapter may
change, so it can never widen owned_paths, change the job's adapter (its lane confinement) or raise a limit."""
import json
import sys

from supervisor import diagnosis, jobs, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_exception_route_and_checkpoints import run_until


def process_task(layout, tid, argv, timeout_s=60, owned=()):
    make_task(layout, tid)
    tasks._update(layout, tid, lambda d: d.update(job={"adapter": "process", "argv": argv, "timeout_s": timeout_s},
                                                  artifacts=[f"{tid}.txt"], owned_paths=list(owned)))
    tasks.set_status(layout, tid, "BLOCKED", note="stalled: nothing left to retry")


def diag_rec(layout, tid, attempt="T.a9"):
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "claude_cli", "phase": "RUNNING",
                              "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": "manager",
                              "diag_role": "senior_pm", "diag_signature": "s1", "diag_issue": None})


def test_a_diagnosis_corrected_argv_is_applied_through_task_set_job_runs_and_is_logged(layout):
    bad_argv = [sys.executable, "-c", "import sys; sys.exit(1)"]
    good_argv = [sys.executable, "-c",
                "import os, json, pathlib; pathlib.Path('T.txt').write_text('ok'); "
                "pathlib.Path(os.environ['SUPERVISOR_RUN_DIR'], 'result.json').write_text("
                "json.dumps({'status': 'COMPLETE', 'artifacts': ['T.txt']}))"]
    process_task(layout, "T", bad_argv)
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "its argv always exits 1",
                                                                      "guidance": "the command itself was wrong",
                                                                      "job_fix": {"argv": good_argv}}))
    assert out["job_fix"] == {"applied": {"argv": good_argv}, "dropped": []}
    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job == {"adapter": "process", "argv": good_argv, "timeout_s": 60}   # only argv moved
    assert tasks.load(layout, "T").status == TaskStatus.READY
    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "task_job_set" and e["job"]["argv"] == good_argv for e in events)
    assert any(e["event"] == "diagnosis_job_fixed" and e["applied"] == {"argv": good_argv} for e in events)
    # the corrected argv is what actually runs next
    run_until(layout, lambda: tasks.load(layout, "T").status == TaskStatus.COMPLETE, timeout=30)
    assert (layout.root / "T.txt").read_text() == "ok"


def test_a_job_fix_cannot_change_the_adapter_raise_a_limit_or_touch_owned_paths(layout):
    process_task(layout, "T", [sys.executable, "-c", "pass"], timeout_s=60, owned=["only/this"])
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    malicious = {"argv": ["good"], "adapter": "claude_cli", "timeout_s": 999999, "fn": "evil", "owned_paths": ["."]}
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "x", "guidance": "g",
                                                                      "job_fix": malicious}))
    job = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert job == {"adapter": "process", "argv": ["good"], "timeout_s": 60}        # adapter and timeout_s untouched
    assert tasks.load(layout, "T").owned_paths == ["only/this"]                    # never written by a job_fix
    assert out["job_fix"]["dropped"] == ["adapter", "fn", "owned_paths", "timeout_s"]


def test_a_job_fix_is_ignored_for_an_agent_job(layout):
    make_task(layout, "T")                                       # cost_category claude -> adapter claude_cli
    tasks.set_status(layout, "T", "BLOCKED", note="stalled")
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "x", "guidance": "g",
                                                                      "job_fix": {"argv": ["should never apply"]}}))
    assert "job_fix" not in out
    assert "job" not in (tasks.load(layout, "T").model_extra or {})
