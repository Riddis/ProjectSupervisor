"""The release's own acceptance for the recovery plan's recorded repairs: three repairs that were once applied by
a human's own hand now reach the same end state through `supervisor.plans` alone, on neutral fixtures, with no
manual import and no owner item.

Each test below replays one of the three recorded cases and is named after it:

- a dependency inversion, which needs the wrong edge *removed* from a neighbour and the right one added on the
  subject -- the reason the validator's own scope rule reaches a direct neighbour at all, not only the subject
  and whatever the plan creates;
- a proposal refused for prose in `checks`, which the `checks_to_tests` normalisation and the one correction turn
  the validator grants exist to resolve without ever reaching the owner;
- an agent-shaped task split into a harness, a process run and a report -- three creations plus a `set_job` from a
  template on one of them, on a lane deterministic from the subject's own agent lane.

Each assertion is on the *end state* -- every involved task's final status, the edges, the contract fields, the
plan's own authority class, how many approvals were taken, and that no owner item ever opened -- never on the path
taken to get there. No fixture, task id or assertion here names a project, a task, an issue or a decision id."""
from __future__ import annotations

import sys

import yaml

from supervisor import inbox as inbox_mod
from supervisor import plans, store
from supervisor import tasks as tasks_mod
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

PY = sys.executable


def _write_routing(layout, text):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(text, encoding="utf-8")


