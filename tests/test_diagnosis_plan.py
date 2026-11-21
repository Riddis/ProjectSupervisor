"""A diagnosis `defer` proposes a plan (RECOVERY_PLAN.md section 2.5, D-027 decision 4, order-of-work step 9).

Before this: a defer named `new_task`/`wait_for`, a validation error went straight to the owner with no chance to
correct it, and every refused proposal in the records died of one correctable cause (prose in `checks`). Now a
defer carries a `plan` (the six-operation document `supervisor.plans` validates): normalised deterministically,
corrected exactly once in a fresh session of the SAME diagnosis job (`cause: continuation`, no new cause value),
then escalated to the owner with both versions and both refusal lists from the one plan document if it is still
refused — never with `apply_plan` among the owner's choices, since a twice-refused plan is refused whole."""
import json

import pytest

from supervisor import diagnosis, escalation, inbox, jobs, plans, store, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def proposal(**fields):
    doc = {"reason": "the parser's own test needs declaring", "operations": [
        {"op": "patch_contract", "task": "T", "fields": {"checks": ["the documented input parses correctly"]}},
        {"op": "resume", "task": "T", "as": "ready"}]}
    doc.update(fields)
    return doc


def out_of_scope_proposal():
    return {"reason": "repair the neighbour instead", "operations": [
        {"op": "patch_contract", "task": "NOPE", "fields": {"tests": ["x"]}},
        {"op": "resume", "task": "T", "as": "ready"}]}


def stopped(layout, tid="T", **fields):
    make_task(layout, tid, priority=5, **fields)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.set_status(layout, tid, "BLOCKED", actor="escalation", note="retries stopped")


def diag_rec(layout, tid="T", attempt="T.a9", level="manager", role="senior_pm", **extra):
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "claude_cli", "phase": "RUNNING",
                               "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": level, "diag_role": role,
                               "diag_signature": "s", "diag_issue": None, **extra})


def defer(layout, rec=None, **plan_obj):
    rec = rec or diag_rec(layout)
    raw = {"kind": "diagnosis", "schema_version": 1, "action": "defer", "summary": "a defect in delivered code",
           "blocked_on": "the parser must accept the documented input", "plan": proposal(**plan_obj)}
    return diagnosis.ingest(Supervisor(layout, sleep_s=0), rec, Result(status="COMPLETE", raw=raw))


def diag_records(layout, tid="T"):
    return [r for r in jobs.for_task(layout, tid) if (r.get("spec") or {}).get("kind") == "diagnosis"]


def events(layout):
    return [json.loads(e)["event"] for e in layout.log.read_text().splitlines()]


def test_a_normalised_proposal_applies_with_no_owner_item(layout):
    stopped(layout)
    out = defer(layout)
    assert out["action"] == "defer"
    plan_id = out["plan"]
    doc = plans.load_plan(layout, plan_id)
    assert doc is not None and doc["status"] == "awaiting_approval"
    assert [n["code"] for n in doc["report"]["normalised"]] == ["checks_to_tests"]
    assert doc["refusals"] == [] and doc.get("revisions") == []
    plans.resume_plans(layout)                      # the loop's own next turn
    doc = plans.load_plan(layout, plan_id)
    assert doc["status"] == "applied"
    assert inbox.open_items(layout, "owner") == []
    assert tasks.load(layout, "T").status == TaskStatus.READY
    assert escalation.current_grant(layout, "T") == 1


def test_a_refused_proposal_gets_exactly_one_more_session_of_the_same_job(layout):
    stopped(layout)
    original = diag_rec(layout)
    out = defer(layout, rec=original, **out_of_scope_proposal())
    assert out["action"] == "defer" and out["plan"] == "T.a9"
    doc = plans.load_plan(layout, "T.a9")
    assert doc["status"] == "refused" and doc["refusals"] and doc.get("revisions") == []
    assert inbox.open_items(layout, "owner") == []          # not escalated yet: one more session is due

    recs = diag_records(layout)
    assert len(recs) == 2, "exactly one continuation session of the same job"
    continuation = next(r for r in recs if r["attempt_id"] != "T.a9")
    assert continuation["cause"] == "continuation" and continuation["parent"] == "T.a9"
    assert continuation["diag_plan_id"] == "T.a9"
    seen_causes = {r.get("cause") for r in jobs.for_task(layout, "T")}
    assert seen_causes <= {None, "diagnose", "continuation"}     # no new cause value anywhere in the tree


