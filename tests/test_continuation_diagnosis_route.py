"""2.1.0 change 1 (D-010 Consequences): continuations exhausted now routes through the manager's diagnosis
(one bounded requeue/defer/architect/owner decision) instead of straight to a BLOCKED "owner decision needed"
note that only the owner's inbox ever saw. The continuation cap itself still holds, and the diagnosis route's own
requeue budget (DIAGNOSIS_BUDGET) still bounds how many times a diagnosis may grant a fresh continuation chain."""
import os
import time

from supervisor import diagnosis, escalation, inbox, jobs, tasks
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


def grants(layout, tid):
    """The grants the escalation route made for `tid`, read from the task's history rather than its live status.
    once() ingests a finished diagnosis in reconcile() and plans the same iteration, so the loop can launch the task
    the grant just queued before a run_until() predicate is ever checked (the race review 1.10.1 F4 names): the
    status is then IN_PROGRESS again, by `loop`, and only the recorded transition still shows the grant."""
    return [h for h in tasks.load(layout, tid).history if h["to"] == "READY" and h["by"] == "escalation"]


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
    assert len(grants(layout, "T")) == 1                        # the diagnosis granted a fresh continuation budget


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


# ------------------------------------------------------------------ R4 regressions: explicit, replay-safe grants


def test_a_manager_requeue_grants_a_fresh_continuation_chain_once_and_a_replay_grants_none(layout, fake_claude):
    """The owner's REQUEUE always retires the old continuation markers (supervisor.inbox); a diagnosis-granted
    requeue must do the same (R4), and only once per diagnosis job — a replayed ingest (a crash between the
    decision and the job record's own DONE write) must not spend a second grant."""
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    run_until(layout, lambda: any(r.get("diag_action") for r in diag_records(layout, "T")))
    diag = diag_records(layout, "T")[0]
    assert len(grants(layout, "T")) == 1
    prior = [r for r in jobs.for_task(layout, "T") if str(r.get("deferred_reason", "")).startswith("continuation")]
    assert prior == []                                   # the grant retired every marker the cap had counted
    state = escalation._load(layout, "T")
    assert state.get("resets") == 1
    # replay: the same diagnosis result ingested again must not grant a second reset
    result = Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue", "summary": "again", "guidance": "retry"})
    diagnosis.ingest(sup, diag, result)
    assert escalation._load(layout, "T").get("resets") == 1


