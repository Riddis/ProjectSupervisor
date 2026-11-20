"""supervisor/plans.py — the recovery plan's document model, closed vocabulary and validator (RECOVERY_PLAN.md
step 1). One test per refusal code RF-01..RF-26, each isolated so it produces exactly that code; the three
normalisations; a valid plan; and the structural guarantee behind RF-04. No test here applies a plan — there is
no applier in this module."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from supervisor import plans, store


def codes(refusals):
    return [r["code"] for r in refusals]


def assert_only(refusals, code):
    assert refusals, f"expected a {code} refusal, got none"
    assert all(r["code"] == code for r in refusals), f"expected only {code}, got {codes(refusals)}"


def base_plan(subject="T1", rev=1, ops=()):
    return {"plan_schema": 1, "id": "T1.d1", "subject": {"task": subject}, "reason": "repair this task",
            "expected_rev": {subject: rev}, "operations": [*ops, {"op": "resume", "task": subject, "as": "ready"}]}


# ------------------------------------------------------------------ the envelope, the vocabulary, RF-01/RF-02/RF-26

def test_rf01_unknown_operation(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "reroute", "task": "T1"}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-01")


def test_rf01_unknown_plan_schema(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan()
    doc["plan_schema"] = 2
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-01")


def test_rf26_owner_only_effect_refused_not_rf01(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    for effect in ("apply_plan", "cancel_plan", "cancel_task", "hold", "release_hold", "diagnose_now"):
        doc = base_plan(ops=[{"op": effect, "task": "T1"}])
        _, refusals = plans.validate(doc, layout)
        assert_only(refusals, "RF-26")
        assert "RF-01" not in codes(refusals), f"{effect} must report RF-26, never RF-01"


def test_rf02_malformed_plan_missing_subject(layout):
    doc = {"plan_schema": 1, "id": "x", "operations": []}
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-02")


def test_rf02_malformed_plan_operations_not_a_list(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan()
    doc["operations"] = "not-a-list"
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-02")


def test_plan_with_one_unknown_op_is_refused_whole(layout):
    """Nothing is applied, reported or half-recorded: the other, otherwise-valid operation is never reflected
    in the report's graph diff once any operation is refused."""
    from tests.conftest import make_task
    make_task(layout, "T0", status="NOT_STARTED")
    make_task(layout, "T1", status="BLOCKED", deps=["T0"])
    doc = base_plan(ops=[{"op": "bogus", "task": "T1"},
                         {"op": "add_dependency", "task": "T0", "depends_on": []}])
    report, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-01")
    assert report.graph_diff == {}


# ------------------------------------------------------------------ RF-03, RF-04, RF-21 (I1 — never a command)

def test_rf03_command_shaped_value(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"review_note": "fix; rm -rf /"}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-03")


def test_rf04_job_without_template_structural():
    """RF-04 is a property of the document shape: constructing a `SetJobOp` carrying argv/fn/cmd/shell fails
    before any validator runs at all — there is no field for the value to land in."""
    with pytest.raises(ValidationError):
        plans.SetJobOp(op="set_job", task="T1", template="t", argv=["sh", "-c", "rm -rf /"])
    with pytest.raises(ValidationError):
        plans.SetJobOp(op="set_job", task="T1", template="t", cmd="rm -rf /")
    with pytest.raises(ValidationError):
        plans.SetJobOp(op="set_job", task="T1", template="t", fn="whatever")
    with pytest.raises(ValidationError):
        plans.SetJobOp(op="set_job", task="T1", template="t", shell="/bin/sh")
    plans.SetJobOp(op="set_job", task="T1", template="t", params={"n": 1})   # the declared shape still works


def test_rf04_job_without_template_refusal(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "t", "params": {},
                         "argv": ["sh", "-c", "rm -rf /"]}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-04")


def test_rf21_secret_shaped_value(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1",
                         "fields": {"review_note": "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-21")


# ------------------------------------------------------------------ jobs — the D-018 frontier: RF-05..RF-09, RF-11, RF-12

def _write_templates(layout, templates: dict):
    import yaml
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def test_rf05_template_unknown(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic"}},
                         {"op": "set_job", "task": "T2", "template": "ghost", "params": {}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-05")


def test_rf06_parameter_rejected(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["plan"],
                                     "params": {"mode": {"type": "enum", "values": ["a", "b"]}}}})
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic"}},
                         {"op": "set_job", "task": "T2", "template": "t1", "params": {"mode": "c"}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-06")


def test_rf07_path_outside_root(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["plan"],
                                     "params": {"script": {"type": "path", "roots": ["scripts"]}}}})
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic"}},
                         {"op": "set_job", "task": "T2", "template": "t1", "params": {"script": "../outside.txt"}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-07")


def test_rf08_preflight_failed(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["plan"],
                                     "executable": "no_such_executable_xyz123", "params": {}}})
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic"}},
                         {"op": "set_job", "task": "T2", "template": "t1", "params": {}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-08")


def test_rf09_template_not_plannable(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["owner"], "params": {}}})
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic"}},
                         {"op": "set_job", "task": "T2", "template": "t1", "params": {}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-09")


def test_rf11_job_identity_change_first_job(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")           # no existing job block at all
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["plan"], "params": {}}})
    doc = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "t1", "params": {}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-11")


