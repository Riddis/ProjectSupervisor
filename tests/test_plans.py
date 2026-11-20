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
    """The *key* is what RF-04 refuses, not what it holds: an `argv` naming nothing dangerous is refused all the
    same, so the refusal cannot be mistaken for the value scan of RF-03 (which this value deliberately passes)."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "t", "params": {}, "argv": ["noop"]}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-04")


def test_rf03_scans_an_operation_whose_shape_is_already_refused(layout):
    """The R0 tripwire runs on every string of every operation, including one the shape check has refused:
    a command-shaped value reaches the refusal list as RF-03 beside the shape's own code, never instead of it."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "t", "params": {},
                         "argv": ["sh", "-c", "rm -rf /"]}])
    _, refusals = plans.validate(doc, layout)
    assert "RF-04" in codes(refusals)
    assert "RF-03" in codes(refusals), "a malformed operation is still scanned for a command-shaped value"


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


# ------------------------------------------------------------------ R0 again, on the contract's own executable
# content: a check command is an executable the verifier re-runs, a `cwd` is where it runs and a test root is what
# bounds that `cwd`. None of the three is a value a plan may author, on either operation that writes a contract.

SECRET = "sk-ant-" + "a" * 24          # shaped like a credential, is none; built here so no literal is in the file


def _executable_check(layout):
    """A check entry that would pass every other gate: an allow-listed command prefix, no shell metacharacter,
    nothing removed, and a `cwd` that is nonetheless outside the project root."""
    outside = layout.root.anchor
    return {"command": "python -m pytest .", "cwd": outside + "somewhere"}


def test_rf03_patch_contract_may_not_author_a_check_command(layout):
    """The gap this closes: add-only monotonicity compares command strings only, so a plan could add a command
    (and rewrite the `cwd` and `timeout_s` of every existing check) and nothing screened it on this path."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1",
                         "fields": {"test_roots": [layout.root.anchor], "checks": [_executable_check(layout)]}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-03")
    fields = {r["field"] for r in refusals}
    assert fields == {"fields.checks", "fields.test_roots"}, fields


def test_rf03_create_task_may_not_author_a_check_command(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                        "owner_role": "engineer", "cost_category": "deterministic",
                                                        "checks": [_executable_check(layout)]}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-03")
    assert {r["field"] for r in refusals} == {"task.checks"}


def test_rf03_create_task_may_not_author_a_test_root(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                        "owner_role": "engineer", "cost_category": "deterministic",
                                                        "test_roots": ["tests"]}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-03")


def test_rf03_a_relative_in_project_check_is_refused_too(layout):
    """Not a containment rule with a hole in it: the refusal is that a plan authored an executable string at all,
    so a command that would have run inside the project root is refused exactly as one that would not."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1",
                         "fields": {"checks": [{"command": "python -m pytest tests", "cwd": "tests"}]}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-03")


def test_prose_in_checks_is_still_normalised_not_refused(layout):
    """The one recorded cause of a refused proposal keeps its repair: prose in `checks` moves to `tests` (§1.3)
    before any refusal is computed, so what the new refusal sees is only ever a command."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1",
                         "fields": {"checks": ["the report names each refusal code"]}}])
    report, refusals = plans.validate(doc, layout)
    assert refusals == []
    assert [n["code"] for n in report.normalised] == ["checks_to_tests"]


def test_create_task_may_not_carry_a_job_block(layout):
    """The other way a contract could name a program: `job` is not a contract field a plan may write (§2.2 — a
    job is a separate `set_job`, so the template rule lives in one place)."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                        "owner_role": "engineer", "cost_category": "deterministic",
                                                        "job": {"adapter": "process"}}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-02")


# ------------------------------------------------------------------ RF-03's path-separator carve-out, pinned on
# both sides: a contract's own path fields may hold a path; nothing else may grow one.