def _write_templates(layout, templates: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


# ======================================================================= replay 1: the dependency inversion

def test_replay_dependency_inversion_an_edge_removed_on_a_neighbour(layout):
    """The real repair edits the NEIGHBOUR, not the subject: the neighbour's own wrong edge onto the subject is
    removed, and the edge the subject needs is added on the subject itself. Only a direct-neighbour scope can
    reach the neighbour's edge at all, not only the subject and whatever the plan creates."""
    make_task(layout, "T1", status="BLOCKED", owned_paths=["x"], tests=["t1"])
    make_task(layout, "T0", status="BLOCKED", deps=["T1"], owned_paths=["x"], tests=["t0"])
    rev0 = tasks_mod.load(layout, "T0").rev
    rev1 = tasks_mod.load(layout, "T1").rev
    doc = {"plan_schema": 1, "id": "T1.d1", "subject": {"task": "T1"},
           "reason": "T0 was made to wait on T1 by mistake; T1 must wait on T0 instead",
           "expected_rev": {"T0": rev0, "T1": rev1},
           "operations": [
               {"op": "remove_dependency", "task": "T0", "depends_on": ["T1"]},
               {"op": "add_dependency", "task": "T1", "depends_on": ["T0"]},
               {"op": "resume", "task": "T1", "as": "blocked_on_dependencies"},
           ]}

    # the plan's own class: routine repair, needing no approval at all
    assert plans.authority(doc, {}) == plans.CLASS_A

    proposed = plans.propose(layout, doc, by="diagnosis")
    assert proposed["status"] == "awaiting_approval" and proposed["refusals"] == []

    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")

    # end state: the plan
    assert stored["status"] == "applied"
    assert not stored.get("plan_review"), "class A: zero approvals taken"
    assert not stored.get("owner_item")
    assert inbox_mod.all_items(layout) == [], "no owner item opened, start to finish"

    # end state: every task involved
    t0 = tasks_mod.load(layout, "T0")
    t1 = tasks_mod.load(layout, "T1")
    assert t1.status == TaskStatus.BLOCKED, "the subject now waits on T0, which has not finished"
    assert t0.status == TaskStatus.BLOCKED, "the neighbour's own status is untouched by the inversion"

    # end state: the edges
    assert t1.depends_on == ["T0"], "the subject now carries the edge the neighbour used to carry"
    assert t0.depends_on == [], "the wrong edge is gone from the neighbour"

    # end state: the contract fields -- nothing outside the two edges moved
    assert t0.tests == ["t0"] and t1.tests == ["t1"]
    assert t0.owned_paths == ["x"] and t1.owned_paths == ["x"]


# ======================================================================= replay 2: prose in `checks` is
# normalised, not refused

def test_replay_refused_prose_in_checks_is_normalised_and_applied(layout):
    """Every proposal refused for this one recorded cause carried a `checks` entry that was prose, not a command.
    The `checks_to_tests` normalisation moves it to `tests` before any refusal is computed, so the plan this test
    proposes never trips a refusal at all and never spends the one correction turn the mechanism exists to
    grant."""
    make_task(layout, "T1", status="BLOCKED", owned_paths=["src"], tests=["existing test command"])
    rev = tasks_mod.load(layout, "T1").rev
    doc = {"plan_schema": 1, "id": "T1.d1", "subject": {"task": "T1"},
           "reason": "the documented behaviour needs a test asserting it",
           "expected_rev": {"T1": rev},
           "operations": [
               {"op": "patch_contract", "task": "T1",
                "fields": {"checks": ["the documented behaviour parses correctly"]}},
               {"op": "resume", "task": "T1", "as": "ready"},
           ]}

    assert plans.authority(doc, {}) == plans.CLASS_A

    proposed = plans.propose(layout, doc, by="diagnosis")
    assert proposed["refusals"] == [], "prose in checks is normalised away before any refusal is computed"
    assert [n["code"] for n in proposed["report"]["normalised"]] == ["checks_to_tests"]
    assert proposed.get("revisions") == [], "the normalisation alone resolves it; no correction turn was spent"

    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")

    # end state: the plan
    assert stored["status"] == "applied"
    assert not stored.get("plan_review"), "class A: zero approvals taken"
    assert not stored.get("owner_item")
    assert inbox_mod.all_items(layout) == [], "no owner item opened, start to finish"

    # end state: the task
    t1 = tasks_mod.load(layout, "T1")
    assert t1.status == TaskStatus.READY

    # end state: the contract fields -- the prose never became a check, and the existing test is kept
    assert t1.checks == []
    assert t1.tests == ["existing test command", "the documented behaviour parses correctly"]


# ======================================================================= replay 2, on a task that already
# carries a real check -- the shape the fixture above never exercised, and the one P1-2 crashed `propose()` on

def test_replay_refused_prose_in_checks_beside_an_existing_check_keeps_the_real_check(layout):
    """The same recorded cause, on a task that already carries a real check: the `checks_to_tests` move must merge
    the task's existing checks back in rather than discard them, and `propose()` must write the document -- its
    `before` is the task's own `Check` model object, which `yaml.safe_dump` cannot write unserialised."""
    make_task(layout, "T1", status="BLOCKED", owned_paths=["src"], tests=["existing test command"],
              checks=[{"command": f"{PY} -c pass"}])
    rev = tasks_mod.load(layout, "T1").rev
    doc = {"plan_schema": 1, "id": "T1.d1", "subject": {"task": "T1"},
           "reason": "the documented behaviour needs a test asserting it",
           "expected_rev": {"T1": rev},
           "operations": [
               {"op": "patch_contract", "task": "T1",
                "fields": {"checks": ["the documented behaviour parses correctly"]}},
               {"op": "resume", "task": "T1", "as": "ready"},
           ]}

    assert plans.authority(doc, {}) == plans.CLASS_A

    proposed = plans.propose(layout, doc, by="diagnosis")
    assert proposed["refusals"] == [], "prose in checks is normalised away before any refusal is computed"
    assert [n["code"] for n in proposed["report"]["normalised"]] == ["checks_to_tests"]
    assert proposed.get("revisions") == [], "the normalisation alone resolves it; no correction turn was spent"

    acted = plans.resume_plans(layout)
    assert acted == ["T1.d1"]
    stored = plans.load_plan(layout, "T1.d1")

    # end state: the plan
    assert stored["status"] == "applied"
    assert not stored.get("plan_review"), "class A: zero approvals taken"
    assert not stored.get("owner_item")
    assert inbox_mod.all_items(layout) == [], "no owner item opened, start to finish"

    # end state: the task
    t1 = tasks_mod.load(layout, "T1")
    assert t1.status == TaskStatus.READY

    # end state: the contract fields -- the real check survives the move; only the prose becomes a test
    assert [c.command for c in t1.checks] == [f"{PY} -c pass"], "the real check was dropped by the move"
    assert t1.tests == ["existing test command", "the documented behaviour parses correctly"]


# ======================================================================= replay 3: the agent-shaped task split

def test_replay_agent_shaped_task_split_into_harness_process_and_report(layout):
    """An agent-routed subject's own repair is not "make the subject itself run something else" (RF-11 refuses
    that); it is three creations -- a harness, a process run and a report -- plus a `set_job` from a template on
    one of them, on a deterministic lane distinct from the subject's own agent lane. Creating work on an unmetered
    lane is class B: the plan waits for exactly one approval session, and never reaches the owner as prose."""
    _write_routing(layout, "plan:\n  approver: [chief_architect, senior_pm]\n  max_creates: 3\n")
    _write_templates(layout, {"harness_check": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                                "may_be_set_by": ["plan"], "params": {}}})
    make_task(layout, "T1", status="BLOCKED", owned_paths=["x"])        # agent-routed: cost_category "claude"
    rev = tasks_mod.load(layout, "T1").rev

    def created(tid, title):
        return {"op": "create_task", "task": {"id": tid, "title": title, "objective": "o",
                                               "owner_role": "engineer", "cost_category": "deterministic",
                                               "artifacts": ["x"], "tests": ["t"], "actions": ["a"],
                                               "acceptance": "ok"}}

    doc = {"plan_schema": 1, "id": "T1.d1", "subject": {"task": "T1"},
           "reason": "T1 is agent-shaped; split it into a harness, a process run and a report",
           "expected_rev": {"T1": rev},
           "operations": [
               created("T2", "harness"), created("T3", "process run"), created("T4", "report"),
               {"op": "add_dependency", "task": "T1", "depends_on": ["T2", "T3", "T4"]},
               {"op": "set_job", "task": "T2", "template": "harness_check", "params": {}},
               {"op": "resume", "task": "T1", "as": "blocked_on_dependencies"},
           ]}

    # the plan's own class: shaping work, on an unmetered lane -- one approval session, never the owner
    assert plans.authority(doc, {}) == plans.CLASS_B

    proposed = plans.propose(layout, doc, by="diagnosis", proposer_role="senior_pm")
    assert proposed["status"] == "awaiting_approval" and proposed["refusals"] == []

    # open_plan_review/apply_plan_review (the plan_review dispatch's own mechanics, exercised directly: nothing
    # in this tree submits the job it mints yet -- P1-3 -- so `resume_plans()` itself no longer routes a class B
    # plan here; it reaches the owner instead, exactly as docs/releases/2.9.0/P1-3-R1-class-b-approval-lane.md
    # records, and that path is covered in tests/test_plan_authority.py)
    stored = plans.open_plan_review(layout, "T1.d1")
    assert stored["status"] == "awaiting_approval", "class B is never auto-applied"
    review = stored["plan_review"]
    assert review["role"] == "chief_architect" and review["role"] != "senior_pm", "the approver is never the proposer"
    assert not stored.get("owner_item"), "class B never reaches the owner as prose"
    assert inbox_mod.all_items(layout) == []
    for tid in ("T2", "T3", "T4"):
        assert not (layout.tasks / f"{tid}.yaml").exists(), "nothing is created before the one approval"

    # the one approval
    applied = plans.apply_plan_review(layout, "T1.d1", "APPROVE", review["role"])
    assert applied["status"] == "applied"

    # idempotent: a repeat verdict on an already-applied plan changes nothing -- exactly one approval was
    # ever acted on, whatever is called afterwards
    again = plans.apply_plan_review(layout, "T1.d1", "APPROVE", review["role"])
    assert again == applied

    # end state: the plan
    assert not applied.get("owner_item")
    assert inbox_mod.all_items(layout) == [], "still no owner item, start to finish"

    # end state: every task involved
    t1 = tasks_mod.load(layout, "T1")
    t2 = tasks_mod.load(layout, "T2")
    t3 = tasks_mod.load(layout, "T3")
    t4 = tasks_mod.load(layout, "T4")
    assert t1.status == TaskStatus.BLOCKED, "waiting on its new dependencies, none of which are done"
    assert t2.status == TaskStatus.NOT_STARTED and t3.status == TaskStatus.NOT_STARTED \
        and t4.status == TaskStatus.NOT_STARTED

    # end state: the edges
    assert t1.depends_on == ["T2", "T3", "T4"]

    # end state: the contract fields -- the created tasks' own lane, distinct from the subject's agent lane
    assert t1.cost_category.value == "claude"
    assert t2.cost_category.value == "deterministic" and t3.cost_category.value == "deterministic" \
        and t4.cost_category.value == "deterministic"
    assert t2.job is not None and t2.job.template == "harness_check" and t2.job.adapter == "process"
    assert t3.job is None and t4.job is None, "the split's job goes on the harness alone"
