import pytest

from supervisor import escalation, inbox, jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_continuation_diagnosis_route import continuation_chain, old_markers


def human_task(layout, tid, deps=(), subject="decide X", summary="pick one", allowed=None, for_role="owner"):
    make_task(layout, tid, deps=deps)

    def fn(doc):
        doc["job"] = {"adapter": "human", "for_role": for_role, "subject": subject, "summary": summary,
                      "allowed_responses": allowed}
        doc["artifacts"] = []

    tasks._update(layout, tid, fn)


# --------------------------------------------------------------------------- inbox store


def test_create_and_answer_roundtrip(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    assert item["id"] == "H-001"
    assert item["status"] == "open"
    assert inbox.open_items(layout) == [item]
    answered = inbox.answer(layout, item["id"], "ACCEPT", note="looks good")
    assert answered["status"] == "answered"
    assert answered["answer"]["kind"] == "ACCEPT"
    assert inbox.open_items(layout) == []


def test_free_text_answer_is_valid_by_default(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    answered = inbox.answer(layout, item["id"], "make the hair longer and redo it")
    assert answered["answer"]["kind"] == "text"
    assert answered["status"] == "answered"


def test_structured_response_with_argument(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    answered = inbox.answer(layout, item["id"], "REDO #3")
    assert answered["answer"]["kind"] == "REDO"
    assert answered["answer"]["response"] == "REDO #3"


def test_answer_validation_rejects_non_allowed_when_free_text_disabled(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body",
                        allowed_responses=["ACCEPT", "REJECT"], free_text=False)
    with pytest.raises(inbox.InboxError):
        inbox.answer(layout, item["id"], "MODIFY something")


def test_answer_validation_rejects_empty_response(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    with pytest.raises(inbox.InboxError):
        inbox.answer(layout, item["id"], "   ")


def test_answer_validation_rejects_double_answer(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    inbox.answer(layout, item["id"], "ACCEPT")
    with pytest.raises(inbox.InboxError):
        inbox.answer(layout, item["id"], "REJECT")


def test_answer_is_idempotent_by_command_id(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    first = inbox.answer(layout, item["id"], "ACCEPT", command_id="cmd-1")
    second = inbox.answer(layout, item["id"], "ACCEPT", command_id="cmd-1")
    assert first["answer"]["at"] == second["answer"]["at"]


def test_answer_unknown_item_raises(layout):
    with pytest.raises(inbox.InboxError):
        inbox.answer(layout, "H-999", "ACCEPT")


def test_summary_over_limit_rejected(layout):
    with pytest.raises(inbox.InboxError):
        inbox.create(layout, for_role="owner", subject="s", summary="x" * 601)


def test_cap_on_open_items_per_role(layout):
    for _ in range(3):
        inbox.create(layout, for_role="owner", subject="s", summary="body")
    with pytest.raises(inbox.InboxFull):
        inbox.create(layout, for_role="owner", subject="s", summary="body")
    # a different role is unaffected
    inbox.create(layout, for_role="engineer", subject="s", summary="body")


# --------------------------------------------------------------------------- human adapter + loop


def test_task_goes_waiting_for_human_and_resumes_on_answer(layout):
    human_task(layout, "H")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    task = tasks.load(layout, "H")
    assert task.status == TaskStatus.WAITING_FOR_HUMAN
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1
    assert items[0]["subject"] == "decide X"
    assert items[0]["task_id"] == "H"

    inbox.answer(layout, items[0]["id"], "ACCEPT")
    sup.once()
    assert tasks.load(layout, "H").status == TaskStatus.COMPLETE
    assert inbox.open_items(layout) == []


def test_human_task_fails_on_more_like_with_feedback_recorded(layout):
    human_task(layout, "H")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    item = inbox.open_items(layout, "owner")[0]
    inbox.answer(layout, item["id"], "REDO #2")
    sup.once()
    assert tasks.load(layout, "H").status == TaskStatus.FAILED
    rec = jobs.read(layout, "H.a1")
    assert rec["result"] == "FAILED"


def test_human_task_fails_on_reject(layout):
    human_task(layout, "H")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    item = inbox.open_items(layout, "owner")[0]
    inbox.answer(layout, item["id"], "REJECT", note="no")
    sup.once()
    assert tasks.load(layout, "H").status == TaskStatus.FAILED


@pytest.mark.parametrize("answer, status", [("first", TaskStatus.COMPLETE), ("REJECT", TaskStatus.FAILED)])
def test_a_human_job_offering_approve_completes_on_it_after_migrate(layout, answer, status):
    """Review 1.10.1 F1: a human job may offer the approval list (APPROVE/REJECT/MODIFY). Its item is attempt-backed,
    so the approval bridge (inbox.apply_approval) never sees it; the adapter itself must read APPROVE as ACCEPT.
    `supervisor migrate` keeps APPROVE in the list (1.10.1), so the owner's first button is APPROVE."""
    from supervisor import migrations
    human_task(layout, "H", allowed=list(inbox.APPROVAL_RESPONSES))
    tasks._update(layout, "H", lambda doc: doc["job"].update(free_text=False))
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    item = inbox.open_items(layout, "owner")[0]
    assert item["attempt_id"] == "H.a1"
    migrations._approval_responses(layout, dry_run=False)
    offered = inbox.load(layout, item["id"])["allowed_responses"]
    assert offered == ["APPROVE", "REJECT", "MODIFY"]
    inbox.answer(layout, item["id"], offered[0] if answer == "first" else answer)
    sup.once()
    assert tasks.load(layout, "H").status == status
    assert jobs.read(layout, "H.a1")["result"] == status.value


# --------------------------------------------------------------------------- F2 regressions: resumable retirement
# (supervisor/inbox.py's owner delivery-REQUEUE, the same ordering diagnosis.ingest has — docs/releases/2.1.0/
# F2-resumable-grant-retirement.md)


def blocked_delivery_item(layout, tid="T"):
    """An answered REQUEUE item for a task BLOCKED with its continuation budget exhausted (the diagnosis budget
    already spent, so the owner's own REQUEUE is what grants a fresh continuation chain — resume == "delivery"):
    the exact shape apply_decision() acts on and a replay re-applies (answer.py's `answer()` writes `status:
    answered` durably, then calls apply_decision() as a second, separate step — a crash between the two, or a
    retried apply_decision() call, passes this same dict again)."""
    make_task(layout, tid)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    rec = continuation_chain(layout, tid)
    tasks.set_status(layout, tid, "BLOCKED",
                     note=f"{rec['attempt_id']}: capacity limit after 3 continuations — owner decision needed")
    Supervisor(layout, sleep_s=0).request_owner_decisions()
    item = next(i for i in inbox.all_items(layout) if i.get("task_id") == tid)
    assert item["resume"] == "delivery"
    return {**item, "status": "answered", "answer": {"response": "REQUEUE"}}


def test_a_crash_before_retirement_starts_in_the_owner_requeue_path_is_finished_on_replay(layout, monkeypatch):
    """supervisor/inbox.py:369/380-381 has the same ordering diagnosis.py:301-303 did (F2): a crash between
    `reset_retries` and `grant_continuations` must not leave the old markers counted forever against the grant a
    replayed REQUEUE already spent."""
    answered = blocked_delivery_item(layout, "T")
    real_grant = escalation.grant_continuations
    monkeypatch.setattr(escalation, "grant_continuations", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before retirement")))
    with pytest.raises(RuntimeError):
        inbox.apply_decision(layout, answered)
    assert escalation._load(layout, "T").get("resets") == 1        # the reset is already durable
    assert len(old_markers(layout, "T")) == 3                       # nothing retired yet
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED     # never reached READY with retirement unfinished
    monkeypatch.setattr(escalation, "grant_continuations", real_grant)
    out = inbox.apply_decision(layout, answered)                    # replay: the same item re-applied
    assert out["resumed"] == "delivery"
    assert escalation._load(layout, "T").get("resets") == 1         # still one grant, never a second
    assert old_markers(layout, "T") == []                            # the replay finished retiring every old marker
    assert tasks.load(layout, "T").status == TaskStatus.READY


def test_a_crash_partway_through_the_retirement_loop_in_the_owner_requeue_path_is_finished_on_replay(layout, monkeypatch):
    """escalation.py:250-253's per-record loop interrupted after retiring some but not all markers (F2), reached
    through the owner REQUEUE path this time: a replay must finish retiring exactly the rest."""
    answered = blocked_delivery_item(layout, "T")
    real_set_phase = jobs.set_phase
    calls = {"n": 0}

    def flaky_set_phase(layout_, attempt_id, phase, **fields):
        if str(fields.get("deferred_reason", "")).startswith("reset ("):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("crash partway through retirement")
        return real_set_phase(layout_, attempt_id, phase, **fields)
    monkeypatch.setattr(jobs, "set_phase", flaky_set_phase)
    with pytest.raises(RuntimeError):
        inbox.apply_decision(layout, answered)
    assert escalation._load(layout, "T").get("resets") == 1
    assert len(old_markers(layout, "T")) == 2                       # exactly one of the three was retired
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    monkeypatch.setattr(jobs, "set_phase", real_set_phase)
    out = inbox.apply_decision(layout, answered)
    assert out["resumed"] == "delivery"
    assert escalation._load(layout, "T").get("resets") == 1
    assert old_markers(layout, "T") == []


def test_owner_requeue_replay_after_new_continuations_already_exist_retires_only_the_old_grant(layout):
    """F2's third gap in the owner REQUEUE path: once the fresh chain has earned its own continuation, a
    late-arriving replay of the grant decision must retire none of it."""
    answered = blocked_delivery_item(layout, "T")
    out = inbox.apply_decision(layout, answered)
    assert out["resumed"] == "delivery"
    assert escalation._load(layout, "T").get("resets") == 1
    assert old_markers(layout, "T") == []                            # the grant fully retired the old chain
    sup = Supervisor(layout, sleep_s=0)
    n = len(jobs.for_task(layout, "T")) + 1
    rec2 = jobs.write(layout, {"attempt_id": f"T.a{n}", "task_id": "T", "adapter": "python", "phase": "RUNNING",
                              "spec": {"kind": "delivery"}, "handle": {}})
    sup.requeue(rec2, Result(status="DEFERRED", error="still waiting"), "capacity limit")
    fresh = old_markers(layout, "T")
    assert len(fresh) == 1                                           # the fresh chain's own continuation
    escalation.grant_continuations(layout, "T", reason="late-arriving replay")
    assert old_markers(layout, "T") == fresh                         # the fresh marker is untouched


def test_crash_before_submit_redispatches_same_attempt(layout):
    human_task(layout, "H")
    task = tasks.load(layout, "H")
    spec = REGISTRY["human"].prepare(layout, task, "H.a1")
    jobs.prepare(layout, "H", "H.a1", "human", spec)          # crash here: record exists, nothing launched
    Supervisor(layout, sleep_s=0).once()                      # restart: reconcile relaunches H.a1
    assert tasks.load(layout, "H").status == TaskStatus.WAITING_FOR_HUMAN
    recs = list(layout.jobs.glob("H.*.json"))
    assert [r.name for r in recs] == ["H.a1.json"]
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1


def test_dispatch_deferred_without_consuming_an_attempt_when_inbox_full(layout):
    for i in range(3):
        human_task(layout, f"H{i}")
    human_task(layout, "H3")
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        sup.once()
    for i in range(3):
        assert tasks.load(layout, f"H{i}").status == TaskStatus.WAITING_FOR_HUMAN
    # H3 could not open a 4th inbox item for "owner": deferred, not failed, not waiting
    task3 = tasks.load(layout, "H3")
    assert task3.status == TaskStatus.IN_PROGRESS
    recs = jobs.for_task(layout, "H3")
    assert len(recs) == 1
    assert recs[0]["phase"] == "PREPARED"
    assert recs[0].get("deferred_until")
    assert len(inbox.open_items(layout, "owner")) == 3


def test_no_timeout_past_a_human_gate(layout):
    """Many iterations with no answer: the task stays WAITING_FOR_HUMAN forever, never FAILED/BLOCKED."""
    human_task(layout, "H")
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(10):
        sup.once()
    assert tasks.load(layout, "H").status == TaskStatus.WAITING_FOR_HUMAN
