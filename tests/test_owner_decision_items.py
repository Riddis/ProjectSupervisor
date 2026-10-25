"""Owner decision items for a task BLOCKED by unreadable reviews: one item, then REQUEUE (review budget reset, the
verdict on disk salvaged, no new review paid for), DEFER and CANCEL. Upstreamed from the Studio's
tests/test_owner_decisions.py at its migration phase 6: test_review_salvage covered these only in part."""
import json

from supervisor import escalation, inbox, jobs, review, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

DIGEST = "d" * 64


def review_text(category="requirements"):
    obj = {"kind": "review", "schema_version": 1, "verdict": "REVISE", "summary": "s", "reviewed": ["T.txt"],
           "digest": DIGEST, "findings": [{"category": category, "blocking": True, "message": "m", "evidence": "e:1"}]}
    return "prose\n```json\n" + json.dumps(obj) + "\n```\n"


def blocked_by_unreadable_reviews(layout, tid="T"):
    make_task(layout, tid, reviewer=["code_review"])
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.record_delivery(layout, tid, DIGEST, actor="test", paths=["T.txt"])
    tasks.record_verification(layout, tid, DIGEST, True, detail="ok")
    tasks.advance(layout, tid)
    sup = Supervisor(layout, sleep_s=0)
    for n in (1, 2):
        run_dir = layout.runs / tid / f"{tid}.r{n}"
        run_dir.mkdir(parents=True)
        (run_dir / "result.txt").write_text(review_text("vibes" if n == 1 else "requirements"), encoding="utf-8")
        rec = jobs.write(layout, {"attempt_id": f"{tid}.r{n}", "task_id": tid, "adapter": "claude_cli", "phase": "RUNNING",
                                  "spec": {"kind": "review", "run_dir": str(run_dir)}, "handle": {},
                                  "review_role": "code_review", "review_digest": DIGEST})
        review.ingest(sup, rec, Result(status="FAILED", error="no structured result after follow-up"))
        jobs.set_phase(layout, f"{tid}.r{n}", "DONE")
    assert tasks.load(layout, tid).status == TaskStatus.BLOCKED
    return sup


def decision_items(layout, tid="T"):
    return [i for i in inbox.all_items(layout) if i.get("kind") == "decision" and i.get("task_id") == tid]


def test_a_blocked_task_reaches_the_owner_once(layout):
    sup = blocked_by_unreadable_reviews(layout)
    sup.request_owner_decisions()
    sup.request_owner_decisions()
    items = decision_items(layout)
    assert len(items) == 1 and items[0]["for"] == "owner" and items[0]["resume"] == "review"
    assert items[0]["allowed_responses"] == ["REQUEUE", "CANCEL", "DEFER"] and "REQUEUE" in items[0]["summary"]


def test_requeue_resets_the_review_budget_and_salvages_the_verdict_on_disk(layout, monkeypatch):
    sup = blocked_by_unreadable_reviews(layout)
    sup.request_owner_decisions()
    item = decision_items(layout)[0]
    inbox.answer(layout, item["id"], "REQUEUE")
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.WAITING_FOR_REVIEW
    assert not any(jobs.read(layout, f"T.r{n}").get("failed_review") for n in (1, 2))
    assert inbox.load(layout, item["id"])["applied"]["resumed"] == "review"
    failures = []
    monkeypatch.setattr(escalation, "record_failure", lambda sup, tid, src, detail, event=None: failures.append((tid, src, event)))
    dispatched = []
    monkeypatch.setattr(review, "dispatch", lambda *a, **k: dispatched.append(a))
    sup.dispatch_reviews()
    t = tasks.load(layout, "T")
    assert [r.verdict.value for r in t.delivery.reviews] == ["REVISE"]          # r2's answer, now readable
    assert failures == [("T", "review", "T.r2")] and dispatched == []           # revision route, no new review paid for
    assert jobs.read(layout, "T.r2")["salvaged"] is True
    sup.dispatch_reviews()
    assert len(tasks.load(layout, "T").delivery.reviews) == 1                   # never salvaged twice


def test_cancel_and_defer(layout):
    sup = blocked_by_unreadable_reviews(layout)
    sup.request_owner_decisions()
    inbox.answer(layout, decision_items(layout)[0]["id"], "DEFER", note="later")
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    sup2 = blocked_by_unreadable_reviews(layout, "U")
    sup2.request_owner_decisions()
    inbox.answer(layout, decision_items(layout, "U")[0]["id"], "CANCEL")
    assert tasks.load(layout, "U").status == TaskStatus.CANCELLED