def test_rf03_contract_path_fields_may_hold_a_path(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED", artifacts=["x"])
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1",
                         "fields": {"artifacts": ["x", "docs/notes/summary.md"]}}])
    _, refusals = plans.validate(doc, layout)
    assert refusals == []


def test_rf03_a_path_outside_contract_content_is_refused(layout):
    from tests.conftest import make_task
    make_task(layout, "T0", status="NOT_STARTED")
    make_task(layout, "T1", status="BLOCKED", deps=["T0"])
    doc = base_plan(ops=[{"op": "add_dependency", "task": "T1", "depends_on": ["../elsewhere/T0"]}])
    _, refusals = plans.validate(doc, layout)
    assert "RF-03" in codes(refusals)
    assert [r["field"] for r in refusals if r["code"] == "RF-03"] == ["depends_on[0]"]


# ------------------------------------------------------------------ a refusal names the field, never the value

def test_a_refusal_never_carries_the_value_it_refused(layout):
    """RF-21's rule, held on every path out of the validator: the shape check runs on an operation carrying a
    credential-shaped value in a key the schema does not have, and neither its message nor any other quotes it."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "t", "params": {}, "token": SECRET}])
    _, refusals = plans.validate(doc, layout)
    assert "RF-21" in codes(refusals), "the secret scan runs whatever the shape check decided"
    assert "RF-04" in codes(refusals)
    for r in refusals:
        assert SECRET not in str(r), r


def test_a_shape_refusal_names_the_field_not_the_value(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "t", "params": {}, "extra_key": "a-plain-value"}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-04")
    assert "extra_key" in refusals[0]["message"]
    assert "a-plain-value" not in refusals[0]["message"], refusals[0]["message"]


def test_a_contract_refusal_never_carries_the_value_it_refused(layout):
    """The same rule on the other message built from an exception: a created task whose contract does not
    validate at all, with a credential-shaped value in one of its fields."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                        "owner_role": "engineer", "cost_category": "deterministic",
                                                        "evidence": SECRET}}])
    _, refusals = plans.validate(doc, layout)
    assert refusals, "an unknown evidence level is refused"
    for r in refusals:
        assert SECRET not in str(r), r


# ------------------------------------------------------------------ RF-18 on the patch path; RF-19 on a plan that
# declares no rev at all; the whole refusal list beside an owner-only effect

def test_rf18_contract_refused_on_patch_contract(layout):
    """RF-18 screened a created task's contract and not a patched one, although `patch_contract` may add a
    `reviewer` — a role routing does not configure parks the task exactly as it would a new one."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text("roles:\n  engineer:\n    active: true\n", encoding="utf-8")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"reviewer": ["ghost_role"]}}])
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-18")


def test_rf18_a_warning_the_task_already_carried_is_not_a_refusal(layout):
    """Only what the patch adds: a task already waiting on an unconfigured role is not made unpatchable by it."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED", owner_role="ghost_role")
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text("roles:\n  engineer:\n    active: true\n", encoding="utf-8")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"review_note": "look at the log order"}}])
    _, refusals = plans.validate(doc, layout)
    assert refusals == []


def test_rf19_expected_rev_must_name_the_subject(layout):
    """A plan that simply omits `expected_rev` got no staleness check at all: the subject's rev is the one every
    plan reads and writes, so a plan that does not state it is stale by construction."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan()
    doc.pop("expected_rev")
    _, refusals = plans.validate(doc, layout)
    assert_only(refusals, "RF-19")


def test_owner_only_effect_does_not_truncate_the_refusal_list(layout):
    """§2.5 grants one correction turn on the refusal list, so the list is whole: an owner-only effect reports
    RF-26 and still reports every plan-level refusal the same document trips."""
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED", held={"by": "owner", "at": store.now_iso(), "note": "wait"})
    doc = base_plan(ops=[{"op": "hold", "task": "T1"}])
    _, refusals = plans.validate(doc, layout)
    assert "RF-26" in codes(refusals)
    assert "RF-22" in codes(refusals), "the held subject is reported too, not suppressed by the owner-only effect"
