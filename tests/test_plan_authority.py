"""supervisor/plans.py's authority classes and the approval lane (RECOVERY_PLAN.md section 2.7, D-027 decision 6,
step 8 of the order of work): `plans.authority()` classifies every validated plan into A, B or the owner class,
total over the closed vocabulary; `resume_plans()` routes a freshly validated plan by that class — class A on the
spot, class B behind exactly one `plan_review` job, the owner class behind exactly one typed choice; and the
approver is never the proposer (RF-23)."""
from __future__ import annotations

import pytest

from supervisor import inbox as inbox_mod
from supervisor import plans, store
from supervisor import tasks as tasks_mod

APPROVER_ROUTING = "plan:\n  approver: [chief_architect, senior_pm]\n"


def _write_routing(layout, text):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(text, encoding="utf-8")


def base_plan(subject="T1", rev=1, ops=(), plan_id="T1.d1"):
    return {"plan_schema": 1, "id": plan_id, "subject": {"task": subject}, "reason": "repair this task",
           "expected_rev": {subject: rev}, "operations": [*ops, {"op": "resume", "task": subject, "as": "ready"}]}


def _create_op(tid, cost_category="claude"):
    return {"op": "create_task", "task": {"id": tid, "title": tid, "objective": "o", "owner_role": "engineer",
                                          "cost_category": cost_category, "owned_paths": ["x"]}}


# ------------------------------------------------------------------ plans.authority(): total, highest-class

def test_authority_raises_on_an_unclassifiable_operation():
    doc = {"operations": [{"op": "reroute", "task": "T1"}]}
    with pytest.raises(AssertionError):
        plans.authority(doc, {})


def test_authority_raises_on_a_plan_with_no_operations():
    with pytest.raises(AssertionError):
        plans.authority({"operations": []}, {})


def test_authority_class_a_the_four_routine_operations():
    for op in ({"op": "add_dependency", "task": "T1", "depends_on": ["T0"]},
              {"op": "remove_dependency", "task": "T1", "depends_on": ["T0"]},
              {"op": "patch_contract", "task": "T1", "fields": {"tests": ["t"]}},
              {"op": "resume", "task": "T1", "as": "ready"}):
        assert plans.authority({"operations": [op]}, {}) == plans.CLASS_A


def test_authority_class_b_create_task_on_an_unmetered_lane():
    doc = {"operations": [_create_op("T2")]}
    assert plans.authority(doc, {}) == plans.CLASS_B


@pytest.mark.parametrize("category", ["paid", "human"])
def test_authority_owner_class_create_task_on_a_metered_lane(category):
    doc = {"operations": [_create_op("T2", cost_category=category)]}
    assert plans.authority(doc, {}) == plans.CLASS_OWNER


def test_authority_owner_class_via_configured_resource_category(layout):
    _write_routing(layout, "limits:\n  resource_cost_categories: [local]\n")
    doc = {"operations": [_create_op("T2", cost_category="local")]}
    assert plans.authority(doc, {}, layout) == plans.CLASS_OWNER
    assert plans.authority(doc, {}) == plans.CLASS_B, "without a layout only the built-in categories are metered"


def test_authority_set_job_on_an_existing_task_is_class_a():
    doc = {"operations": [{"op": "set_job", "task": "T1", "template": "t", "params": {}}]}
    assert plans.authority(doc, {}) == plans.CLASS_A


def test_authority_set_job_inherits_its_created_tasks_class():
    doc = {"operations": [_create_op("T2", cost_category="paid"),
                          {"op": "set_job", "task": "T2", "template": "t", "params": {}}]}
    assert plans.authority(doc, {}) == plans.CLASS_OWNER


def test_authority_is_the_highest_class_of_its_operations():
    doc = {"operations": [{"op": "add_dependency", "task": "T1", "depends_on": ["T0"]}, _create_op("T2")]}
    assert plans.authority(doc, {}) == plans.CLASS_B


