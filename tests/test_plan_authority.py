"""supervisor/plans.py's authority classes and the approval lane: `plans.authority()` classifies every validated plan into A, B or the owner class,
total over the closed vocabulary; `resume_plans()` routes a freshly validated plan by that class — class A on the
spot, class B and the owner class each behind exactly one typed `apply_plan` choice. Class B is designed for a
`plan_review` job instead (`open_plan_review`/`apply_plan_review`, tested below as standalone building blocks),
but nothing in this tree dispatches that job yet (P1-3), so `resume_plans()` does not route there until the
dispatch exists. The approver is never the proposer (RF-23)."""
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


def test_authority_raises_on_a_create_task_with_no_id_to_classify():
    """The docstring's own claim ("fires the assertion rather than falling through to a default class") is
    proved, not just asserted: a create_task op whose task carries no id must never silently resolve to class B,
    the metered check it would then skip."""
    doc = {"operations": [{"op": "create_task", "task": {"title": "no id"}}]}
    with pytest.raises(AssertionError):
        plans.authority(doc, {})


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
    """The load-bearing case: the default manager level and the default planning role are the
    same role, so a project configured with one manager role only must not approve its own proposal."""
    _write_routing(layout, "plan:\n  approver: [senior_pm]\n")
    role, reason = plans.approver_role(layout, "senior_pm")
    assert role is None
    assert "RF-23" in reason


def test_approver_role_skips_an_inactive_role(layout):
    _write_routing(layout, "plan:\n  approver: [chief_architect, senior_pm]\n"
                          "roles:\n  chief_architect:\n    active: false\n  senior_pm: {}\n")
    role, reason = plans.approver_role(layout, "engineer")
    assert role == "senior_pm"


def test_approver_role_empty_configuration(layout):
    _write_routing(layout, "plan: {}\n")
    role, reason = plans.approver_role(layout, "engineer")
    assert role is None
    assert "empty" in reason


def test_approver_role_skips_a_role_the_project_does_not_configure_at_all(layout):
    """Another load-bearing case: a project that configures `roles:` at all, and
    whose template-shipped `plan.approver` default names a manager role the project never configured (a single-
    manager project), must escalate to the owner with the reason — never resolve the absent role as if it were
    an ordinary, active approver (tasks.py's own contract_warnings treats the same absence as "the task would
    wait for it forever")."""
    _write_routing(layout, "plan:\n  approver: [chief_architect, senior_pm]\n"
                          "roles:\n  chief_architect: {}\n")
    role, reason = plans.approver_role(layout, "engineer")
    assert role == "chief_architect"
    # now the only role actually configured IS the proposer: nothing left standing
    role, reason = plans.approver_role(layout, "chief_architect")
    assert role is None
    assert "senior_pm" in reason
    assert "not configured" in reason


def test_approver_role_with_no_roles_section_at_all_still_resolves(layout):
    """The complement of the case above: a project that configures no `roles:` section at all skips the
    "is it configured" question entirely, same as tasks.py's own contract_warnings does — unchanged behaviour
    for every project that does not use the roles: section."""
    _write_routing(layout, APPROVER_ROUTING)
    role, reason = plans.approver_role(layout, "senior_pm")
    assert role == "chief_architect"
    assert reason is None


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


def test_open_plan_review_job_is_attempt_scoped_when_the_subject_names_an_attempt(layout):
    """§2.1's subject carries its own attempt (RF-19 already compares it with current_attempt); the plan_review
    job must land inside that same attempt's own jobs[], not task-scoped like a diagnosis raised before any
    delivery exists — task scope is reserved for a plan whose subject names no attempt at all."""
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    doc["subject"]["attempt"] = 1
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    plans.open_plan_review(layout, "T1.d1")
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["plan_review"]["job"] == "T1.a1.p1"
    task = tasks_mod.load(layout, "T1")
    entry = next(a for a in task.attempts if a.n == 1)
    assert any(j.get("id") == "T1.a1.p1" for j in entry.jobs), "the job row lands inside the attempt's own jobs[]"


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


