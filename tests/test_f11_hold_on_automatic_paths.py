"""Round-5 review F11 (docs/reviews/RELEASE_REVIEW_2.1.0_round5.md lines 92-120, answer 3 at lines 188-199):
F9's durable `held` field (`models/task.py:160`) was read by only two of the automatic paths that move a task -
`diagnosis.ingest`'s requeue branch and `loop._repair_capped_escalation` - so the loop's own continuation launch
still reversed a Hold. `Supervisor.requeue` (`loop.py:151-208`) queued a continuation for any DEFERRED delivery
without reading `held` at all; `launch()` ran `_ensure_in_progress` (`loop.py:239-242`) BEFORE `gate.check` ever
saw the task, moving a BLOCKED, held task to IN_PROGRESS with `actor="loop"`; and `gate.check`
(`gate.py:121-163`) had no task-status condition to refuse the launch that followed.

Probe `held_then_launch` (round 5, `runs/T2-70/T2-70.a1/r5_hold.py` section A, `r5_hold.txt:5`): the owner Holds
an IN_PROGRESS task through the real `/hold` route while its delivery is RUNNING; the delivery then reports a
capacity DEFERRED through production `requeue()` - one continuation is PREPARED on the held task; the window
resets and production `launch()` runs it: before this fix the gate refused nothing (`launch_held_reason: null`,
phase `SUBMITTED`) and the task read IN_PROGRESS by `loop` with `held` still true. Because `loop`'s own write
overwrote the owner's Hold history entry, the escalation cap that followed wrote its own BLOCKED entry
(`actor="escalation"`), so `sweep_stalled` - whose owner-stop skip reads only the latest history entry - no
longer saw an owner's stop there and wrongly escalated the task to the owner after the grace period.

Fix: `_ensure_in_progress` never moves a held task (whatever its status), and `gate.check` refuses a `delivery`
launch while `held` is set - leaving the continuation PREPARED, retried every `reconcile()` like any other
refused record, same as before any Hold. `requeue()` still queues the continuation either way (crash-safe, F3
unchanged) but now reads `held` to say, in the note the owner reads, that the record will not launch until they
release it - so the task is never silently stranded. Because the task then never leaves BLOCKED, the owner's own
Hold history entry is never overwritten, and `sweep_stalled` correctly leaves it alone."""
import time
from unittest.mock import patch

from supervisor import diagnosis, escalation, gate, jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_exception_route_and_checkpoints import diag_records, fake_claude  # noqa: F401
from tests.test_f9_owner_hold_identity import _answer, _hold, _history


def _notes(layout, tid):
    return [n["note"] for n in (tasks.load(layout, tid).model_extra or {}).get("notes") or []]


def _prepared_continuation(layout, tid, attempt="T.a2"):
    """One PREPARED delivery continuation, the same shape `requeue()`'s own dispatch leaves behind."""
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "process", "phase": "PREPARED",
                               "spec": {"kind": "delivery"}, "deferred_until": 0})


# ------------------------------------------------------------------ gate.check reads `held` directly


