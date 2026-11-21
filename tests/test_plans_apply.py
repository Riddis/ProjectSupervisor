"""supervisor/plans.py's applier (RECOVERY_PLAN.md section 2.6, step 2 of the order of work): the plan is its own
journal, apply is ordered, idempotent and resumable, resume is always the last operation, and a plan that cannot
finish stops visibly instead of half-applying. No test here touches the validator's own refusal codes (test_plans.py
owns those); every plan here is otherwise clean and exercises the fixed operation order: create, dependency edits,
contract patches, set_job, resume."""
from __future__ import annotations

import pytest

from supervisor import inbox as inbox_mod
from supervisor import plans, store
from supervisor import tasks as tasks_mod
from supervisor.models.task import TaskStatus

PLAN_ID = "T1.d1"
TEMPLATE_YAML = "version: 1\ntemplates:\n  noop:\n    adapter: process\n    may_be_set_by: [owner, plan]\n    params: {}\n"


def _write_template(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(TEMPLATE_YAML, encoding="utf-8")


def _setup(layout, rev=1):
    """T1 (the subject, BLOCKED) and a five-operation plan exercising every category of the fixed order, in that
    same order, so each operation's original index equals its category rank: 0 create_task, 1 add_dependency,
    2 patch_contract, 3 set_job (the task this same plan creates, its first job), 4 resume."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    _write_template(layout)
    ops = [
        {"op": "create_task", "task": {"id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer",
                                        "cost_category": "deterministic", "artifacts": ["x"], "tests": ["t"],
                                        "actions": ["a"], "acceptance": "ok"}},
        {"op": "add_dependency", "task": "T1", "depends_on": ["T2"]},
        {"op": "patch_contract", "task": "T1", "fields": {"tests": ["t", "t2"]}},
        {"op": "set_job", "task": "T2", "template": "noop", "params": {}},
        {"op": "resume", "task": "T1", "as": "ready"},
    ]
    return {"plan_schema": 1, "id": PLAN_ID, "subject": {"task": "T1"}, "reason": "repair this task",
            "expected_rev": {"T1": rev}, "operations": ops}


def test_propose_writes_refused_status_for_an_invalid_plan(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = {"plan_schema": 1, "id": "T1.bad", "subject": {"task": "T1"}, "reason": "x",
           "expected_rev": {"T1": 1}, "operations": [{"op": "bogus", "task": "T1"}]}
    result = plans.propose(layout, doc)
    assert result["status"] == "refused"
    assert any(r["code"] == "RF-01" for r in result["refusals"])
    assert plans.apply(layout, "T1.bad") == result             # terminal: a no-op, never half-applied


def test_apply_raises_for_an_unknown_plan(layout):
    with pytest.raises(plans.PlanError):
        plans.apply(layout, "nope")


def test_apply_twice_is_a_noop(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    first = plans.apply(layout, PLAN_ID)
    assert first["status"] == "applied"
    before = store.read(plans.path_for(layout, PLAN_ID))

    second = plans.apply(layout, PLAN_ID)
    after = store.read(plans.path_for(layout, PLAN_ID))
    assert second["status"] == "applied"
    assert before == after, "apply() on an already-applied plan rewrote the journal"


@pytest.mark.parametrize("op_name,op_index", [("create_task", 0), ("add_dependency", 1), ("patch_contract", 2),
                                              ("set_job", 3), ("resume", 4)])
def test_a_crash_between_every_pair_of_operations_leaves_the_subject_stopped_and_resumes(
        layout, monkeypatch, op_name, op_index):
    """A crash (here: the operation's own effect raising once) between the previous operation's journal write and
    this one's leaves `done` exactly where it was and the subject exactly where it already was — never half
    released. The next turn (a second call to apply()) finishes the plan from the journal."""
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS[op_name]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, op_name, flaky)

    first = plans.apply(layout, PLAN_ID)
    assert first["status"] == "applying"
    assert sorted(first["done"]) == list(range(op_index)), "an operation after the crash point was journalled"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED, "the subject moved on a half-applied plan"

    second = plans.apply(layout, PLAN_ID)
    assert second["status"] == "applied"
    assert sorted(second["done"]) == [0, 1, 2, 3, 4]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY, "resume did not run last turn finished it"
    assert tasks_mod.load(layout, "T2").id == "T2"


def test_stale_expected_rev_refuses_with_rf19_before_the_first_mutation(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    tasks_mod.add_note(layout, "T1", "an unrelated change", actor="owner")   # moves T1's rev under the plan
    result = plans.apply(layout, PLAN_ID)
    assert result["status"] == "refused"
    assert any(r["code"] == "RF-19" for r in result["refusals"])
    assert not (layout.tasks / "T2.yaml").exists(), "create_task ran despite the stale rev"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED


def test_stuck_after_max_resumes_opens_one_owner_item_and_rolls_back_nothing(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)

    def always_fails(layout_, op, plan_doc):
        raise RuntimeError("permanent failure")
    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)

    result = None
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        result = plans.apply(layout, PLAN_ID)

    assert result["status"] == "stuck"
    assert sorted(result["done"]) == [0, 1], "an operation past the failing one was journalled"
    open_owner_items = [i for i in inbox_mod.all_items(layout) if i.get("status") == "open" and i.get("for") == "owner"]
    assert len(open_owner_items) == 1
    item = open_owner_items[0]
    assert PLAN_ID in item["summary"]
    assert "patch_contract" in item["summary"]
    assert tasks_mod.load(layout, "T2").id == "T2"                      # the completed create_task stays applied
    assert "T2" in tasks_mod.load(layout, "T1").depends_on              # the completed add_dependency stays applied
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED    # resume never ran — nothing released

    # a second round of failing attempts does not open a second item (dedup_key)
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        plans.apply(layout, PLAN_ID)
    still_open = [i for i in inbox_mod.all_items(layout) if i.get("status") == "open" and i.get("for") == "owner"]
    assert len(still_open) == 1

    cancelled = plans.cancel_plan(layout, PLAN_ID)
    assert cancelled["status"] == "abandoned"
    assert sorted(cancelled["done"]) == [0, 1]
    assert tasks_mod.load(layout, "T2").id == "T2"
    assert "T2" in tasks_mod.load(layout, "T1").depends_on

    # cancel_plan is a no-op from here; apply() on an abandoned plan does nothing
    assert plans.apply(layout, PLAN_ID)["status"] == "abandoned"


def test_cancel_plan_refuses_off_a_plan_that_is_not_stuck(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    with pytest.raises(plans.PlanError):
        plans.cancel_plan(layout, PLAN_ID)


def test_replayed_propose_and_apply_applies_the_plan_once(layout):
    doc = _setup(layout)
    first_propose = plans.propose(layout, doc)
    replay_propose = plans.propose(layout, doc)
    assert replay_propose == first_propose, "a second propose() of the same id re-validated or rewrote the plan"

    applied = plans.apply(layout, PLAN_ID)
    assert applied["status"] == "applied"

    replay_propose_after = plans.propose(layout, doc)
    assert replay_propose_after["status"] == "applied"
    replay_apply = plans.apply(layout, PLAN_ID)
    assert replay_apply["status"] == "applied"

    t1 = tasks_mod.load(layout, "T1")
    assert t1.depends_on.count("T2") == 1
    assert t1.tests.count("t2") == 1
    assert t1.status == TaskStatus.READY


def test_resume_plans_sweep_continues_an_applying_plan(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS["resume"]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, "resume", flaky)

    plans.apply(layout, PLAN_ID)                                         # stops with resume still pending
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applying"

    acted = plans.resume_plans(layout)
    assert acted == [PLAN_ID]
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applied"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY


def test_resume_plans_leaves_a_stuck_plan_for_the_owner(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)

    def always_fails(layout_, op, plan_doc):
        raise RuntimeError("permanent failure")
    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        plans.apply(layout, PLAN_ID)
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "stuck"

    acted = plans.resume_plans(layout)
    assert acted == [], "the sweep auto-retried a stuck plan instead of leaving it for the owner"
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "stuck"
