"""Hold is the one moment the owner most
needs it - a task has just failed and an automatic retry is about to race whatever they are repairing - and that
is exactly the moment `tasks.hold()` refuses: `ALLOWED[FAILED]` carries no `BLOCKED` (`models/task.py`), so
`hold()`'s own `TaskStatus.BLOCKED not in ALLOWED[current]` check raises before it ever reaches `held`. The same
review also found the task page offering Hold, Cancel and Re-queue for every status alike, whatever the status
refuses: five of eight failed owner commands in the records were exactly that - an illegal transition the page
had offered."""
from unittest.mock import patch

import pytest

from supervisor import escalation, gate, jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def _prepared_retry(layout, tid, attempt="T.a2"):
    """A PREPARED delivery record, the shape the automatic retry after a failure leaves behind (requeue()/
    dispatch_revision() queue it crash-safe either way, win or lose a race with the owner's own Hold)."""
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "process", "phase": "PREPARED",
                               "spec": {"kind": "delivery"}, "deferred_until": 0})


# ------------------------------------------------------------------ hold() itself


def test_hold_succeeds_on_a_failed_task_without_force(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")

    tasks.hold(layout, "T", "owner-ui", "investigating before the retry runs")

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED
    assert task.held and task.held["by"] == "owner-ui"
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"


# ------------------------------------------------------------------ the queued retry and release


def test_a_queued_retry_does_not_launch_while_the_failed_task_is_held(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")
    rec = _prepared_retry(layout, "T")

    tasks.hold(layout, "T", "owner-ui", "investigating")

    task = tasks.load(layout, "T")
    reason = gate.check(layout, task, "process", "delivery", [])
    assert reason is not None and "held" in reason.lower()
    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        launched = Supervisor(layout, sleep_s=0).launch(jobs.read(layout, rec["attempt_id"]))
    assert launched["phase"] == "PREPARED"            # never SUBMITTED
    assert launched.get("held")
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED


def test_releasing_the_hold_lets_the_retry_proceed_within_the_same_budget(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")
    rec = _prepared_retry(layout, "T")
    tasks.hold(layout, "T", "owner-ui", "investigating")
    revisions_before = escalation._load(layout, "T")["counters"]["total"]

    tasks.set_status(layout, "T", "READY", actor="owner-ui", note="re-queued by owner")   # the owner's release

    task = tasks.load(layout, "T")
    assert task.held is None
    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        launched = Supervisor(layout, sleep_s=0).launch(jobs.read(layout, rec["attempt_id"]))
    assert launched["phase"] == "SUBMITTED"           # the same record now launches
    assert escalation._load(layout, "T")["counters"]["total"] == revisions_before   # no extra revision spent


# ------------------------------------------------------------------ only legal actions, for every status


ACTIONS = ("hold", "cancel", "requeue")


def _force_into(layout, tid, status):
    tasks.set_status(layout, tid, status.value, actor="owner", note="test setup", force=True)


def _probe(layout, tid, action):
    """True when the named task-page action (hold/cancel/requeue, the production route's own targets) is
    refused by the real command for the task's current status."""
    try:
        if action == "hold":
            tasks.hold(layout, tid, "owner-ui", "probe")
        elif action == "cancel":
            tasks.set_status(layout, tid, "CANCELLED", actor="owner-ui", note="probe")
        else:
            status = tasks.load(layout, tid).status
            target = "NOT_STARTED" if status == TaskStatus.CANCELLED else "READY"
            tasks.set_status(layout, tid, target, actor="owner-ui", note="probe")
        return False
    except tasks.TaskError:
        return True


@pytest.mark.parametrize("status", list(TaskStatus))
@pytest.mark.parametrize("action", ACTIONS)
def test_the_task_page_offers_no_action_the_command_would_refuse(layout, status, action):
    make_task(layout, "T")
    _force_into(layout, "T", status)

    refused = _probe(layout, "T", action)

    reason = tasks.action_blocked_reason(action, status)
    assert (reason is not None) == refused, (
        f"{action} at {status.value}: page reason={reason!r}, command refused={refused}")