def test_gate_refuses_a_delivery_launch_while_the_task_is_held(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    task = tasks.load(layout, "T")

    reason = gate.check(layout, task, "process", "delivery", [])

    assert reason is not None and "held" in reason.lower()


def test_gate_does_not_refuse_a_diagnosis_launch_while_the_task_is_held(layout):
    """A manager diagnosis still runs against a held task - it is how the owner hears a proposed requeue was
    declined (diagnosis.py's own `held` read, F9). Only the delivery/continuation lane is refused here."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    task = tasks.load(layout, "T")

    assert gate.check(layout, task, "claude_cli", "diagnosis", []) is None


def test_gate_allows_a_delivery_launch_for_an_unheld_task(layout):
    """The unheld control: nothing about this fix changes the ordinary gate decision."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    task = tasks.load(layout, "T")

    assert gate.check(layout, task, "process", "delivery", []) is None


# ------------------------------------------------------------------ the loop's own continuation path (launch)


def test_ensure_in_progress_and_the_gate_both_refuse_a_held_tasks_continuation(layout):
    """`launch()` calls `_ensure_in_progress` before `gate.check` ever runs (`loop.py:259-260`) - before this fix
    that alone moved a held, BLOCKED task to IN_PROGRESS, and the gate refused nothing afterward. Both must now
    leave the task BLOCKED, with the record parked PREPARED behind a held reason."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    rec = _prepared_continuation(layout, "T")

    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        launched = Supervisor(layout, sleep_s=0).launch(rec)

    assert launched["phase"] == "PREPARED"                    # never SUBMITTED
    assert launched.get("held")                                # a held reason, not null
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED                    # _ensure_in_progress never moved it
    assert task.held is not None
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"   # the Hold entry survives


def test_an_unheld_prepared_continuation_still_launches(layout):
    """The unheld control: nothing about this fix changes the ordinary launch path."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    rec = _prepared_continuation(layout, "T")

    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        launched = Supervisor(layout, sleep_s=0).launch(rec)

    assert launched["phase"] == "SUBMITTED"
    assert not launched.get("held")
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS


def test_requeue_still_queues_a_held_tasks_continuation_but_says_so_in_a_note(layout):
    """`requeue()` stays crash-safe either way (F3): it still queues the continuation record (so a later release
    needs no extra repair), but a held task must not be silently stranded - the note the owner reads must say the
    record is held and will not launch until they release it."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = jobs.write(layout, {"attempt_id": "T.a1", "task_id": "T", "adapter": "process", "phase": "RUNNING",
                              "spec": {"kind": "delivery"}, "handle": {}})
    _hold(layout)

    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")

    cont = [r for r in jobs.for_task(layout, "T") if r["phase"] == "PREPARED"]
    assert len(cont) == 1                                     # still queued, crash-safe (F3 unchanged)
    notes = _notes(layout, "T")
    assert notes and "held" in notes[-1].lower() and "release" in notes[-1].lower()


# ------------------------------------------------------------------ the full interleaving, end to end


def test_held_then_launch_leaves_the_task_blocked_and_held_with_no_continuation_started(layout, fake_claude):
    """Production port of round 5's `held_then_launch` probe: the owner Holds an IN_PROGRESS task through the
    real `/hold` route while its delivery is RUNNING; the delivery reports a capacity DEFERRED through production
    `requeue()`; the continuation's deferral window resets and production `launch()` runs it; the chain then
    exhausts its continuation budget and the manager answers requeue; `sweep_stalled` runs after the grace period.
    At every step the task must stay BLOCKED and held, with no continuation ever reaching SUBMITTED/RUNNING, no
    path moving it to IN_PROGRESS, and no owner item from the stall sweep."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    running = jobs.write(layout, {"attempt_id": "T.a1", "task_id": "T", "adapter": "process", "phase": "RUNNING",
                                  "spec": {"kind": "delivery"}, "handle": {}})
    _hold(layout)
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED

    sup.requeue(running, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    cont = [r for r in jobs.for_task(layout, "T") if r["phase"] == "PREPARED"]
    assert len(cont) == 1
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED and task.held is not None

    for r in cont:
        jobs.set_phase(layout, r["attempt_id"], "PREPARED", deferred_until=0)
    with patch.object(REGISTRY["process"], "recover", return_value=None), \
         patch.object(REGISTRY["process"], "submit", return_value={"kind": "fake", "pid": 0}):
        launched = sup.launch(jobs.read(layout, cont[0]["attempt_id"]))
    assert launched["phase"] == "PREPARED"                    # never SUBMITTED
    assert launched.get("held")                                # launch_held_reason is no longer null
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED                   # never moved to IN_PROGRESS by loop
    assert task.held is not None
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"

    # the stale deferral meets the continuation cap: force the parked record through as the probe does, to prove
    # the manager's own already-correct Hold read (F9) and the stall sweep both still behave once it does
    rec = jobs.read(layout, cont[0]["attempt_id"])
    jobs.set_phase(layout, rec["attempt_id"], "RUNNING")
    for _ in range(sup.MAX_CONTINUATIONS):
        rec = jobs.for_task(layout, "T")[-1]
        sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED and task.held is not None
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"   # the Hold entry, still

    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1                            # the cap's own dispatch-regardless control (F9)
    out = diagnosis.ingest(sup, manager_diags[0], _answer("requeue"))
    assert out["action"] == "held"                            # declined, not granted (F9, unchanged)
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED and task.held is not None

    swept = sup.sweep_stalled(now=time.time() + 3600)
    assert swept == []                                        # the owner's Hold is still the latest stop: no escalation
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.BLOCKED and task.held is not None
    assert task.history[-1]["to"] == "BLOCKED" and task.history[-1]["by"] == "owner-ui"


def test_an_unheld_recovery_window_is_unaffected(layout, fake_claude):
    """The unheld control for the same shape of chain: an ordinary (never held) capacity-exhaustion chain still
    caps, dispatches a manager and reaches READY exactly as F6/F9 left it."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    from tests.test_continuation_diagnosis_route import continuation_chain
    rec = continuation_chain(layout, "T")

    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")

    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1
    out = diagnosis.ingest(sup, manager_diags[0], _answer("requeue"))
    assert out["action"] == "requeue"
    assert tasks.load(layout, "T").status == TaskStatus.READY