def test_the_next_continuation_after_a_grant_runs_and_the_fresh_chain_still_hits_its_own_cap(layout, fake_claude):
    """Scoped to the new grant: MAX_CONTINUATIONS fresh continuations after the grant all go through (the old cap
    is not immediately hit again), and exhausting the FRESH chain opens a new issue and dispatches a second,
    independent manager diagnosis instead of silently reusing the grant-1 issue/diagnosis forever."""
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    run_until(layout, lambda: any(r.get("diag_action") for r in diag_records(layout, "T")))
    assert len(grants(layout, "T")) == 1
    tasks.set_status(layout, "T", "IN_PROGRESS")
    issues_after_grant_1 = len(list(layout.issues.glob("I-*.yaml")))
    n = len(jobs.for_task(layout, "T")) + 1
    rec2 = jobs.write(layout, {"attempt_id": f"T.a{n}", "task_id": "T", "adapter": "python", "phase": "RUNNING",
                              "spec": {"kind": "delivery"}, "handle": {}})
    for _ in range(Supervisor.MAX_CONTINUATIONS):
        sup.requeue(rec2, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
        assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS    # the fresh grant's budget is still live
        rec2 = jobs.for_task(layout, "T")[-1]                               # the continuation requeue() just queued
    # one more exhausts the fresh chain and must escalate again, scoped to THIS grant
    sup.requeue(rec2, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    capped = jobs.read(layout, rec2["attempt_id"])
    assert capped.get("continued") is True and capped.get("capped") is True
    assert len(list(layout.issues.glob("I-*.yaml"))) > issues_after_grant_1   # a NEW issue, not the spent grant's
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 2                        # a second, independent diagnosis — not deduped away


def test_a_torn_continuation_write_below_the_cap_is_still_repaired(layout):
    """repair_continuations()'s original purpose (a crash between the old attempt's DONE+DEFERRED write and the
    fresh continuation it should have queued) must still work once the cap/grant handling changes."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    jobs.write(layout, {"attempt_id": "T.a1", "task_id": "T", "adapter": "python", "phase": "DONE",
                        "spec": {"kind": "delivery"}, "handle": {}, "result": "DEFERRED"})
    before = len(jobs.for_task(layout, "T"))
    sup.repair_continuations()
    recs = jobs.for_task(layout, "T")
    assert len(recs) == before + 1                        # the missed continuation is queued now
    assert recs[-1]["spec"]["kind"] == "delivery"
    assert jobs.read(layout, "T.a1").get("continued") is True


def test_repair_continuations_enforces_the_same_cap_as_requeue(layout, fake_claude):
    """R4: repair_continuations() must never dispatch a fresh delivery for a task that already hit
    MAX_CONTINUATIONS — not even when the capped attempt's own DONE write was interrupted before `continued` was
    set (a torn write AT the cap boundary, not below it)."""
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")                 # 3 prior continuations already recorded
    # the cap decision's DONE+DEFERRED write landed, but a crash means `continued`/`capped` never did
    jobs.set_phase(layout, rec["attempt_id"], "DONE", result="DEFERRED")

    def deliveries():
        return [r["attempt_id"] for r in jobs.for_task(layout, "T") if (r.get("spec") or {}).get("kind", "delivery") == "delivery"]
    before = deliveries()
    sup.repair_continuations()
    assert deliveries() == before                          # no bypassing delivery queued (a manager diagnosis may be)
    repaired = jobs.read(layout, rec["attempt_id"])
    assert repaired.get("continued") is True and repaired.get("capped") is True
    assert (layout.issues / "I-001.yaml").exists()         # routed through escalation instead of repair's own dispatch


def test_repeated_manager_requeues_spend_the_diagnosis_budget_instead_of_looping(layout, fake_claude):
    """The probe's five-round sequence (runs/T2-05/T2-05.a1/probes.py, `continuation_cap_bypass`): a manager
    requeue used to leave the old continuation markers in place and the stale issue/diagnosis deduped every
    later exhaustion away, so repair_continuations() could dispatch new deliveries without ever consulting
    DIAGNOSIS_BUDGET. Scoped grants mean each exhaustion after a fresh grant reaches its OWN diagnosis, so the
    sequence still ends at DIAGNOSIS_BUDGET requeues, not after an unbounded number of rounds."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")
    attempt = 50
    owner_items = []
    for round_n in range(1, diagnosis.DIAGNOSIS_BUDGET + 2):          # one more round than the budget allows
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
        owner_items = inbox.open_items(layout, "owner")
        if owner_items:
            assert round_n == diagnosis.DIAGNOSIS_BUDGET + 1          # stopped exactly at the budget, not later
            break
        diag = next((r for r in diag_records(layout, "T")
                    if r.get("diag_level") == "manager" and not r.get("diag_action")), None)
        assert diag is not None, f"round {round_n}: no fresh diagnosis after the grant (the stale one was reused)"
        out = diagnosis.ingest(sup, diag, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue",
                                                                          "summary": "again", "guidance": "retry"}))
        assert out["action"] == "requeue"
        tasks.set_status(layout, "T", "IN_PROGRESS")
        attempt += 10
        rec = jobs.write(layout, {"attempt_id": f"T.a{attempt}", "task_id": "T", "adapter": "python", "phase": "RUNNING",
                                  "spec": {"kind": "delivery"}, "handle": {}})
        for _ in range(Supervisor.MAX_CONTINUATIONS):
            sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
            rec = jobs.for_task(layout, "T")[-1]
    else:
        raise AssertionError("the sequence ran past DIAGNOSIS_BUDGET without ever reaching the owner")
    assert len(owner_items) == 1
    assert diagnosis.count_requeues(layout, "T") == diagnosis.DIAGNOSIS_BUDGET
