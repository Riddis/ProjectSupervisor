"""D-018 (state/decisions/D-018-job-fix-out-of-2.1.0.md): 2.1.0 ships with no way for a diagnosis to change a
task's job. A `requeue` that carries a `job_fix` is read exactly like one that does not: the job block is left
byte-identical, nothing about it is logged, and the ordinary guidance-only requeue still happens. A process job
whose own command is wrong reaches the owner as a question naming the `task.set_job` it needs, not a diagnosis
correction — `RESULT_CONTRACT` says so and no longer documents a `job_fix` field at all."""
import json
import sys

from supervisor import diagnosis, jobs, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def process_task(layout, tid, argv, timeout_s=60, fix_params=None):
    make_task(layout, tid)
    job = {"adapter": "process", "argv": argv, "timeout_s": timeout_s}
    if fix_params:
        job["fix_params"] = fix_params
    tasks._update(layout, tid, lambda d: d.update(job=job, artifacts=[f"{tid}.txt"]))
    tasks.set_status(layout, tid, "BLOCKED", note="stalled: nothing left to retry")


def diag_rec(layout, tid, attempt="T.a9"):
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "claude_cli", "phase": "RUNNING",
                              "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": "manager",
                              "diag_role": "senior_pm", "diag_signature": "s1", "diag_issue": None})


def test_a_requeue_job_fix_changes_nothing_about_the_job_and_logs_nothing(layout):
    argv = [sys.executable, "run.py", "--mode", "fail"]
    process_task(layout, "T", argv, fix_params={"mode": {"index": 3, "pattern": r"^[a-z]+$"}})
    before = dict((tasks.load(layout, "T").model_extra or {}).get("job"))
    sup = Supervisor(layout, sleep_s=0)
    rec = diag_rec(layout, "T")
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                      "summary": "its mode was wrong",
                                                                      "guidance": "the job itself needed --mode succeed",
                                                                      "job_fix": {"params": {"mode": "succeed"}}}))
    after = (tasks.load(layout, "T").model_extra or {}).get("job")
    assert after == before                                  # the job block is byte-identical
    assert "job_fix" not in out
    assert out["action"] == "requeue" and out["guidance"] == "the job itself needed --mode succeed"
    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines()]
    assert not any(e["event"] in ("task_job_set", "diagnosis_job_fixed") for e in events)
    assert tasks.load(layout, "T").status == TaskStatus.READY   # the ordinary requeue still happens


def test_result_contract_names_no_job_fix_and_sends_a_wrong_process_command_to_the_architect():
    """A manager still cannot change a job; since the architect `revise` (tests/test_diagnosis_revise.py) it sends
    a wrong command to the architect with guidance, and with `escalation.revise: false` to the owner as before."""
    contract = diagnosis.RESULT_CONTRACT
    assert "job_fix" not in contract
    assert "You cannot change the contract; the architect can" in contract
    off = diagnosis.result_contract("manager", revise=False)
    assert "task.set_job" in off and "owner" in off and "job_fix" not in off