def test_class_b_reaches_the_owner_as_one_typed_choice_until_the_dispatch_exists(layout):
    """P1-3: `open_plan_review` only ever mints a `plan_review` job id — nothing in this tree submits it to an
    adapter — so routing a class B plan there and stopping left it `awaiting_approval` forever with no job behind
    it and an empty inbox. Until that dispatch exists, `resume_plans()` routes class B through the same owner
    path the owner authority class and the no-approver escalation already use, never minting a dead
    `plan_review`."""
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
    assert not stored.get("plan_review"), "no dead job id is minted for a plan nothing will ever dispatch"
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1
    item = items[0]
    assert item["choices"][0]["id"] == "apply_plan"
    assert item["choices"][0]["effect"] == {"op": "apply_plan", "plan": "T1.d1"}
    assert "class B" in item["summary"], item["summary"]

    # idempotent: a second sweep (a retried turn) never opens a second item
    plans.resume_plans(layout)
    assert len(inbox_mod.open_items(layout, "owner")) == 1


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
    """`open_plan_review`'s own no-candidate branch (`approver_role()` resolving to `(None, reason)` because the
    only configured approver is the proposer, RF-23) — exercised directly, since `resume_plans()` no longer
    reaches `open_plan_review` for class B at all (every class B plan escalates to the owner regardless of
    `plan.approver`, until the dispatch exists) and so can no longer be the thing that proves this branch."""
    from tests.conftest import make_task
    _write_routing(layout, "plan:\n  approver: [senior_pm]\n")
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    stored = plans.open_plan_review(layout, "T1.d1")
    assert not stored.get("plan_review"), "no job is minted when no candidate qualifies"
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1
    assert items[0]["choices"][0]["id"] == "apply_plan"
    assert "RF-23" in items[0]["summary"], items[0]["summary"]


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


def test_a_hold_placed_after_validation_is_refused_before_any_approval_record_opens(layout):
    """Only a choice the underlying command would accept is ever offered. A hold placed
    between propose() (which found none) and the routing turn must still refuse with RF-22 here — never open an
    approver's plan_review job, or an owner's apply_plan button, that apply()'s own re-validation would refuse
    anyway."""
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    tasks_mod.hold(layout, "T1", "owner", note="wait")
    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["status"] == "refused"
    assert any(r["code"] == "RF-22" for r in stored["refusals"])
    assert not stored.get("plan_review")
    assert not stored.get("owner_item")
    assert inbox_mod.all_items(layout) == []


# ------------------------------------------------------------------ apply_plan_review(): the typed result.
# `resume_plans()` no longer routes a class B plan through `open_plan_review` (P1-3: nothing dispatches the job
# it mints) — these tests are about `apply_plan_review()`'s own handling of a verdict once a review job exists,
# so the fixture calls `open_plan_review()` directly rather than going through the sweep.

def _propose_class_b(layout, plan_id="T1.d1"):
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")], plan_id=plan_id)
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    plans.open_plan_review(layout, plan_id)
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


# ------------------------------------------------------------------ open_plan_review()'s own expected_rev write
# never silences a move it did not itself make (RF-19 stays live for class B, exactly as it already is for A)

def test_open_plan_review_absorbs_only_its_own_rev_bump_not_a_move_it_did_not_make(layout):
    """allocate_job's write bumps the subject's rev — open_plan_review must absorb exactly that bump and no
    more. A move the store already took independently, in the same window, must stay visible to apply()'s own
    re-validation (RF-19) instead of being quietly folded into "the rev open_plan_review itself produced"."""
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    recorded = plans.load_plan(layout, "T1.d1")["expected_rev"]["T1"]

    store.update(tasks_mod.path_for(layout, "T1"), lambda d: d)      # a move open_plan_review did not make
    assert tasks_mod.load(layout, "T1").rev != recorded

    plans.open_plan_review(layout, "T1.d1")
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["expected_rev"]["T1"] == recorded, "the independent move is never silently absorbed here"

    result = plans.apply_plan_review(layout, "T1.d1", "APPROVE", stored["plan_review"]["role"])
    assert result["status"] == "refused", "apply()'s own re-validation still catches the move RF-19 names"
    assert any(r["code"] == "RF-19" for r in result["refusals"])


# ------------------------------------------------------------------ `supervisor doctor`'s cheap complement:
# a plan.approver entry absent from roles: is worth a warning before a plan ever resolves it

def test_doctor_warns_when_plan_approver_names_a_role_routing_does_not_configure(layout):
    from supervisor import doctor as doctor_mod
    _write_routing(layout, "plan:\n  approver: [chief_architect, senior_pm]\n"
                          "roles:\n  chief_architect: {}\n")
    check = doctor_mod.check_routing(layout)
    assert check.ok
    assert check.warn
    assert "senior_pm" in check.detail


def test_doctor_does_not_warn_when_every_plan_approver_is_configured(layout):
    from supervisor import doctor as doctor_mod
    _write_routing(layout, APPROVER_ROUTING + "roles:\n  chief_architect: {}\n  senior_pm: {}\n")
    check = doctor_mod.check_routing(layout)
    assert check.ok
    assert not check.warn


