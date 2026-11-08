"""2.4.0 G6: an automatic revision must wait for its dependencies. The check lives in the launch gate (`gate.check`),
not in `escalation.dispatch_revision`, so every launch path is covered alike; `tasks.eligible()` is refactored onto
the same predicate. A FAILED task with an unmet dependency is not re-dispatched: the queued record stays PREPARED,
held, and launches once the dependency completes — within the same attempt id, no second revision spent."""
from unittest.mock import patch

from supervisor import escalation, gate, jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_escalation import FakeSupervisor


def _complete(layout, tid):
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.record_delivery(layout, tid, "d" * 16, actor="test", paths=["x"])
    tasks.record_verification(layout, tid, "d" * 16, True, detail="ok")
    tasks.advance(layout, tid)


def _prepared_continuation(layout, tid, attempt):
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "process", "phase": "PREPARED",
                               "spec": {"kind": "delivery"}, "deferred_until": 0})


def test_unmet_dependency_names_the_first_incomplete_dependency(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    assert tasks.unmet_dependency(layout, tasks.load(layout, "T")) == "DEP (NOT_STARTED)"
    _complete(layout, "DEP")
    assert tasks.load(layout, "DEP").status == TaskStatus.COMPLETE
    assert tasks.unmet_dependency(layout, tasks.load(layout, "T")) is None


def test_gate_refuses_a_delivery_whose_dependency_is_unmet(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    task = tasks.load(layout, "T")
    reason = gate.check(layout, task, "process", "delivery", [])
    assert reason is not None and "waiting on DEP" in reason


def test_gate_does_not_refuse_a_diagnosis_launch_while_a_dependency_is_unmet(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    task = tasks.load(layout, "T")
    assert gate.check(layout, task, "claude_cli", "diagnosis", []) is None


def test_eligible_still_refuses_a_task_whose_dependency_is_unmet(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    assert "T" not in [t.id for t in tasks.eligible(layout)]           # DEP itself is eligible; T waits on it
    _complete(layout, "DEP")
    assert [t.id for t in tasks.eligible(layout)] == ["T"]


def test_ensure_in_progress_leaves_a_failed_tasks_status_alone_while_its_dependency_is_unmet(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")
    rec = _prepared_continuation(layout, "T", "T.a2")

    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        launched = Supervisor(layout, sleep_s=0).launch(rec)

    assert launched["phase"] == "PREPARED"
    assert launched.get("held") and "waiting on DEP" in launched["held"]
    assert tasks.load(layout, "T").status == TaskStatus.FAILED      # never flipped to IN_PROGRESS by the refusal


def test_the_record_launches_once_the_dependency_completes_within_the_same_attempt(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")
    _prepared_continuation(layout, "T", "T.a2")
    sup = Supervisor(layout, sleep_s=0)

    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        first = sup.launch(jobs.read(layout, "T.a2"))
        assert first["phase"] == "PREPARED"

        _complete(layout, "DEP")
        assert tasks.load(layout, "DEP").status == TaskStatus.COMPLETE

        second = sup.launch(jobs.read(layout, "T.a2"))

    assert second["phase"] == "SUBMITTED"
    recs = jobs.for_task(layout, "T")
    assert [r["attempt_id"] for r in recs] == ["T.a2"]               # no second revision spent


def test_dispatch_revision_notes_the_unmet_dependency_at_queue_time(layout):
    make_task(layout, "DEP")
    make_task(layout, "T", deps=["DEP"])
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)

    escalation.dispatch_revision(sup, tasks.load(layout, "T"), "verification", "missing artifacts: out.txt", False)

    assert sup.calls and sup.calls[0]["task"].id == "T"
    notes = [n["note"] for n in (tasks.load(layout, "T").model_extra or {}).get("notes") or []]
    assert any("revision queued; waiting on DEP" in n for n in notes)
