"""Owner responses must re-trigger the tasks they gate (regression for 2026-10-20: H-002 APPROVE left T0-24 in
WAITING_FOR_HUMAN; OT-010 done left T0-13 FAILED). Two bridges are under test: the loop's `approval` inbox item +
inbox.apply_approval for human_approval=required tasks, and owner_tasks.set_status(done) releasing `blocks`."""

from supervisor import commands as commands_mod
from supervisor import inbox as inbox_mod
from supervisor import owner_tasks as owner_tasks_mod
from supervisor import store
from supervisor import tasks as tasks_mod
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_ui import auth_client, client, app, post  # noqa: F401  (fixtures)


def _approved_delivery(layout, tid="A", reviewer=("senior_pm",)):
    make_task(layout, tid, reviewer=reviewer, human="required", status="IN_PROGRESS")
    tasks_mod.record_delivery(layout, tid, "d1")
    tasks_mod.record_verification(layout, tid, "d1", ok=True)
    for role in reviewer:
        tasks_mod.record_review(layout, tid, "d1", role, "ACCEPT")
    t = tasks_mod.advance(layout, tid)
    assert t.status == TaskStatus.WAITING_FOR_HUMAN
    return t


# ------------------------------------------------------------------ approval bridge


def test_loop_opens_one_approval_item_per_digest(layout):
    _approved_delivery(layout)
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    items = [i for i in inbox_mod.open_items(layout, "owner") if i.get("kind") == inbox_mod.APPROVAL_KIND]
    assert len(items) == 1
    assert items[0]["task_id"] == "A" and items[0]["approval_digest"] == "d1"
    assert "APPROVE" in items[0]["allowed_responses"]
    sup.once()
    sup.once()
    assert len([i for i in inbox_mod.open_items(layout, "owner") if i.get("kind") == inbox_mod.APPROVAL_KIND]) == 1


def test_answering_the_approval_item_completes_the_task(layout):
    _approved_delivery(layout)
    Supervisor(layout, sleep_s=0).once()
    item = inbox_mod.open_items(layout, "owner")[0]
    out = commands_mod.HANDLERS["inbox.answer"](layout, {"item_id": item["id"], "response": "APPROVE", "_id": "c1"}, "owner-ui")
    assert out["task_status"] == "COMPLETE"
    t = tasks_mod.load(layout, "A")
    assert t.status == TaskStatus.COMPLETE
    assert t.delivery.approvals[-1].by == item["id"] and t.delivery.approvals[-1].digest == "d1"
    # a retried command is a no-op, not a second approval
    commands_mod.HANDLERS["inbox.answer"](layout, {"item_id": item["id"], "response": "APPROVE", "_id": "c1"}, "owner-ui")
    assert len(tasks_mod.load(layout, "A").delivery.approvals) == 1


def test_modify_on_the_approval_item_sends_the_task_back_to_work(layout):
    _approved_delivery(layout)
    Supervisor(layout, sleep_s=0).once()
    item = inbox_mod.open_items(layout, "owner")[0]
    inbox_mod.answer(layout, item["id"], "MODIFY shorter runbook", by="owner")
    t = tasks_mod.load(layout, "A")
    assert t.status == TaskStatus.IN_PROGRESS
    assert t.delivery.approvals[-1].response.value == "MODIFY"
    assert "shorter runbook" in t.delivery.approvals[-1].note


def test_agent_written_question_for_a_waiting_task_counts_as_the_approval(layout):
    """The H-002 shape: the agent itself asked the owner (kind=question, task_id set, no attempt) and the owner
    clicked APPROVE before the loop could open its own item."""
    _approved_delivery(layout)
    item = inbox_mod.create(layout, for_role="owner", subject="Approve archiving", summary="move trees to E:",
                            kind="question", allowed_responses=["APPROVE", "MODIFY", "REJECT"], from_="identity_engineer",
                            task_id="A", attempt_id=None)
    inbox_mod.answer(layout, item["id"], "APPROVE", by="owner-ui", command_id="c9")
    assert tasks_mod.load(layout, "A").status == TaskStatus.COMPLETE
    Supervisor(layout, sleep_s=0).once()                        # nothing more to ask
    assert not inbox_mod.open_items(layout, "owner")


def test_free_text_on_a_plain_question_is_not_an_approval(layout):
    _approved_delivery(layout)
    item = inbox_mod.create(layout, for_role="owner", subject="q", summary="s", kind="question", task_id="A")
    inbox_mod.answer(layout, item["id"], "let me think about it", by="owner")
    assert tasks_mod.load(layout, "A").status == TaskStatus.WAITING_FOR_HUMAN
    assert not tasks_mod.load(layout, "A").delivery.approvals


def test_human_adapter_tasks_are_left_to_their_attempt(layout):
    from tests.test_inbox import human_task
    human_task(layout, "H", subject="decide X", summary="pick one")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    sup.once()
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1 and items[0].get("kind") != inbox_mod.APPROVAL_KIND


