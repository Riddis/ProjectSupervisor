import pytest

from supervisor import inbox, jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


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
