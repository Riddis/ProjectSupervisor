"""Round-4 review F9 (docs/reviews/RELEASE_REVIEW_2.1.0_round4.md lines 81-140, answer 3 at lines 178-207):
nothing durable distinguished an owner Hold from the cap event's own BLOCKED, so the cap's automatic requeue
undid it - in each of the three windows where recovery (`loop._repair_capped_escalation`) replayed an
interrupted escalation over a Hold, and in the uninterrupted path too, where `diagnosis.ingest`'s own ordinary
manager requeue moved a held task straight to READY. A Hold on a task the cap had already moved to BLOCKED also
left no history entry and no change to the task record at all (`tasks.set_status`'s same-status write is a
silent `_NoChange`) - only a `command` line in `state/log.jsonl`.

`tasks.hold()` now records a durable identity on the task itself (`held`: by/at/note), even when the task is
already BLOCKED; `diagnosis.ingest`'s requeue branch and `loop._repair_capped_escalation` both read it directly
and decline to move a held task, rather than inferring a Hold from the task's status, its history order or the
escalation event's own `routed` flag (none of which can tell an owner's BLOCKED apart from the cap event's own
BLOCKED - the gap this finding names)."""
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from supervisor import diagnosis, escalation, jobs, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task
from tests.test_continuation_diagnosis_route import continuation_chain
from tests.test_exception_route_and_checkpoints import diag_records, fake_claude  # noqa: F401


def _answer(action="requeue", **extra):
    return Result(status="COMPLETE", raw=dict(kind="diagnosis", action=action, summary="probe", guidance="retry", **extra))


def _hold(layout, note="held by owner"):
    """The real `/tasks/T/hold` route, through FastAPI's in-process TestClient - production code, not a stub."""
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set(CSRF_COOKIE, "probe")
    r = client.post("/tasks/T/hold", data={"csrf_token": "probe", "note": note})
    assert r.status_code == 200, r.text
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    return r


def _history(layout):
    return [(h.get("to"), h.get("by"), h.get("note")) for h in tasks.load(layout, "T").history or []]


# ------------------------------------------------------------------ durable identity, even with no status change


def test_hold_on_an_already_blocked_task_leaves_a_durable_record(layout):
    """Before this fix: tasks.set_status(..., BLOCKED, ...) on a task already BLOCKED raises `_NoChange` and
    persists nothing - no history entry, no change to the task's own bytes at all. tasks.hold() must leave one
    regardless, with the durable `held` identity the automatic path reads."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "BLOCKED", actor="escalation", note="automatic")
    before = (layout.tasks / "T.yaml").read_bytes()
    history_before = _history(layout)

    tasks.hold(layout, "T", "owner-ui", "held by owner")

    after = (layout.tasks / "T.yaml").read_bytes()
    task = tasks.load(layout, "T")
    assert after != before                                  # a durable write happened, not a silent _NoChange
    assert task.status == TaskStatus.BLOCKED
    assert task.held and task.held["by"] == "owner-ui" and task.held["note"] == "held by owner"
    assert len(task.history) == len(history_before) + 1      # a new entry, not a repeat of the automatic one
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"


def test_hold_through_the_real_route_on_an_already_blocked_task_also_leaves_a_record(layout):
    """Same gap, through the real `/hold` route rather than calling tasks.hold() directly."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "BLOCKED", actor="escalation", note="automatic")
    before = (layout.tasks / "T.yaml").read_bytes()

    _hold(layout)

    after = (layout.tasks / "T.yaml").read_bytes()
    task = tasks.load(layout, "T")
    assert after != before
    assert task.held and task.held["by"] == "owner-ui"
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"


# ------------------------------------------------------------------ the uninterrupted path (hold_nocrash)


