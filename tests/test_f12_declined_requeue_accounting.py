"""Round-5 review F12 (docs/reviews/RELEASE_REVIEW_2.1.0_round5.md lines 122-151, answers 3 and 4 at lines
188-213): `diagnosis.ingest` wrote `diag_action="requeue"` on the job record before the `held` check (F9) ever
ran, so a decline was recorded, counted and deduplicated exactly like a granted requeue - `count_requeues` spent
one of the two `DIAGNOSIS_BUDGET` slots on a decline, and `already()` matched the declined record by (signature,
level, grant) and returned it for the next identical escalation, so the owner's own release through `/requeue`
was followed by nothing on the primary route: no new diagnosis, no issue, no owner item, until the stall sweep's
grace period.

A declined requeue now writes its own action (`held`, the same word `ingest()` already returns) on the job
record, is excluded from `count_requeues`, and is excluded from `already()`'s dedup, so a fresh escalation after
the owner's release reaches its own manager diagnosis again instead of reusing the spent decline."""
import time

import pytest

from supervisor import diagnosis, jobs, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_continuation_diagnosis_route import continuation_chain
from tests.test_exception_route_and_checkpoints import diag_records, fake_claude  # noqa: F401


def _answer(action="requeue", **extra):
    return Result(status="COMPLETE", raw=dict(kind="diagnosis", action=action, summary="probe", guidance="retry", **extra))


def test_a_declined_requeue_writes_its_own_action_and_is_not_counted(layout, fake_claude):
    """Before this fix: the record's own `diag_action` stayed "requeue" even though the decision was declined,
    and that declined record counted against `DIAGNOSIS_BUDGET`."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    diag = next(r for r in diag_records(layout, "T") if r.get("diag_level") == "manager")

    out = diagnosis.ingest(sup, diag, _answer("requeue"))

    assert out["action"] == "held"
    rec_after = jobs.read(layout, diag["attempt_id"])
    assert rec_after.get("diag_action") == "held"            # its own action, not the manager's declined "requeue"
    assert diagnosis.count_requeues(layout, "T") == 0         # a decline spends no budget slot


def test_already_does_not_reuse_a_declined_record_for_the_next_identical_escalation(layout, fake_claude):
    """`already()` matches a diagnosis job by (signature, level, grant) alone; a declined record must not satisfy
    that match, or the next identical escalation reuses the spent decline instead of asking its own question."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    diag = next(r for r in diag_records(layout, "T") if r.get("diag_level") == "manager")
    diagnosis.ingest(sup, diag, _answer("requeue"))

    sig = diag["diag_signature"]
    assert diagnosis.already(layout, "T", sig, "manager", grant=0) is None


def test_release_then_recap_reaches_a_fresh_diagnosis_on_the_primary_route(layout, fake_claude):
    """The probe's release-then-recap sequence (r5_hold.txt:6, `declined_requeue_accounting`/`held`): after a
    Hold, a declined requeue and the owner's own release, the task's next identical failure must reach a fresh
    manager diagnosis on the primary route (escalation._apply -> diagnosis.dispatch), not nothing until
    sweep_stalled's grace period."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    diag = next(r for r in diag_records(layout, "T") if r.get("diag_level") == "manager")
    diagnosis.ingest(sup, diag, _answer("requeue"))
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED

    tasks.set_status(layout, "T", "READY", actor="owner-ui", note="re-queued by owner")   # the owner's own release
    assert tasks.load(layout, "T").held is None

    tasks.set_status(layout, "T", "IN_PROGRESS")
    rec2 = continuation_chain(layout, "T", start=20)   # past the first chain's attempt numbers
    before = diag_records(layout, "T")

    sup.requeue(rec2, Result(status="DEFERRED", reset_epoch=time.time()), "capacity limit")

    after = diag_records(layout, "T")
    assert len(after) == len(before) + 1               # a fresh manager diagnosis, not the reused decline
    fresh = [r for r in after if r["attempt_id"] not in {r0["attempt_id"] for r0 in before}]
    assert len(fresh) == 1 and fresh[0]["diag_level"] == "manager"
    assert fresh[0]["attempt_id"] != diag["attempt_id"]
