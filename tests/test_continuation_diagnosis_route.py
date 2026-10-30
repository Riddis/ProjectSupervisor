"""2.1.0 change 1 (D-010 Consequences): continuations exhausted now routes through the manager's diagnosis
(one bounded requeue/defer/architect/owner decision) instead of straight to a BLOCKED "owner decision needed"
note that only the owner's inbox ever saw. The continuation cap itself still holds, and the diagnosis route's own
requeue budget (DIAGNOSIS_BUDGET) still bounds how many times a diagnosis may grant a fresh continuation chain."""
import os
import time

from supervisor import diagnosis, inbox, jobs, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_exception_route_and_checkpoints import diag_records, fake_claude, run_until  # noqa: F401


def continuation_chain(layout, tid, start=1):
    """MAX_CONTINUATIONS prior continuation attempts already recorded for `tid` (attempt numbers `start`..), and
    one more (the task's newest attempt) still running and about to defer again — the next requeue() call for it
    must hit the cap. `start` must be past any other attempt already recorded for `tid`: requeue()'s own crash-safety
    check ("a newer attempt already exists") keys off the attempt number alone."""
    n = Supervisor.MAX_CONTINUATIONS
    jobs.write(layout, {"attempt_id": f"{tid}.a{start}", "task_id": tid, "adapter": "python", "phase": "DONE",
                        "spec": {"kind": "delivery"}, "handle": {}, "result": "DEFERRED"})
    for i in range(start + 1, start + n + 1):
        fields = {"attempt_id": f"{tid}.a{i}", "task_id": tid, "adapter": "python", "spec": {"kind": "delivery"},
                  "handle": {}, "deferred_reason": "continuation after capacity limit"}
        fields.update({"phase": "DONE", "result": "DEFERRED", "continued": True} if i < start + n else {"phase": "RUNNING"})
        jobs.write(layout, fields)
    return jobs.read(layout, f"{tid}.a{start + n}")


def test_continuations_exhausted_reaches_manager_diagnosis_not_the_inbox(layout, fake_claude):
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    rec = continuation_chain(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    # the cap fired, but it did not go straight to the owner the way it used to
    assert jobs.read(layout, rec["attempt_id"])["phase"] == "DONE"
    assert inbox.open_items(layout, "owner") == []
    assert (layout.issues / "I-001.yaml").exists()
    note = next(h["note"] for h in tasks.load(layout, "T").history if h["to"] == "BLOCKED")
    assert "owner decision needed" not in note          # the old marker that sent it to request_owner_decisions()
    run_until(layout, lambda: any(r.get("diag_action") for r in diag_records(layout, "T")))
    recs = diag_records(layout, "T")
    assert len(recs) == 1 and recs[0]["diag_level"] == "manager" and recs[0]["diag_action"] == "requeue"
    assert inbox.open_items(layout, "owner") == []
    assert tasks.load(layout, "T").status == TaskStatus.READY   # the diagnosis granted a fresh continuation budget


def test_the_continuation_cap_is_still_enforced_once_the_diagnosis_budget_is_spent(layout, fake_claude):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    for n in range(1, diagnosis.DIAGNOSIS_BUDGET + 1):
        rec = jobs.write(layout, {"attempt_id": f"T.a{90 + n}", "task_id": "T", "adapter": "claude_cli", "phase": "RUNNING",
                                  "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": "manager", "diag_role": "senior_pm",
                                  "diag_signature": f"s{n}", "diag_issue": None})
        out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                          "summary": "again", "guidance": "try harder"}))
        assert out["action"] == "requeue"
        tasks.set_status(layout, "T", "IN_PROGRESS")
    assert diagnosis.count_requeues(layout, "T") == diagnosis.DIAGNOSIS_BUDGET
    rec4 = continuation_chain(layout, "T", start=100)   # past the diagnosis records' attempt numbers (90+n) above
    sup.requeue(rec4, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    # the budget is already spent: this time the cap goes straight to the owner, not to a third diagnosis
    assert len(inbox.open_items(layout, "owner")) == 1
    assert len([r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]) == diagnosis.DIAGNOSIS_BUDGET
