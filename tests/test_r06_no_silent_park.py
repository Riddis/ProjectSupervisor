"""R-06: an owner answer that is not one of a decision item's three keywords (REQUEUE/CANCEL/DEFER) must not park
the task with nothing watching it; an idle alert must name every non-terminal task waiting, not only three
statuses, and must not fire for a task the owner is deliberately holding; a review that cannot be dispatched for
capacity must leave a reason behind, not fail silently."""
import json
import time

import pytest

from supervisor import inbox, review, tasks
from supervisor._vendor.contract import CapacityDeferred
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def events(layout, name: str) -> list[dict]:
    """The structured log records of one event name, parsed from state/log.jsonl."""
    log = layout.state / "log.jsonl"
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [r for r in (json.loads(ln) for ln in lines if ln.strip()) if r.get("event") == name]


def _stopped_task(layout, tid, status, minutes_ago):
    make_task(layout, tid)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.set_status(layout, tid, status, note="left here by something that routes nothing")
    return time.time() + minutes_ago * 60


# ---- scenario 1: a free-text or DEFER answer must not park the task silently


@pytest.mark.parametrize("token", ["a", "DEFER"])
def test_free_text_or_defer_answer_leaves_the_task_visibly_waiting_on_the_owner(layout, token):
    tid = f"T-{token}"
    stop_at = _stopped_task(layout, tid, "BLOCKED", minutes_ago=15)
    item = inbox.create(layout, for_role="owner", subject="s", summary="s", kind=inbox.DECISION_KIND,
                        allowed_responses=list(inbox.DECISION_RESPONSES), task_id=tid)
    inbox.answer(layout, item["id"], token)
    assert tasks.load(layout, tid).status == TaskStatus.BLOCKED       # neither answer resumes the task

    sup = Supervisor(layout, sleep_s=0)
    sup.sweep_stalled(now=stop_at + 7 * 24 * 3600)                    # a week later

    still_waiting = [i for i in inbox.open_items(layout, "owner") if i.get("task_id") == tid]
    assert still_waiting, (
        f"{tid}: a week after a {token!r} answer, the task must still be listed as waiting on the owner "
        "(an open or re-opened item), never silent")
    assert set(inbox.DECISION_RESPONSES) <= set(still_waiting[0].get("allowed_responses") or []), (
        "the task must still be reachable with a real way to act (REQUEUE/CANCEL/DEFER)"
    )


# ---- scenario 2: the idle alert must name every waiting status and skip an owner hold


def _in_status(layout, tid, status):
    make_task(layout, tid)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    if status != "IN_PROGRESS":
        tasks.set_status(layout, tid, status)


def test_idle_alert_names_every_non_terminal_status_and_skips_an_owner_hold(layout):
    _in_status(layout, "A", "IN_PROGRESS")
    _in_status(layout, "B", "WAITING_FOR_REVIEW")
    _in_status(layout, "C", "WAITING_FOR_HUMAN")
    _in_status(layout, "D", "BLOCKED")
    _in_status(layout, "E", "FAILED")
    make_task(layout, "H")
    tasks.hold(layout, "H", actor="owner", note="held for now")        # the owner's own call, not a stall

    sup = Supervisor(layout, sleep_s=0)
    now = time.time()
    assert sup.alert_if_idle(now=now) is None                          # idle clock starts
    assert sup.alert_if_idle(now=now + 60) is None                     # not long enough yet
    item = sup.alert_if_idle(now=now + 3600)
    assert item is not None
    text = item["summary"] + (item.get("detail") or "")
    for tid, status in (("A", "IN_PROGRESS"), ("B", "WAITING_FOR_REVIEW"), ("C", "WAITING_FOR_HUMAN"),
                         ("D", "BLOCKED"), ("E", "FAILED")):
        assert f"{tid} {status}" in text, f"the idle alert must name {tid} ({status}) and what it waits for"
    assert f"H {TaskStatus.BLOCKED.value}" not in text, "a task the owner holds is not the pipeline's problem"
    assert not (set(item.get("allowed_responses") or []) & {"ACCEPT", "REJECT", "MODIFY", "REDO"}), (
        "the idle alert must carry no approve/reject buttons: none of them does anything for 'nothing can run'"
    )


def test_idle_alert_is_not_raised_for_an_owner_hold_alone(layout):
    make_task(layout, "H")
    tasks.hold(layout, "H", actor="owner", note="held for now")
    sup = Supervisor(layout, sleep_s=0)
    now = time.time()
    for bump in (0, 60, 3600, 2 * 3600):
        assert sup.alert_if_idle(now=now + bump) is None, (
            "a task the owner is holding must never raise the idle alert by itself"
        )


# ---- scenario 3: a review deferred for capacity must leave a logged, visible reason


def test_review_dispatch_deferred_for_capacity_logs_a_reason(layout, monkeypatch):
    make_task(layout, "T", reviewer=["code_review"])
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.record_delivery(layout, "T", "d" * 64, actor="test", paths=["T.txt"])
    tasks.record_verification(layout, "T", "d" * 64, True, detail="ok")
    tasks.advance(layout, "T")                                          # -> WAITING_FOR_REVIEW

    def deny(layout_, engine):
        raise CapacityDeferred(0.0, "no capacity")
    monkeypatch.setattr("supervisor.capacity.require", deny)

    sup = Supervisor(layout, sleep_s=0)
    out = review.dispatch(sup, tasks.load(layout, "T"), "code_review")
    assert out is None
    assert events(layout, "review_deferred"), (
        "a review deferred for capacity must leave one logged, visible reason behind, not fail silently"
    )