def test_the_correction_sessions_own_brief_carries_only_the_frozen_evidence_the_refused_plan_and_its_refusals(layout):
    stopped(layout)
    original = diag_rec(layout, diag_source="verification", diag_detail="the parser rejected valid input")
    defer(layout, rec=original, **out_of_scope_proposal())
    recs = diag_records(layout)
    continuation = next(r for r in recs if r["attempt_id"] != "T.a9")
    doc = plans.load_plan(layout, "T.a9")
    task = tasks.load(layout, "T")
    text = diagnosis._correction_brief(layout, task, "senior_pm", "verification",
                                       "the parser rejected valid input", doc)
    assert "the parser rejected valid input" in text            # the frozen original evidence
    assert "NOPE" in text and "repair the neighbour instead" in text   # the refused plan, verbatim
    assert "RF-13" in text and "op 0" not in text                # the refusal list: this is field-free prose, not a literal op index marker
    assert str(doc["refusals"][0]["op_index"]) in text and doc["refusals"][0]["field"] in text
    assert "## Attempts" not in text and "## Notes on the task" not in text   # nothing brief() would add
    assert continuation is not None


def test_a_corrected_plan_validates_and_proceeds(layout):
    stopped(layout)
    original = diag_rec(layout)
    defer(layout, rec=original, **out_of_scope_proposal())
    recs = diag_records(layout)
    continuation = next(r for r in recs if r["attempt_id"] != "T.a9")
    out = defer(layout, rec=continuation)                        # the correction turn: a valid plan this time
    assert out["action"] == "defer" and out["plan"] == "T.a9"
    doc = plans.load_plan(layout, "T.a9")
    assert doc["status"] == "awaiting_approval" and doc["refusals"] == []
    assert len(doc["revisions"]) == 1 and doc["revisions"][0]["refusals"]
    plans.resume_plans(layout)
    assert plans.load_plan(layout, "T.a9")["status"] == "applied"
    assert tasks.load(layout, "T").status == TaskStatus.READY
    assert len(diag_records(layout)) == 2, "a second correction is never attempted"


def test_a_twice_refused_plan_reaches_the_owner_with_both_versions_and_never_offers_apply_plan(layout):
    stopped(layout)
    original = diag_rec(layout)
    defer(layout, rec=original, **out_of_scope_proposal())
    recs = diag_records(layout)
    continuation = next(r for r in recs if r["attempt_id"] != "T.a9")
    out = defer(layout, rec=continuation, **out_of_scope_proposal())   # corrected, but still refused
    assert out["action"] == "owner"
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1
    item = items[0]
    choice_ids = {c["id"] for c in (item.get("choices") or [])}
    assert "apply_plan" not in choice_ids
    assert choice_ids and choice_ids <= {"cancel_task", "hold", "diagnose_now"}
    text = item["summary"] + (item.get("detail") or "")
    assert "RF-13" in text
    doc = plans.load_plan(layout, "T.a9")
    assert doc["status"] == "refused" and len(doc["revisions"]) == 1
    assert doc["revisions"][0]["refusals"] and doc["refusals"]
    assert len(diag_records(layout)) == 2, "a second correction is never attempted"
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED


def test_cause_vocabulary_is_unchanged():
    """D-027 decision 4: the correction turn reuses `cause: continuation`; no new `cause` value is added anywhere
    in the tree. Pinned against every literal `cause=` this release's own source carries."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "supervisor"
    found = set()
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        found |= set(re.findall(r"""cause=["']([a-z_]+)["']""", text))
    assert found == {"diagnose", "revision", "scheduled", "continuation", "reroute", "reverify", "rereview"}


def test_release_plan_and_new_task_fields_have_no_reader_left():
    assert not hasattr(diagnosis, "release_plan")
    assert not hasattr(diagnosis, "_release")
    assert not hasattr(diagnosis, "NEW_TASK_FIELDS")
    assert '"plan":' in diagnosis.RESULT_CONTRACT
    assert "new_task" not in diagnosis.RESULT_CONTRACT and "wait_for" not in diagnosis.RESULT_CONTRACT


def test_a_held_task_declines_the_defer_and_proposes_no_plan(layout):
    stopped(layout)
    tasks.hold(layout, "T", "owner", "looking at it myself")
    out = defer(layout)
    assert out["action"] == "held"
    assert plans.load_plan(layout, "T.a9") is None
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.BLOCKED and t.held
    assert jobs.read(layout, "T.a9")["diag_action"] == "held"