# ------------------------------------------------------------------ P1-3: the class B
# approval lane is a dead end under the real loop -- `open_plan_review` mints a `plan_review` job id nothing ever
# dispatches, `resume_plans()` never revisits an `awaiting_approval` plan once that field is set, and a subject
# waiting on it reads `why_not_runnable`'s `resource_claimed` fallback ("nothing currently claims it"), a code
# that contradicts its own detail (P3-4).

CLASS_B_LOOP_ROUTING = ("plan:\n  approver: [chief_architect, senior_pm]\nroles:\n  chief_architect:\n"
                       "    adapter: claude_cli\n  senior_pm:\n    adapter: claude_cli\n  engineer:\n"
                       "    adapter: claude_cli\n")


def test_class_b_under_the_real_loop_reaches_an_actionable_state_within_bounded_turns(layout):
    """The review's own turn-by-turn case: a class B plan proposed under the real loop (`Supervisor.once()`,
    not `resume_plans()` called alone and not `apply_plan_review()` called by hand) must not merely sit
    `awaiting_approval` forever with a minted `plan_review` id nothing dispatches, `jobs.for_task` empty and the
    inbox empty. Within a bounded number of turns it must reach either a dispatched `plan_review` job with a
    real job record behind it, or an owner item -- something an operator can act on."""
    from supervisor import jobs as jobs_mod
    from supervisor.loop import Supervisor
    from tests.conftest import make_task
    _write_routing(layout, CLASS_B_LOOP_ROUTING)
    make_task(layout, "T1", status="BLOCKED", owned_paths=["x"])
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="senior_pm", proposer_role="senior_pm")
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(4):
        sup.once()
    stored = plans.load_plan(layout, "T1.d1")
    job = (stored.get("plan_review") or {}).get("job")
    job_live = bool(job) and any(r.get("attempt_id") == job for r in jobs_mod.for_task(layout, "T1"))
    owner_items = inbox_mod.open_items(layout, "owner")
    assert job_live or owner_items, (
        f"a class B plan must reach a dispatched plan_review job or an owner item within a bounded number of "
        f"turns under the real loop; stored={stored!r}, owner_items={owner_items!r}")


def test_awaiting_approval_plan_with_no_live_job_behind_it_is_noticed_exactly_once(layout):
    """An `awaiting_approval` plan whose `plan_review` names a job id with no record behind it at all -- a
    store written by an earlier version, or a job lost -- must be noticed by `resume_plans()` (a stall sweep)
    and reach the owner, exactly once however many sweeps run; it must never stay silently `awaiting_approval`
    just because the `plan_review` field is already set."""
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")

    def fn(d):
        d["status"] = "awaiting_approval"
        d["plan_review"] = {"job": "T1.p1", "role": "chief_architect"}
        return d
    store.update(plans.path_for(layout, "T1.d1"), fn)

    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"], "the sweep must notice and act on the orphaned plan_review"
    stored = plans.load_plan(layout, "T1.d1")
    items = inbox_mod.open_items(layout, "owner")
    assert len(items) == 1
    assert items[0]["choices"][0]["id"] == "apply_plan"
    assert stored.get("owner_item") == items[0]["id"]

    again = plans.resume_plans(layout)
    assert again == [], "nothing left to act on"
    assert len(inbox_mod.open_items(layout, "owner")) == 1, "never a second item on a repeat sweep"


def test_why_not_runnable_for_a_subject_waiting_on_a_plan_approval_names_the_real_reason(layout):
    """P3-4: a BLOCKED subject whose own plan sits `awaiting_approval` must not read `resource_claimed` /
    "nothing currently claims it; no diagnosis has resolved it yet" -- a code that contradicts its own detail,
    since a diagnosis plainly has resolved it (a plan). It must answer a code whose detail names the plan."""
    from supervisor.why_not_runnable import why_not_runnable
    from tests.conftest import make_task
    _write_routing(layout, APPROVER_ROUTING)
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(subject="T1", ops=[_create_op("T2")])
    plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    plans.resume_plans(layout)
    stored = plans.load_plan(layout, "T1.d1")
    assert stored["status"] == "awaiting_approval"
    task = tasks_mod.load(layout, "T1")
    why = why_not_runnable(task, {"T1": task}, layout)
    assert not (why["code"] == "resource_claimed" and "nothing currently claims it" in why["detail"]), why
    assert "T1.d1" in why["detail"], why
