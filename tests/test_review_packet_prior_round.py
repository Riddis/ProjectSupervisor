"""A round-two review packet carries round one's findings, the
producer's claim, the artifact-level diff and `review_note` — entirely from the store, never a run directory.
Round one's packet asks for every blocking finding. A review result that omits `findings[].id` still gets one.
The producer's per-finding response (`findings_response`, optional, additive) renders beside the matching
finding; `task.result` stays the one free-text fallback when a producer gives none."""
import json

from supervisor import jobs, packets, tasks
from supervisor._vendor.contract import review_result
from tests.conftest import make_task

DIGEST_1 = "1" * 64
DIGEST_2 = "2" * 64


def _review_text(message, finding_id=None, digest=DIGEST_1):
    finding = {"category": "correctness", "blocking": True, "message": message, "evidence": "out.py:1"}
    if finding_id is not None:
        finding["id"] = finding_id
    obj = {"kind": "review", "schema_version": 1, "verdict": "REVISE", "summary": "s", "reviewed": ["out.py"],
          "digest": digest, "findings": [finding]}
    return "```json\n" + json.dumps(obj) + "\n```"


def test_a_review_result_without_findings_id_still_gets_one_deterministically():
    obj1 = review_result(_review_text("the default is wrong"))
    obj2 = review_result(_review_text("the default is wrong"))
    fid = obj1["findings"][0]["id"]
    assert fid and fid == obj2["findings"][0]["id"]
    other = review_result(_review_text("the default is upside down"))
    assert other["findings"][0]["id"] != fid


def test_a_review_result_with_an_explicit_id_keeps_it():
    assert review_result(_review_text("m", finding_id="R-7"))["findings"][0]["id"] == "R-7"


def test_round_one_brief_asks_for_every_blocking_finding(layout):
    make_task(layout, "T1", reviewer=["code_review"])
    task = tasks.load(layout, "T1")
    brief = packets.review_brief(layout, task, DIGEST_1, ["out.py"], "code_review")
    assert "every blocking finding" in brief.lower()
    assert "## Round 1" in brief


def _set_up_round_one(layout):
    """Round one, entirely through the store: a delivery, its verification and a review carrying one finding —
    the review job record carries the frozen manifest and text snapshot review.dispatch() would have written.
    No run directory is ever created for this task."""
    make_task(layout, "T", reviewer=["code_review"], review_note="watch the edge cases")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    (layout.root / "out.py").write_text("value = 1\n", encoding="utf-8")
    tasks.record_delivery(layout, "T", DIGEST_1, actor="test", paths=["out.py"])
    tasks.record_verification(layout, "T", DIGEST_1, True, detail="ok")
    manifest = packets.frozen_inputs(layout, ["out.py"])
    jobs.write(layout, {"attempt_id": "T.a1.r1", "task_id": "T", "adapter": "claude_cli", "phase": "DONE",
                        "spec": {"kind": "review"}, "handle": None, "kind": "review", "attempt": 1,
                        "review_role": "code_review", "review_digest": DIGEST_1,
                        "review_frozen": manifest, "review_text_snapshot": {"out.py": "value = 1\n"}})
    finding = {"id": "R-1", "category": "correctness", "blocking": True,
              "message": "the default is wrong", "evidence": "out.py:1"}
    tasks.record_review(layout, "T", DIGEST_1, "code_review", "REVISE", vendor="claude", findings=[finding])

    def fn(doc):
        doc["result"] = "addressed R-1 by fixing the default; nothing else changed"
    tasks._update(layout, "T", fn)
    return finding


def test_round_two_packet_carries_round_one_and_the_diff_entirely_from_the_store(layout):
    finding = _set_up_round_one(layout)
    (layout.root / "out.py").write_text("value = 2\n", encoding="utf-8")   # the fix the revision made
    tasks.record_delivery(layout, "T", DIGEST_2, actor="test", paths=["out.py"])
    tasks.record_verification(layout, "T", DIGEST_2, True, detail="ok")
    task = tasks.load(layout, "T")
    assert not (layout.runs / "T").exists()        # nothing under runs/ exists for this task at all

    brief = packets.review_brief(layout, task, DIGEST_2, ["out.py"], "code_review")
    assert finding["id"] in brief and finding["message"] in brief and "REVISE" in brief
    assert "addressed R-1 by fixing the default" in brief            # the producer's own claim
    assert "watch the edge cases" in brief                           # review_note reaches the packet
    assert "out.py: changed" in brief                                # artifact-level change list
    assert "-value = 1" in brief and "+value = 2" in brief            # unified diff content
    assert "first review round" not in brief                        # this is round two, not round one

    ctx = packets.prior_round_context(layout, task, DIGEST_2, ["out.py"])
    assert ctx["rounds"][0]["attempt"] == 1
    assert ctx["rounds"][0]["reviews"][0]["findings"][0]["id"] == "R-1"
    assert ctx["rounds"][0]["reviews"][0]["verdict"] == "REVISE"
    assert ctx["claim"] == "addressed R-1 by fixing the default; nothing else changed"
    assert {"path": "out.py", "change": "changed"} in ctx["change_list"]
    assert "-value = 1" in ctx["diff_text"] and "+value = 2" in ctx["diff_text"]


def test_round_two_packet_renders_the_producers_response_beside_each_finding(layout):
    """What the producer claimed PER FINDING (addressed/disputed/deferred), not
    only the one undifferentiated paragraph `task.result` already carries — the optional `findings_response`
    delivery field, keyed by the prior round's own finding id, rendered next to that finding's own line.
    `task.result` stays the fallback: a producer that gives none still has its one free-text claim shown."""
    finding = _set_up_round_one(layout)
    (layout.root / "out.py").write_text("value = 2\n", encoding="utf-8")
    tasks.record_delivery(layout, "T", DIGEST_2, actor="test", paths=["out.py"],
                          findings_response=[{"id": finding["id"], "status": "addressed",
                                              "note": "fixed the default"}])
    task = tasks.load(layout, "T")

    brief = packets.review_brief(layout, task, DIGEST_2, ["out.py"], "code_review")
    finding_line = next(line for line in brief.splitlines() if finding["id"] in line)
    assert "producer: addressed (fixed the default)" in finding_line

    ctx = packets.prior_round_context(layout, task, DIGEST_2, ["out.py"])
    assert ctx["responses"][finding["id"]] == {"status": "addressed", "note": "fixed the default"}