def test_a_hold_set_after_the_cap_routes_is_not_undone_by_the_managers_requeue(layout, fake_claude):
    """The cap lands fully (issue, BLOCKED, manager dispatched, routed); the owner Holds the already-BLOCKED
    task; the manager then answers requeue. Before this fix the task reached READY with the Hold persisting
    nothing at all."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    event = f"{rec['attempt_id']}:continuations"
    assert escalation.event_outcome(layout, "T", event) == "routed"
    diag = next(r for r in diag_records(layout, "T") if r.get("diag_level") == "manager")

    bytes_before_hold = (layout.tasks / "T.yaml").read_bytes()
    _hold(layout)
    assert (layout.tasks / "T.yaml").read_bytes() != bytes_before_hold

    out = diagnosis.ingest(sup, diag, _answer("requeue"))

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED                 # not READY: the Hold held
    assert task.held is not None
    assert out["action"] == "held"
    history = _history(layout)
    assert history[-1][:2] == ("BLOCKED", "owner-ui")        # the Hold entry, not a later READY one


def test_a_hold_set_before_the_cap_lands_is_not_undone_by_the_managers_requeue(layout, fake_claude):
    """The owner Holds the IN_PROGRESS task first; the cap then lands normally (a fresh issue, a manager
    dispatched on the already-held task - unaffected by this fix, per the review's own control); the manager
    answers requeue. Before this fix the task still reached READY, with the Hold's own history entry
    overwritten by the escalation's later one."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    _hold(layout)
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    event = f"{rec['attempt_id']}:continuations"
    assert escalation.event_outcome(layout, "T", event) == "routed"
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1                            # the dispatch-regardless control still holds

    out = diagnosis.ingest(sup, manager_diags[0], _answer("requeue"))

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED
    assert task.held is not None
    assert out["action"] == "held"
    history = _history(layout)
    assert ("BLOCKED", "owner-ui") in [(h[0], h[1]) for h in history]
    assert history[-1][0] != "READY"


# ------------------------------------------------------------------ the three pending recovery windows


def test_a_hold_after_the_decision_is_durable_blocks_the_recovery_replay(layout, fake_claude):
    """Interrupted after record_failure's own decision write, before `_apply` ever opens the issue. The owner
    Holds the still-IN_PROGRESS task (so this also exercises the ordinary IN_PROGRESS->BLOCKED hold path, not
    only the already-BLOCKED one). Before this fix, a fresh reconcile() replayed the escalation over the Hold:
    one new manager diagnosis, then READY."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    event = f"{rec['attempt_id']}:continuations"
    real_save = escalation._save

    def save_then_stop(*args, **kwargs):
        real_save(*args, **kwargs)
        raise RuntimeError("stop after decision durable")

    with patch.object(escalation, "_save", side_effect=save_then_stop):
        with pytest.raises(RuntimeError):
            sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    assert escalation.event_outcome(layout, "T", event) == "pending"
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS

    _hold(layout)
    before_diags = len(diag_records(layout, "T"))

    fresh = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        fresh.reconcile()

    assert len(diag_records(layout, "T")) == before_diags    # no replay dispatched over the Hold
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED
    assert task.held is not None


def test_a_hold_before_the_blocked_write_blocks_the_recovery_replay(layout, fake_claude):
    """Interrupted after the issue is opened, before the IN_PROGRESS->BLOCKED write. Before this fix, a fresh
    reconcile() replayed the escalation over the owner's Hold: one new manager diagnosis, then READY."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    event = f"{rec['attempt_id']}:continuations"
    real_status = tasks.set_status

    def status_stop(*args, **kwargs):
        target = args[2] if len(args) > 2 else kwargs.get("status")
        if str(target) in ("BLOCKED", "TaskStatus.BLOCKED"):
            raise RuntimeError("stop before BLOCKED")
        return real_status(*args, **kwargs)

    with patch.object(tasks, "set_status", side_effect=status_stop):
        with pytest.raises(RuntimeError):
            sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    assert escalation.event_outcome(layout, "T", event) == "pending"
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS

    _hold(layout)
    before_diags = len(diag_records(layout, "T"))

    fresh = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        fresh.reconcile()

    assert len(diag_records(layout, "T")) == before_diags
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED
    assert task.held is not None


def test_a_hold_after_the_blocked_write_blocks_the_recovery_replay(layout, fake_claude):
    """Interrupted after the automatic BLOCKED write, before the manager dispatch. The owner Holds a task the
    cap event itself already moved to BLOCKED - the exact ambiguity F9 names: before this fix, the Hold left no
    durable trace at all (a silent _NoChange) and a fresh reconcile() still replayed the escalation: one new
    manager diagnosis, then READY."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    event = f"{rec['attempt_id']}:continuations"

    with patch.object(diagnosis, "dispatch", side_effect=RuntimeError("stop before dispatch")):
        with pytest.raises(RuntimeError):
            sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    assert escalation.event_outcome(layout, "T", event) == "pending"
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED   # the cap's own automatic write, not the owner's

    bytes_before_hold = (layout.tasks / "T.yaml").read_bytes()
    _hold(layout)
    assert (layout.tasks / "T.yaml").read_bytes() != bytes_before_hold   # a durable write, not _NoChange
    before_diags = len(diag_records(layout, "T"))

    fresh = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        fresh.reconcile()

    assert len(diag_records(layout, "T")) == before_diags
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED
    assert task.held is not None


# ------------------------------------------------------------------ release and the unheld controls


def test_the_owner_s_own_requeue_releases_a_hold(layout):
    """The owner's own explicit requeue (not an automatic one) still moves a held task and clears `held`, so it
    is not left stranded."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    assert tasks.load(layout, "T").held is not None

    tasks.set_status(layout, "T", "READY", actor="owner-ui", note="re-queued by owner")

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.READY
    assert task.held is None


def test_an_unheld_blocked_task_still_reaches_ready_through_the_managers_requeue(layout, fake_claude):
    """The unheld control: nothing about this fix changes the ordinary, no-Hold requeue path."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    diag = next(r for r in diag_records(layout, "T") if r.get("diag_level") == "manager")

    out = diagnosis.ingest(sup, diag, _answer("requeue"))

    assert out["action"] == "requeue"
    assert tasks.load(layout, "T").status == TaskStatus.READY


def test_an_unheld_recovery_window_still_reaches_ready(layout, fake_claude):
    """The unheld control for recovery: interrupted after BLOCKED, before dispatch, with no Hold at all - the
    existing F3/F6 repair still finishes it."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    with patch.object(diagnosis, "dispatch", side_effect=RuntimeError("stop before dispatch")):
        with pytest.raises(RuntimeError):
            sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED

    fresh = Supervisor(layout, sleep_s=0)
    fresh.reconcile()
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1

    out = diagnosis.ingest(fresh, manager_diags[0], _answer("requeue"))
    assert out["action"] == "requeue"
    assert tasks.load(layout, "T").status == TaskStatus.READY


def test_cancellation_is_still_ignored_by_ingest(layout, fake_claude):
    """Cancellation's existing protection (diagnosis.ingest's own early return) is unaffected by this fix."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    diag = next(r for r in diag_records(layout, "T") if r.get("diag_level") == "manager")
    tasks.set_status(layout, "T", "CANCELLED", actor="owner-ui", note="cancelled by owner", force=False)

    out = diagnosis.ingest(sup, diag, _answer("requeue"))

    assert out == {"action": "ignored"}
    assert tasks.load(layout, "T").status == TaskStatus.CANCELLED
