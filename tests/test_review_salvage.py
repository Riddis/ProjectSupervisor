"""A review verdict that the contract once refused over a category spelling is salvaged from disk instead of a
second review being paid for (supervisor.review.salvage, _vendor.contract.normalize_category)."""
import json

import pytest

from supervisor import escalation, inbox, jobs, review, tasks
from supervisor._vendor.contract import review_result
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

DIGEST = "d" * 16


def review_text(category="requirements"):
    obj = {"kind": "review", "schema_version": 1, "verdict": "REVISE", "summary": "s", "reviewed": ["T.txt"],
           "digest": DIGEST, "findings": [{"category": category, "blocking": True, "message": "m", "evidence": "e:1"}]}
    return "prose\n```json\n" + json.dumps(obj) + "\n```\n"


def test_category_near_misses_are_accepted_and_unknown_ones_named():
    assert review_result(review_text("requirements"))["findings"][0]["category"] == "requirement"
    assert review_result(review_text("Acceptance"))["findings"][0]["category"] == "requirement"
    with pytest.raises(ValueError, match="offending finding.*vibes"):
        review_result(review_text("vibes"))


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


def test_requeue_salvages_the_readable_verdict_on_disk_instead_of_paying_for_a_review(layout, monkeypatch):
    sup = blocked_by_unreadable_reviews(layout)
    sup.request_owner_decisions()
    item = decision_items(layout)[0]
    inbox.answer(layout, item["id"], "REQUEUE")
    sup.request_owner_decisions()                                               # applies the answer
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.WAITING_FOR_REVIEW
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


def test_nothing_to_salvage_dispatches_a_review_as_before(layout, monkeypatch):
    make_task(layout, "U", reviewer=["code_review"])
    tasks.set_status(layout, "U", "IN_PROGRESS")
    tasks.record_delivery(layout, "U", DIGEST, actor="test", paths=["U.txt"])
    tasks.record_verification(layout, "U", DIGEST, True, detail="ok")
    tasks.advance(layout, "U")
    sup = Supervisor(layout, sleep_s=0)
    dispatched = []
    monkeypatch.setattr(review, "dispatch", lambda *a, **k: dispatched.append(a) or {"attempt_id": "x"})
    sup.dispatch_reviews()
    assert len(dispatched) == 1