def test_rf12_job_out_of_scope(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    make_task(layout, "T9", status="NOT_STARTED")        # unrelated: not the subject, not created
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["plan"], "params": {}}})
    doc = base_plan(ops=[{"op": "set_job", "task": "T9", "template": "t1", "params": {}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-12")


# ------------------------------------------------------------------ the graph and the contract: RF-10, RF-13..RF-18

def test_rf10_budget_spent_max_ops(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    ops = [{"op": "patch_contract", "task": "T1", "fields": {"review_note": f"note {i}"}} for i in range(9)]
    doc = base_plan(ops=ops)
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-10")


def test_rf13_out_of_scope(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    make_task(layout, "T9", status="NOT_STARTED")         # not the subject, not a neighbour, not created
    doc = base_plan(ops=[{"op": "add_dependency", "task": "T9", "depends_on": []}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-13")


def test_rf14_terminal_task(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="COMPLETE")
    make_task(layout, "T1", status="BLOCKED", deps=["T0"])      # T0 is now a direct neighbour, in scope
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "NEWT", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic"}},
                         {"op": "add_dependency", "task": "T0", "depends_on": ["NEWT"]}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-14")


def test_rf15_authority_field_on_patch_contract(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"title": "a new title"}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-15")


def test_rf15_authority_field_on_create_task(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "engineer", "cost_category": "deterministic",
                                                         "priority": 5}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-15")


def test_rf16_loosening_removes_a_test(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED", tests=["keep this"])
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"tests": []}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-16")


def test_rf17_cycle_or_unreleasable(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="NOT_STARTED")
    make_task(layout, "T1", status="BLOCKED", deps=["T0"])
    doc = base_plan(ops=[{"op": "add_dependency", "task": "T0", "depends_on": ["T1"]}])   # T0<->T1 cycle
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-17")


def test_rf18_contract_refused(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text("roles:\n  engineer:\n    active: true\n", encoding="utf-8")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                         "owner_role": "ghost_role", "cost_category": "deterministic"}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-18")


# ------------------------------------------------------------------ application: RF-19, RF-20, RF-22

def test_rf19_stale_inputs(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(rev=999)
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-19")


def test_rf20_subject_moved(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="COMPLETE")
    doc = base_plan()
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-20")


def test_rf22_subject_held(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED", held={"by": "owner", "at": store.now_iso(), "note": "wait"})
    doc = base_plan()
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-22")


# ------------------------------------------------------------------ RF-23, RF-24, RF-25 — pure functions, ahead of
# the approval lane (step 8) and the inbox's typed choices (step 7)

def test_rf23_self_approval():
    assert plans.self_approval_refusal("senior_pm", "senior_pm")["code"] == "RF-23"
    assert plans.self_approval_refusal("senior_pm", "architect") is None


def test_rf24_choice_not_offered():
    assert plans.choice_not_offered_refusal(["requeue", "cancel"], "apply_plan")["code"] == "RF-24"
    assert plans.choice_not_offered_refusal(["requeue", "cancel"], "requeue") is None


def test_rf25_choice_stale():
    assert plans.choice_stale_refusal("abc", "def")["code"] == "RF-25"
    assert plans.choice_stale_refusal("abc", "abc") is None


# ------------------------------------------------------------------ §1.3 normalisations: applied, reported, never
# counted as a refusal

def test_normalise_checks_to_tests(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1",
                         "fields": {"checks": ["this is prose, not a command"]}}])
    report, refusals = plans.validate(doc, layout)
    assert refusals == []
    assert [n["code"] for n in report.normalised] == ["checks_to_tests"]


def test_normalise_dedupe_dependencies(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="NOT_STARTED")
    make_task(layout, "T2", status="NOT_STARTED")
    make_task(layout, "T1", status="BLOCKED", deps=["T0"])
    doc = base_plan(ops=[{"op": "add_dependency", "task": "T1", "depends_on": ["T0", "T2", "T2"]}])
    report, refusals = plans.validate(doc, layout)
    assert refusals == []
    assert [n["code"] for n in report.normalised] == ["dedupe_dependencies"]
    assert report.graph_diff["edges_added"] == [["T1", "T2"]]


def test_normalise_drop_complete_waits(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="COMPLETE")
    make_task(layout, "T2", status="NOT_STARTED")
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "add_dependency", "task": "T1", "depends_on": ["T0", "T2"]}])
    report, refusals = plans.validate(doc, layout)
    assert refusals == []
    assert [n["code"] for n in report.normalised] == ["drop_complete_waits"]
    assert report.graph_diff["edges_added"] == [["T1", "T2"]]


# ------------------------------------------------------------------ a valid plan: empty refusals, computed scope,
# a graph diff

def test_valid_plan_validates_clean(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="NOT_STARTED")
    make_task(layout, "T1", status="BLOCKED", deps=["T0"])
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"tests": ["t", "and this"]}}])
    report, refusals = plans.validate(doc, layout)
    assert refusals == []
    assert report.scope == ["T0", "T1"]
    assert report.graph_diff["contract_patches"][0]["task"] == "T1"
