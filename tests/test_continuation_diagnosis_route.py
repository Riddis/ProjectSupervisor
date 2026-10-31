"""2.1.0 change 1 (D-010 Consequences): continuations exhausted now routes through the manager's diagnosis
(one bounded requeue/defer/architect/owner decision) instead of straight to a BLOCKED "owner decision needed"
note that only the owner's inbox ever saw. The continuation cap itself still holds, and the diagnosis route's own
requeue budget (DIAGNOSIS_BUDGET) still bounds how many times a diagnosis may grant a fresh continuation chain."""
import os
import time
from unittest.mock import patch

import pytest

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


def reconcile_until(sup, predicate, timeout=25):
    """Poll `sup.reconcile()` (never `once()`) until `predicate()` is true. The manager diagnosis dispatched by
    `escalation.record_failure()` runs as a real subprocess (`FakeDiagnosisClaude`), so a fixed count of calls
    races it; `reconcile()` alone settles it (and any grant it makes) without ever reaching `once()`'s scheduler
    step, which would launch the freshly-granted continuation's own delivery attempt through the same fake
    adapter and fail it — the race `grants()` below is already written to read around, not to re-trigger."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.reconcile()
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition never became true")


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


# ------------------------------------------------------------------ F4/F7 regression: the architect transition
# carries its grant generation, and a replayed hand-off reuses it rather than reading it as unavailable


def test_a_fresh_manager_architect_handoff_reaches_its_own_architect_each_generation(layout):
    """Round-2 review F4: `diagnosis.ingest`'s `architect` branch dispatched the next level without the
    originating diagnosis's own grant, so `dispatch`'s default (`grant=0`) let a generation-1 manager's architect
    request dedupe against the generation-0 architect record — `already()` only excludes by grant generation, and
    the omitted `grant` made every architect request look like generation 0. `dispatch` then returned None and the
    owner was told the architect lane was unavailable. One manager → architect → requeue round per generation, plus
    the ordinary budget-spent transition, must not reproduce that: each generation reaches its OWN architect
    diagnosis, with no owner item until DIAGNOSIS_BUDGET (counted across both levels) is actually spent.

    Round-3 review F7: an interrupted hand-off WITHIN one generation is a second way `dispatch` returning `None`
    reads as "lane unavailable" — `already()` matches the architect record `dispatch` itself just created, not
    only a stale one from an earlier grant. If the process stops after that architect job record is durable but
    before the manager's own `ingest()` call returns, a replay (the same manager rec/result ingested again, exactly
    the crash `ingest`'s own docstring already names for `requeue`) must reuse that matching architect transition —
    not create a second architect job, and not tell the owner the lane is unavailable when it is in fact already
    answering. Exercised right after generation 0's hand-off, before the architect itself has answered, so a
    wrongly-created owner item here could not be mistaken for the ordinary budget-spent one later in this test.

    Lane selection and final dispatch are stubbed to record a job synchronously, the same way the round-2
    reviewer's probe (runs/T2-32/T2-32.a1/probes.py, `architect_grant_lost`) isolated the routing bug from a real
    model session; `diagnosis.dispatch`/`already`/`ingest`, `escalation`'s grant accounting and `inbox.create` are
    all production code."""
    created = []

    def fake_dispatch(task, adapter_name, **kwargs):
        n = max([int(r["attempt_id"].rsplit(".a", 1)[1]) for r in jobs.for_task(layout, "T")] + [0]) + 1
        rec = jobs.write(layout, dict(attempt_id=f"T.a{n}", task_id="T", adapter=adapter_name, phase="RUNNING",
                                      spec={"kind": kwargs.get("kind", "delivery")}, handle={}, **kwargs.get("extra", {})))
        created.append(rec)
        return rec

    def answer(action, **extra):
        return Result(status="COMPLETE", raw=dict(kind="diagnosis", action=action, summary="probe", guidance="retry", **extra))

    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    with patch.object(diagnosis, "settings", return_value={"enabled": True, "roles": diagnosis.DEFAULT_ROLES}), \
         patch("supervisor.review.lane_for", return_value=("claude_cli", {}, False)), \
         patch.object(sup, "dispatch", side_effect=fake_dispatch):

        # Generation 0: the continuation cap escalates to a fresh manager diagnosis, which hands off to the
        # architect, who grants a fresh continuation chain.
        rec = continuation_chain(layout, "T")
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
        manager0 = created[-1]
        assert manager0["diag_level"] == "manager" and manager0["diag_grant"] == 0
        out = diagnosis.ingest(sup, manager0, answer("architect"))
        assert out["action"] == "architect"
        architect0 = created[-1]
        assert architect0["diag_level"] == "architect" and architect0["diag_grant"] == 0

        # F7: replay the SAME manager hand-off before architect0 itself has answered — the architect job it
        # already made durable must be reused, not read as an unavailable lane.
        replay_out = diagnosis.ingest(sup, manager0, answer("architect"))
        assert replay_out["action"] == "architect"            # was "owner" before the fix (F7's exact symptom)
        assert replay_out.get("attempt") == architect0["attempt_id"]     # the existing transition, not a fresh one
        assert [r for r in created if r.get("diag_level") == "architect"] == [architect0]   # no second architect
        assert inbox.open_items(layout, "owner") == []

        diagnosis.ingest(sup, architect0, answer("requeue"))
        assert diagnosis.count_requeues(layout, "T") == 1
        assert inbox.open_items(layout, "owner") == []
        tasks.set_status(layout, "T", "IN_PROGRESS")

        # Generation 1: exhausting the fresh chain reaches a fresh manager diagnosis (already covered by R4/F2);
        # its architect hand-off is where F4 broke — the manager's request must reach a FRESH architect diagnosis,
        # not the spent generation-0 record, and must not tell the owner the lane is unavailable before the new
        # architect even answers.
        rec = continuation_chain(layout, "T", start=20)
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
        manager1 = created[-1]
        assert manager1["diag_level"] == "manager" and manager1["diag_grant"] == 1
        out = diagnosis.ingest(sup, manager1, answer("architect"))
        assert out["action"] == "architect"                      # was "owner" before the fix (F4's exact symptom)
        assert inbox.open_items(layout, "owner") == []            # no premature owner item
        architect_recs = [r for r in created if r.get("diag_level") == "architect"]
        assert len(architect_recs) == 2                           # a fresh architect diagnosis per generation
        architect1 = architect_recs[-1]
        assert architect1["attempt_id"] != architect0["attempt_id"] and architect1["diag_grant"] == 1
        diagnosis.ingest(sup, architect1, answer("requeue"))
        assert diagnosis.count_requeues(layout, "T") == diagnosis.DIAGNOSIS_BUDGET
        tasks.set_status(layout, "T", "IN_PROGRESS")

        # Generation 2: the budget (counted across manager and architect requeues alike) is already spent, so this
        # exhaustion must go straight to the owner — no third manager diagnosis.
        rec = continuation_chain(layout, "T", start=40)
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")

    assert len(inbox.open_items(layout, "owner")) == 1
    manager_recs = [r for r in created if r.get("diag_level") == "manager"]
    assert len(manager_recs) == 2                                 # the budget stopped a third manager diagnosis


# ------------------------------------------------------------------ F2 regressions: resumable retirement


def old_markers(layout, tid="T"):
    return [r for r in jobs.for_task(layout, tid) if str(r.get("deferred_reason", "")).startswith("continuation")]


def start_manager_grant(layout, sup):
    """A task one continuation past the cap (three prior markers, exactly `old_markers`'s count below) with a
    manager diagnosis already waiting for its `requeue` verdict — built by hand, the same way
    `test_the_continuation_cap_is_still_enforced_once_the_diagnosis_budget_is_spent` does, so this test decides
    the grant itself (via a synthetic Result) instead of waiting on a real subprocess: `run_until` would let the
    loop's OWN settle()/ingest() grant it first, leaving nothing left for this test's injected crash to interrupt."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    continuation_chain(layout, "T")
    tasks.set_status(layout, "T", "BLOCKED", note="T.a4: capacity limit after 3 continuations")
    # attempt 5, not some far-off number: requeue()'s own crash-safety check ("a newer attempt already exists",
    # loop.py:157) keys off the attempt number alone, and a gap here would make a later fresh continuation's
    # attempt number look OLDER than this diagnosis job and never get dispatched.
    return jobs.write(layout, {"attempt_id": "T.a5", "task_id": "T", "adapter": "claude_cli", "phase": "RUNNING",
                              "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": "manager", "diag_role": "senior_pm",
                              "diag_signature": "s1", "diag_issue": None})


def requeue_result():
    return Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue", "summary": "again", "guidance": "retry"})


def test_a_crash_before_retirement_starts_in_the_diagnosis_path_is_finished_on_replay(layout, fake_claude, monkeypatch):
    """The reset (escalation.reset_retries) is one durable write; retirement (escalation.grant_continuations) is a
    second one. A crash between them left the grant spent and every old marker still counted (F2). Gating
    grant_continuations on `reset_retries`'s own "was this fresh" flag is exactly what left it unreachable on a
    replay (the key guard answers False); it must run every time instead, resumably."""
    sup = Supervisor(layout, sleep_s=0)
    diag = start_manager_grant(layout, sup)
    real_grant = escalation.grant_continuations
    monkeypatch.setattr(escalation, "grant_continuations", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before retirement")))
    with pytest.raises(RuntimeError):
        diagnosis.ingest(sup, diag, requeue_result())
    assert escalation._load(layout, "T").get("resets") == 1            # the reset is already durable
    assert len(old_markers(layout, "T")) == 3                          # nothing retired yet
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED        # never reached READY with retirement unfinished
    monkeypatch.setattr(escalation, "grant_continuations", real_grant)
    out = diagnosis.ingest(sup, diag, requeue_result())                # replay: settle() would re-collect the same result
    assert out["action"] == "requeue"
    assert escalation._load(layout, "T").get("resets") == 1            # still one grant, never a second
    assert old_markers(layout, "T") == []                               # the replay finished retiring every old marker


def test_a_crash_partway_through_the_retirement_loop_in_the_diagnosis_path_is_finished_on_replay(layout, fake_claude, monkeypatch):
    """escalation.py:250-253's per-record loop can itself be interrupted after retiring some markers but not all
    (F2); a replay must finish retiring exactly the rest, not restart from scratch and not skip them."""
    sup = Supervisor(layout, sleep_s=0)
    diag = start_manager_grant(layout, sup)
    real_set_phase = jobs.set_phase
    calls = {"n": 0}

    def flaky_set_phase(layout_, attempt_id, phase, **fields):
        if str(fields.get("deferred_reason", "")).startswith("reset ("):
            calls["n"] += 1
            if calls["n"] == 2:                     # the loop crashes after retiring exactly the first marker
                raise RuntimeError("crash partway through retirement")
        return real_set_phase(layout_, attempt_id, phase, **fields)
    monkeypatch.setattr(jobs, "set_phase", flaky_set_phase)
    with pytest.raises(RuntimeError):
        diagnosis.ingest(sup, diag, requeue_result())
    assert escalation._load(layout, "T").get("resets") == 1
    assert len(old_markers(layout, "T")) == 2                          # exactly one of the three was retired
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    monkeypatch.setattr(jobs, "set_phase", real_set_phase)
    out = diagnosis.ingest(sup, diag, requeue_result())                # replay finishes the other two
    assert out["action"] == "requeue"
    assert escalation._load(layout, "T").get("resets") == 1
    assert old_markers(layout, "T") == []


def test_a_replay_after_new_continuations_already_exist_retires_only_the_old_grant(layout, fake_claude):
    """The existing test (`test_a_manager_requeue_grants_a_fresh_continuation_chain_once_and_a_replay_grants_none`)
    only replays a grant that already retired everything before any fresh continuation existed. F2's third gap:
    once the fresh chain has earned its own continuation, a late-arriving replay of the grant decision (the exact
    settle()-re-collects-the-same-result path `diagnosis.ingest` documents, or a second call to
    `escalation.grant_continuations` itself) must retire none of it — only a marker older than the CURRENT grant,
    never one the fresh chain already created under it."""
    sup = Supervisor(layout, sleep_s=0)
    diag = start_manager_grant(layout, sup)
    diagnosis.ingest(sup, diag, requeue_result())
    assert escalation._load(layout, "T").get("resets") == 1
    assert old_markers(layout, "T") == []                               # the grant fully retired the old chain
    tasks.set_status(layout, "T", "IN_PROGRESS")
    n = len(jobs.for_task(layout, "T")) + 1
    rec2 = jobs.write(layout, {"attempt_id": f"T.a{n}", "task_id": "T", "adapter": "python", "phase": "RUNNING",
                              "spec": {"kind": "delivery"}, "handle": {}})
    sup.requeue(rec2, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    fresh = old_markers(layout, "T")
    assert len(fresh) == 1                                              # the fresh chain's own continuation
    escalation.grant_continuations(layout, "T", reason="late-arriving replay")
    assert old_markers(layout, "T") == fresh                            # the fresh marker is untouched


# ------------------------------------------------------------------ F3 regressions: a capped attempt stays
# recoverable until its escalation is durable


def _raise(*_a, **_k):
    raise RuntimeError("crash before escalation")


def test_a_capped_attempt_interrupted_before_requeue_s_own_escalation_is_finished_by_reconcile_alone(layout, fake_claude, monkeypatch):
    """requeue()'s cap branch marks the attempt DONE/continued/capped, then calls escalation.record_failure() for
    it. A crash between those two writes used to leave the job terminal and the task IN_PROGRESS with no issue or
    diagnosis, and nothing — not the torn-write repair above (it skips anything already `continued`), not the
    stall sweep (it excludes IN_PROGRESS tasks) — ever revisited it. A fresh Supervisor's own reconcile(), with no
    owner step, must finish it instead."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    rec = continuation_chain(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    real_record_failure = escalation.record_failure
    monkeypatch.setattr(escalation, "record_failure", _raise)
    with pytest.raises(RuntimeError):
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    monkeypatch.setattr(escalation, "record_failure", real_record_failure)
    # exactly the state the real crash point leaves durable: the cap write landed, the escalation call did not
    capped = jobs.read(layout, rec["attempt_id"])
    assert capped["phase"] == "DONE" and capped.get("continued") is True and capped.get("capped") is True
    assert jobs.open_records(layout) == []
    assert list(layout.issues.glob("I-*.yaml")) == []
    assert diag_records(layout, "T") == []
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS
    assert sup.sweep_stalled() == []                       # the stall sweep excludes IN_PROGRESS tasks (not its job)
    deliveries_before = {r["attempt_id"] for r in jobs.for_task(layout, "T")
                         if (r.get("spec") or {}).get("kind", "delivery") == "delivery"}
    fresh = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        fresh.reconcile()
    assert len(list(layout.issues.glob("I-*.yaml"))) == 1
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1                          # idempotent: three reconcile() calls, one diagnosis
    assert tasks.load(layout, "T").status != TaskStatus.IN_PROGRESS
    deliveries_after = {r["attempt_id"] for r in jobs.for_task(layout, "T")
                        if (r.get("spec") or {}).get("kind", "delivery") == "delivery"}
    assert deliveries_after == deliveries_before             # no bypassing requeue past the cap


def test_a_capped_attempt_interrupted_before_repair_s_own_escalation_is_finished_by_reconcile_alone(layout, fake_claude, monkeypatch):
    """The recovery-cap branch: repair_continuations() enforcing the cap itself, for a torn write AT the boundary
    (continued never set, as test_repair_continuations_enforces_the_same_cap_as_requeue builds), repeats the same
    write-then-escalate ordering — and can be interrupted the exact same way. A fresh Supervisor's reconcile()
    alone must still finish it, not a DONE/DEFERRED record missing the continued flag (the weaker, pre-existing
    case that test already covers end-to-end in one call)."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = continuation_chain(layout, "T")                  # 3 prior continuations already recorded
    jobs.set_phase(layout, rec["attempt_id"], "DONE", result="DEFERRED")   # torn write AT the cap boundary
    real_record_failure = escalation.record_failure
    monkeypatch.setattr(escalation, "record_failure", _raise)
    with pytest.raises(RuntimeError):
        sup.repair_continuations()
    monkeypatch.setattr(escalation, "record_failure", real_record_failure)
    capped = jobs.read(layout, rec["attempt_id"])
    assert capped["phase"] == "DONE" and capped.get("continued") is True and capped.get("capped") is True
    assert list(layout.issues.glob("I-*.yaml")) == []
    assert diag_records(layout, "T") == []
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS
    deliveries_before = {r["attempt_id"] for r in jobs.for_task(layout, "T")
                         if (r.get("spec") or {}).get("kind", "delivery") == "delivery"}
    fresh = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        fresh.reconcile()
    assert len(list(layout.issues.glob("I-*.yaml"))) == 1
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1
    assert tasks.load(layout, "T").status != TaskStatus.IN_PROGRESS
    deliveries_after = {r["attempt_id"] for r in jobs.for_task(layout, "T")
                        if (r.get("spec") or {}).get("kind", "delivery") == "delivery"}
    assert deliveries_after == deliveries_before             # no bypassing requeue past the cap


# ------------------------------------------------------------------ F6 regressions: the cap event's own side
# effects (the issue, the BLOCKED transition, the routing that follows) finish as one idempotent transition,
# not merely "the task left IN_PROGRESS"


def test_a_capped_attempt_s_issue_without_its_status_transition_is_finished_by_reconcile_alone(layout, fake_claude, monkeypatch):
    """escalation._apply saves the issue, then moves the task off IN_PROGRESS, then dispatches the manager. A
    crash between the issue write and the status write used to leave the status write skipped forever on replay
    (it only ran `if issue is None`), while the dispatch below it still ran unconditionally: a manager requeue on
    a task still IN_PROGRESS never sets READY (diagnosis.ingest only does that for BLOCKED/FAILED), stranding it
    IN_PROGRESS with no open job. A fresh Supervisor's own reconcile() must finish the status transition too, not
    just whatever ran past it, and the requeued chain must reach READY. The manager diagnosis runs as a real
    subprocess (FakeDiagnosisClaude), so ordinary reconcile() cycles are driven through reconcile_until's poll
    rather than a fixed count of calls racing it; reconcile() alone (never once()) is also what keeps this
    deterministic, since once()'s scheduler step would launch the just-granted continuation's own delivery
    attempt through the same fake adapter and fail it before this test ever gets to assert anything."""
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    rec = continuation_chain(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    real_set_status = tasks.set_status
    monkeypatch.setattr(tasks, "set_status", _raise)
    with pytest.raises(RuntimeError):
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    monkeypatch.setattr(tasks, "set_status", real_set_status)
    # the issue landed, the status write that follows it in the same call did not
    assert len(list(layout.issues.glob("I-*.yaml"))) == 1
    assert diag_records(layout, "T") == []
    assert jobs.open_records(layout) == []
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS
    assert sup.sweep_stalled() == []                       # the stall sweep excludes IN_PROGRESS tasks (not its job)
    deliveries_before = {r["attempt_id"] for r in jobs.for_task(layout, "T")
                         if (r.get("spec") or {}).get("kind", "delivery") == "delivery"}
    fresh = Supervisor(layout, sleep_s=0)
    reconcile_until(fresh, lambda: tasks.load(layout, "T").status == TaskStatus.READY)
    assert len(list(layout.issues.glob("I-*.yaml"))) == 1                        # no second issue
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1                                              # idempotent: one diagnosis
    assert tasks.load(layout, "T").status == TaskStatus.READY                   # not stranded IN_PROGRESS
    assert inbox.open_items(layout, "owner") == []
    deliveries_after = {r["attempt_id"] for r in jobs.for_task(layout, "T")
                        if (r.get("spec") or {}).get("kind", "delivery") == "delivery"}
    assert deliveries_after == deliveries_before             # no bypassing requeue past the cap


def test_a_capped_attempt_s_blocked_status_without_its_manager_dispatch_is_finished_by_reconcile_alone(layout, fake_claude, monkeypatch):
    """A crash between the BLOCKED write and the manager dispatch used to make the recovery guard
    (`_repair_capped_escalation`) return at once: it read "left IN_PROGRESS" as proof the escalation had
    finished, but BLOCKED is this same event's own intermediate state, not only a sign of something else. Ordinary
    reconcile() cycles must dispatch the manager diagnosis (not nothing), and the later stall sweep must not raise
    an owner item for a task whose diagnosis budget was never touched and whose manager lane was free. The
    dispatch runs as a real subprocess, so reconcile_until's poll drives reconcile() here too, rather than a fixed
    count of calls racing it."""
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    rec = continuation_chain(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    real_dispatch = diagnosis.dispatch
    monkeypatch.setattr(diagnosis, "dispatch", _raise)
    with pytest.raises(RuntimeError):
        sup.requeue(rec, Result(status="DEFERRED", error="still waiting", reset_epoch=time.time()), "capacity limit")
    monkeypatch.setattr(diagnosis, "dispatch", real_dispatch)
    # the issue and the BLOCKED status landed, the dispatch call right after them did not
    assert len(list(layout.issues.glob("I-*.yaml"))) == 1
    assert diag_records(layout, "T") == []
    assert jobs.open_records(layout) == []
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    fresh = Supervisor(layout, sleep_s=0)
    reconcile_until(fresh, lambda: len([r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]) == 1)
    assert len(list(layout.issues.glob("I-*.yaml"))) == 1                        # no second issue
    manager_diags = [r for r in diag_records(layout, "T") if r.get("diag_level") == "manager"]
    assert len(manager_diags) == 1                                              # the dispatch the crash skipped
    assert inbox.open_items(layout, "owner") == []
    # the sweep clock advanced an hour must not raise an owner item: no diagnosis budget was spent and the
    # manager lane was free, the opposite of what a stalled/stuck task looks like
    assert fresh.sweep_stalled(now=time.time() + 3700) == []
    assert inbox.open_items(layout, "owner") == []