def test_authority_tightening_raises_a_class_never_lowers_one(layout):
    _write_routing(layout, "plan:\n  authority: {add_dependency: owner, create_task: A}\n")
    doc_a = {"operations": [{"op": "add_dependency", "task": "T1", "depends_on": ["T0"]}]}
    assert plans.authority(doc_a, {}, layout) == plans.CLASS_OWNER, "a project may tighten a class"
    doc_b = {"operations": [_create_op("T2")]}
    assert plans.authority(doc_b, {}, layout) == plans.CLASS_B, "a project may never loosen the vocabulary's own class"


# ------------------------------------------------------------------ approver_role(): RF-23, never the proposer

def test_approver_role_skips_the_proposer(layout):
    _write_routing(layout, APPROVER_ROUTING)
    role, reason = plans.approver_role(layout, "senior_pm")
    assert role == "chief_architect"
    assert reason is None


def test_approver_role_escalates_with_a_reason_when_only_candidate_is_the_proposer(layout):
    """D-027 decision 6's load-bearing case: the default manager level and the default planning role are the
    same role, so a project configured with one manager role only must not approve its own proposal."""
    _write_routing(layout, "plan:\n  approver: [senior_pm]\n")
    role, reason = plans.approver_role(layout, "senior_pm")
    assert role is None
    assert "RF-23" in reason


def test_approver_role_skips_an_inactive_role(layout):
    _write_routing(layout, "plan:\n  approver: [chief_architect, senior_pm]\n"
                          "roles:\n  chief_architect:\n    active: false\n")
    role, reason = plans.approver_role(layout, "engineer")
    assert role == "senior_pm"


def test_approver_role_empty_configuration(layout):
    _write_routing(layout, "plan: {}\n")
    role, reason = plans.approver_role(layout, "engineer")
    assert role is None
    assert "empty" in reason


# ------------------------------------------------------------------ the plan_review job kind: additive to the
# existing id derivation (tasks.JobId / tasks.allocate_job)

