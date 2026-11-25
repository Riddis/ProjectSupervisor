"""The per-turn safety net must not flip a task to FAILED while
the revision or retry that will replace its failing digest is still open. `reconcile_advance` walks
every ADVANCEABLE task, including IN_PROGRESS, and `_evaluate()` reads FAILED back from the current digest's own
verification record whenever it is not ok -- true by construction while an open attempt is still replacing that
digest. Before `reconcile_advance` existed nothing re-evaluated an IN_PROGRESS task between events."""
from __future__ import annotations

import sys
import time

from supervisor import jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def test_reconcile_advance_leaves_an_in_flight_revision_in_progress__pure_state(layout):
    """verification fails -> FAILED -> a revision is dispatched and launched (IN_PROGRESS, an open job record) ->
    reconcile_advance, told about that open job, must leave the task exactly where the launch put it, with no
    second 'verification failed' history entry."""
    make_task(layout, "A", status="IN_PROGRESS", tests=[])
    tasks.record_delivery(layout, "A", "a" * 64, paths=[])
    tasks.record_verification(layout, "A", "a" * 64, False, detail="missing artifact x")
    tasks.advance(layout, "A")
    assert tasks.load(layout, "A").status == TaskStatus.FAILED

    jobs.write(layout, {"attempt_id": "A.a2", "task_id": "A", "adapter": "claude_cli", "phase": "RUNNING",
                        "spec": {"kind": "delivery"}, "handle": {"pid": 0}, "deferred_until": 0})
    tasks.set_status(layout, "A", "IN_PROGRESS", actor="loop", note="launch A.a2 via claude_cli")

    advanced = tasks.reconcile_advance(layout)

    t = tasks.load(layout, "A")
    assert advanced == []
    assert t.status == TaskStatus.IN_PROGRESS
    failed_entries = [h for h in t.history if "verification failed" in (h.get("note") or "")]
    assert len(failed_entries) == 1, "a running revision must not be re-evaluated against the digest it replaces"


def test_a_queued_retry_running_keeps_the_task_in_progress_through_once(layout):
    """The real sequence, through the process adapter and a real loop turn: a deterministic job exits 3 (declared
    retryable) -> FAILED -> the stall sweep queues the one retry -> once() launches it (it now sleeps, still
    running) -- and in that same turn the task must still read IN_PROGRESS, with its job record RUNNING."""
    marker = layout.root / "marker.txt"
    code = f"import os, sys, time\nsys.exit(3) if not os.path.exists({str(marker)!r}) else time.sleep(20)\n"
    make_task(layout, "A", tests=[])
    tasks._update(layout, "A", lambda d: d.update(job={"adapter": "process", "argv": [sys.executable, "-c", code],
                                                        "retry_on": [3]}, artifacts=["A.txt"]))
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + 20
    while time.time() < deadline and tasks.load(layout, "A").status != TaskStatus.FAILED:
        sup.once()
        time.sleep(0.1)
    assert tasks.load(layout, "A").status == TaskStatus.FAILED

    marker.write_text("x")
    assert sup.sweep_stalled(now=time.time() + 3600) == ["A"]
    assert tasks.load(layout, "A").status == TaskStatus.IN_PROGRESS

    try:
        sup.once()  # launches A.a2 (now sleeping) and, in the same turn, runs reconcile_advance

        t = tasks.load(layout, "A")
        records = {r["attempt_id"]: r["phase"] for r in jobs.for_task(layout, "A")}
        assert records.get("A.a2.s1") in ("SUBMITTED", "RUNNING")
        assert t.status == TaskStatus.IN_PROGRESS
        failed_entries = [h for h in t.history if "verification failed" in (h.get("note") or "")]
        assert len(failed_entries) == 1, "the running retry must not be re-evaluated against the digest it replaces"
    finally:
        for r in jobs.for_task(layout, "A"):
            if r["phase"] in ("SUBMITTED", "RUNNING") and r.get("handle"):
                REGISTRY["process"].terminate(r["handle"])