def test_stale_approval_item_is_refused_after_redelivery(layout):
    _approved_delivery(layout)
    Supervisor(layout, sleep_s=0).once()
    item = inbox_mod.open_items(layout, "owner")[0]
    tasks_mod.record_delivery(layout, "A", "d2")                # the agent re-delivered meanwhile
    inbox_mod.answer(layout, item["id"], "APPROVE", by="owner")
    t = tasks_mod.load(layout, "A")
    assert not [a for a in t.delivery.approvals if a.digest == "d2"]
    assert t.status != TaskStatus.COMPLETE


def test_self_heal_reapplies_an_answered_item_without_a_record(layout):
    _approved_delivery(layout)
    Supervisor(layout, sleep_s=0).once()
    item = inbox_mod.open_items(layout, "owner")[0]

    def fn(doc):                                                # simulate a crash between the two writes
        doc["status"] = "answered"
        doc["answer"] = {"response": "APPROVE", "kind": "APPROVE", "note": "", "by": "owner", "command_id": "zz"}
        return doc
    store.update(inbox_mod.path_for(layout, item["id"]), fn, lock_path=layout.state / ".inbox.lock")
    assert tasks_mod.load(layout, "A").status == TaskStatus.WAITING_FOR_HUMAN
    Supervisor(layout, sleep_s=0).once()
    assert tasks_mod.load(layout, "A").status == TaskStatus.COMPLETE


def test_task_page_approve_button(layout, auth_client):  # noqa: F811
    _approved_delivery(layout)
    r = auth_client.get("/tasks/A")
    assert "Needs your approval" in r.text and "/tasks/A/approve" in r.text
    r2 = post(auth_client, "/tasks/A/approve", response="ACCEPT", note="fine")
    assert r2.status_code == 303 and "flash=applied" in r2.headers["location"]
    assert tasks_mod.load(layout, "A").status == TaskStatus.COMPLETE
    r3 = auth_client.get("/tasks/A")
    assert "Needs your approval" not in r3.text and "Owner: ACCEPT" in r3.text


# ------------------------------------------------------------------ owner task blocks


def _owner_rows(layout, rows):
    layout.owner_tasks.write_text(store.dump({"owner_tasks": rows}), encoding="utf-8")


def test_owner_task_done_requeues_failed_and_blocked_tasks(layout):
    make_task(layout, "F", status="FAILED")
    make_task(layout, "B", status="BLOCKED")
    make_task(layout, "C", status="IN_PROGRESS")
    make_task(layout, "D", status="COMPLETE")
    _owner_rows(layout, [{"id": "OT-01", "title": "x", "status": "open", "blocks": ["F", "B", "C", "D", "NOPE"],
                          "priority": "high", "guidance": "g"}])
    row = owner_tasks_mod.set_status(layout, "OT-01", "done", actor="owner-ui")
    assert sorted(row["released"]) == ["B", "F"]
    assert tasks_mod.load(layout, "F").status == TaskStatus.READY
    assert tasks_mod.load(layout, "B").status == TaskStatus.READY
    assert tasks_mod.load(layout, "C").status == TaskStatus.IN_PROGRESS
    assert tasks_mod.load(layout, "D").status == TaskStatus.COMPLETE
    assert "OT-01" in tasks_mod.load(layout, "F").history[-1]["note"]
    assert tasks_mod.eligible(layout)[0].id in ("B", "F")


def test_owner_task_done_waits_for_the_other_blocking_owner_task(layout):
    make_task(layout, "F", status="FAILED")
    _owner_rows(layout, [{"id": "OT-01", "title": "x", "status": "open", "blocks": ["F"], "priority": "high", "guidance": "g"},
                         {"id": "OT-02", "title": "y", "status": "open", "blocks": ["F"], "priority": "high", "guidance": "g"}])
    assert owner_tasks_mod.set_status(layout, "OT-01", "done")["released"] == []
    assert tasks_mod.load(layout, "F").status == TaskStatus.FAILED
    assert owner_tasks_mod.set_status(layout, "OT-02", "done")["released"] == ["F"]
    assert tasks_mod.load(layout, "F").status == TaskStatus.READY


def test_snooze_does_not_release(layout):
    make_task(layout, "F", status="FAILED")
    _owner_rows(layout, [{"id": "OT-01", "title": "x", "status": "open", "blocks": ["F"], "priority": "high", "guidance": "g"}])
    row = owner_tasks_mod.set_status(layout, "OT-01", "snoozed")
    assert "released" not in row
    assert tasks_mod.load(layout, "F").status == TaskStatus.FAILED


def test_owner_task_done_via_command_reports_released(layout):
    make_task(layout, "F", status="FAILED")
    _owner_rows(layout, [{"id": "OT-01", "title": "x", "status": "open", "blocks": ["F"], "priority": "high", "guidance": "g"}])
    out = commands_mod.HANDLERS["owner_task.set_status"](layout, {"owner_task_id": "OT-01", "status": "done"}, "owner-ui")
    assert out == {"status": "done", "released": ["F"]}