def test_plan_review_job_kind_is_additive_to_the_existing_id_derivation(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    # every existing kind's derived id is unchanged
    assert tasks_mod.JobId(task_id="T1", kind="delivery", seq=1, attempt=1).id == "T1.a1.s1"
    assert tasks_mod.JobId(task_id="T1", kind="verify", seq=1, attempt=1).id == "T1.a1.v1"
    assert tasks_mod.JobId(task_id="T1", kind="review", seq=1, attempt=1).id == "T1.a1.r1"
    assert tasks_mod.JobId(task_id="T1", kind="diagnosis", seq=1).id == "T1.d1"
    # the new kind derives the same way, task-scoped like diagnosis
    assert tasks_mod.JobId(task_id="T1", kind="plan_review", seq=1).id == "T1.p1"
    job_id = tasks_mod.allocate_job(layout, "T1", "plan_review").id
    assert job_id == "T1.p1"
    second = tasks_mod.allocate_job(layout, "T1", "plan_review").id
    assert second == "T1.p2", "the counter still advances; nothing about an existing kind changed"


# ------------------------------------------------------------------ resume_plans() routes a freshly validated plan
# by its own class

def test_class_a_dependency_inversion_applies_with_no_approval_at_all(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="NOT_STARTED", owned_paths=["x"])
    make_task(layout, "T1", status="BLOCKED", deps=["T0"], owned_paths=["x"])
    doc = base_plan(subject="T1", ops=[{"op": "remove_dependency", "task": "T1", "depends_on": ["T0"]},
                                       {"op": "add_dependency", "task": "T0", "depends_on": ["T1"]}])
    plans.propose(layout, doc, by="diagnosis")
    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["status"] == "applied"
    assert not stored.get("plan_review")
    assert not stored.get("owner_item")
    assert inbox_mod.all_items(layout) == []


def test_class_b_waits_for_exactly_one_plan_review_job_never_the_proposer(layout):
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["status"] == "awaiting_approval", "a class B plan is never auto-applied"
    assert not (layout.tasks / "T2.yaml").exists()
    review = stored["plan_review"]
    assert review["role"] == "chief_architect"
    assert review["role"] != "senior_pm"
    assert review["job"] == "T1.p1"
    assert inbox_mod.all_items(layout) == [], "class B never reaches the owner as prose"

    # idempotent: a second sweep (a retried turn) never opens a second job
    plans.resume_plans(layout)
    again = plans.load_plan(layout, "T1.d1")
    assert again["plan_review"] == review


def test_owner_class_reaches_the_owner_as_one_typed_choice_not_prose(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2", cost_category="paid")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["status"] == "awaiting_approval"
    assert not stored.get("plan_review")
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1
    item = items[0]
    assert item["choices"][0]["id"] == "apply_plan"
    assert item["choices"][0]["effect"] == {"op": "apply_plan", "plan": "T1.d1"}
    assert "APPLY_PLAN" in item["allowed_responses"]

    plans.resume_plans(layout)
    assert len(inbox_mod.open_items(layout, "owner")) == 1, "never a second item on a repeat sweep"


def test_class_b_with_no_approver_available_escalates_to_the_owner_with_the_reason(layout):
    from tests.conftest import make_task
    _write_routing(layout, "plan:\n  approver: [senior_pm]\n")
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    plans.resume_plans(layout)
    stored = plans.load_plan(layout, "T1.d1")
    assert not stored.get("plan_review")
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1
    assert items[0]["choices"][0]["id"] == "apply_plan"
    assert items[0]["summary"]


def test_held_subject_is_refused_rf22_and_never_reaches_the_owner(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED", held={"by": "owner", "at": store.now_iso(), "note": "wait"})
    doc = base_plan(subject="T1", ops=[{"op": "patch_contract", "task": "T1", "fields": {"tests": ["t"]}}])
    result = plans.propose(layout, doc, by="diagnosis")
    assert result["status"] == "refused"
    assert any(r["code"] == "RF-22" for r in result["refusals"])
    acted = plans.resume_plans(layout)
    assert acted == []
    assert inbox_mod.all_items(layout) == []


# ------------------------------------------------------------------ apply_plan_review(): the typed result

def _propose_class_b(layout, plan_id="T1.d1"):
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")], plan_id=plan_id)
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    plans.resume_plans(layout)
    return plans.load_plan(layout, plan_id)


def test_plan_review_approve_applies_the_plan(layout):
    doc = _propose_class_b(layout)
    role = doc["plan_review"]["role"]
    result = plans.apply_plan_review(layout, doc["id"], "APPROVE", role)
    assert result["status"] == "applied"
    assert (layout.tasks / "T2.yaml").exists()


def test_plan_review_revise_refuses_with_findings_and_never_auto_applies(layout):
    doc = _propose_class_b(layout)
    role = doc["plan_review"]["role"]
    findings = [{"code": "RF-16", "op_index": 0, "field": "task.cost_category", "message": "no", "rule": "I2"}]
    result = plans.apply_plan_review(layout, doc["id"], "REVISE", role, findings=findings)
    assert result["status"] == "refused"
    assert result["refusals"] == findings
    assert not (layout.tasks / "T2.yaml").exists()


def test_plan_review_owner_opens_a_question_never_an_effect(layout):
    doc = _propose_class_b(layout)
    role = doc["plan_review"]["role"]
    result = plans.apply_plan_review(layout, doc["id"], "OWNER", role, question="is this safe?")
    assert result["status"] == "awaiting_approval"
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1
    assert "choices" not in items[0], "a plan_review question is prose, not the owner class's apply_plan choice"
    assert items[0]["free_text"] is True


def test_plan_review_refuses_a_verdict_from_a_role_this_plan_was_not_sent_to(layout):
    doc = _propose_class_b(layout)
    with pytest.raises(plans.PlanError):
        plans.apply_plan_review(layout, doc["id"], "APPROVE", "engineer")


def test_plan_review_refuses_self_approval_even_under_the_recorded_role(layout):
    """Defensive double-check: even if a verdict somehow arrived under the proposer's own role (a borrowed
    identity, a bug upstream), RF-23 refuses it the same way open_plan_review's own resolution would have."""
    doc = _propose_class_b(layout)

    def fn(d):
        d["plan_review"] = {**d["plan_review"], "role": "senior_pm"}
        return d
    store.update(plans.path_for(layout, doc["id"]), fn)
    with pytest.raises(plans.PlanError):
        plans.apply_plan_review(layout, doc["id"], "APPROVE", "senior_pm")
