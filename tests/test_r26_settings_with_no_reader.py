"""R-26: thirteen settings a `project.yaml` or `routing.yaml` could carry with no reader anywhere, plus two read
settings with no documentation. Each name gets one verdict, held by one test: `description`, `repo.*`, `ui.title`
and `owner.name` are removed (nothing ever read them); `routing.yaml`'s own `version`, every role's `active_from`
and `second_opinion`, and `resources.<name>.single_job` are removed too (the "one job at a time" rule is
`limits.max_resource_jobs` plus a job's own `gpu` flag, untouched); `roles.<role>.result_kinds` -- documented as a
permission and, until this release, never enforced -- is implemented; `claude_api.eligible_roles` and
`job.api_credits`, already read, are documented. A document still carrying a removed key loads unchanged and
`doctor` warns; it never refuses."""
from __future__ import annotations

from pathlib import Path

import yaml

from supervisor import __version__ as FW_VERSION
from supervisor import doctor, gate, project
from supervisor import tasks as tasks_mod
from supervisor import testing
from tests.conftest import make_task

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_project_yaml(layout, doc: dict) -> None:
    doc.setdefault("framework_version", FW_VERSION)
    layout.project_file.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _write_routing(layout, doc: dict) -> None:
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


# ------------------------------------------------------------------ project.yaml: description, repo.*, ui.title,
# owner.name -- removed

def test_description_is_removed_nothing_reads_it_and_an_old_file_still_loads_with_a_warning(tmp_path):
    lay = testing.project(tmp_path)
    _write_project_yaml(lay, {"name": "proj", "description": "what this project is"})
    doc = project.load(lay)                              # loads, no exception
    assert "description" not in project.DEFAULTS
    check = doctor.check_project(lay)
    assert check.ok
    assert check.warn
    assert "description" in check.detail


def test_repo_block_is_removed_nothing_reads_any_of_its_three_keys(tmp_path):
    lay = testing.project(tmp_path)
    _write_project_yaml(lay, {"name": "proj", "repo": {"path": ".", "remote": "git@example.com:x/y.git",
                                                         "default_branch": "trunk"}})
    project.load(lay)                                     # loads, no exception
    assert "repo" not in project.DEFAULTS
    check = doctor.check_project(lay)
    assert check.ok and check.warn
    assert "repo" in check.detail


def test_ui_title_is_removed_every_page_title_is_fixed_text(tmp_path):
    lay = testing.project(tmp_path)
    _write_project_yaml(lay, {"name": "proj", "ui": {"port": 8790, "title": "My Console"}})
    project.load(lay)                                     # loads, no exception
    assert "title" not in project.DEFAULTS["ui"]
    check = doctor.check_project(lay)
    assert check.ok and check.warn
    assert "ui" in check.detail and "title" in check.detail


def test_owner_name_is_removed_every_gate_addresses_the_literal_role_id_owner(tmp_path):
    lay = testing.project(tmp_path)
    _write_project_yaml(lay, {"name": "proj", "owner": {"name": "ops"}})
    project.load(lay)                                      # loads, no exception
    assert "owner" not in project.DEFAULTS
    check = doctor.check_project(lay)
    assert check.ok and check.warn
    assert "owner" in check.detail


def test_an_old_project_yaml_carrying_every_removed_key_loads_with_a_warning_not_an_error(tmp_path):
    lay = testing.project(tmp_path)
    _write_project_yaml(lay, {
        "name": "proj", "description": "x",
        "repo": {"path": ".", "remote": None, "default_branch": "main"},
        "ui": {"port": 8790, "title": "x"}, "owner": {"name": "owner"},
    })
    doc = project.load(lay)
    assert doc.get("name") == "proj"                        # still loads; DEFAULTS still apply to what remains
    check = doctor.check_project(lay)
    assert check.ok is True                                 # a warning, never a refusal
    assert check.warn is True


# ------------------------------------------------------------------ routing.yaml: version, active_from,
# second_opinion, resources.<name>.single_job -- removed

def test_routing_version_is_removed(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"version": 1, "roles": {"engineer": {"active": True, "result_kinds": ["delivery"]}}})
    assert gate.routing_doc(lay)["version"] == 1            # the document still loads it back raw...
    removed = gate.removed_routing_settings(lay)
    assert any("version" in r for r in removed)             # ...but nothing else in the framework reads it
    check = doctor.check_routing(lay)
    assert check.ok and check.warn and "version" in check.detail


def test_routing_active_from_is_removed(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"engineer": {"active": True, "active_from": 2, "result_kinds": ["delivery"]}}})
    removed = gate.removed_routing_settings(lay)
    assert any("active_from" in r for r in removed)
    check = doctor.check_routing(lay)
    assert check.ok and check.warn and "active_from" in check.detail


def test_routing_second_opinion_is_removed(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"research_strategist": {"active": True, "second_opinion": "codex_cli",
                                                             "result_kinds": ["delivery"]}}})
    removed = gate.removed_routing_settings(lay)
    assert any("second_opinion" in r for r in removed)
    check = doctor.check_routing(lay)
    assert check.ok and check.warn and "second_opinion" in check.detail


def test_routing_resources_single_job_is_removed_the_slot_mechanism_is_untouched(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"resources": {"gpu": {"single_job": True, "usable_vram_floor_mb": 1000, "devices": [0]}}})
    from supervisor import needs as needs_mod
    # the section still loads, tolerantly, exactly as T8-14 (R-20) left it...
    assert needs_mod.resources_declared(lay)["gpu"]["single_job"] is True
    # ...but the framework names it a removed setting, not a read one
    removed = gate.removed_routing_settings(lay)
    assert any("single_job" in r for r in removed)
    check = doctor.check_routing(lay)
    assert check.ok and check.warn and "single_job" in check.detail


# ------------------------------------------------------------------ roles.<role>.result_kinds: implemented
# (enforced as a warning), the finding's own "looks like a permission" example

def test_result_kinds_is_enforced_for_an_owner_role_producing_an_unlisted_kind(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"engineer": {"active": True, "result_kinds": ["delivery"]}}})
    make_task(lay, "T1", owner_role="engineer", job={"result_kind": "assessment"})
    t = tasks_mod.load(lay, "T1")
    warnings = tasks_mod.contract_warnings([t], lay)
    assert any("T1" in w and "assessment" in w and "result_kinds" in w for w in warnings)


def test_result_kinds_is_enforced_for_a_reviewer_role_that_may_never_review(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {
        "engineer": {"active": True, "result_kinds": ["delivery"]},
        "specialist": {"active": True, "result_kinds": ["delivery"]},   # no "review" in its list
    }})
    make_task(lay, "T1", owner_role="engineer", reviewer=["specialist"])
    t = tasks_mod.load(lay, "T1")
    warnings = tasks_mod.contract_warnings([t], lay)
    assert any("T1" in w and "specialist" in w and "review" in w for w in warnings)


def test_result_kinds_absent_is_unrestricted_no_warning(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"engineer": {"active": True}}})     # no result_kinds key at all
    make_task(lay, "T1", owner_role="engineer", job={"result_kind": "assessment"})
    t = tasks_mod.load(lay, "T1")
    warnings = tasks_mod.contract_warnings([t], lay)
    assert not any("result_kinds" in w for w in warnings)


def test_result_kinds_within_the_list_is_silent(tmp_path):
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"engineer": {"active": True, "result_kinds": ["delivery", "assessment"]}}})
    make_task(lay, "T1", owner_role="engineer", job={"result_kind": "assessment"})
    t = tasks_mod.load(lay, "T1")
    warnings = tasks_mod.contract_warnings([t], lay)
    assert not any("result_kinds" in w for w in warnings)


def test_result_kinds_empty_list_is_not_checked_for_a_human_role(tmp_path):
    """The shipped template's `owner: {adapter: human, result_kinds: []}` idiom: a human-gate task never
    produces a result document, so an empty list must mean "not checked", not "may produce nothing" (R26-01)."""
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"owner": {"adapter": "human", "active": True, "result_kinds": []}}})
    make_task(lay, "T1", owner_role="owner")
    t = tasks_mod.load(lay, "T1")
    warnings = tasks_mod.contract_warnings([t], lay)
    assert not any("result_kinds" in w for w in warnings)


def test_result_kinds_empty_list_is_not_checked_for_a_non_document_job_kind(tmp_path):
    """The shipped template's `conversation: result_kinds: []` idiom: its own job always sets a non-document
    `result_kind` ("conversation"), so an empty list must not warn against it either."""
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"conversation": {"active": True, "result_kinds": []}}})
    make_task(lay, "T1", owner_role="conversation", job={"result_kind": "conversation"})
    t = tasks_mod.load(lay, "T1")
    warnings = tasks_mod.contract_warnings([t], lay)
    assert not any("result_kinds" in w for w in warnings)


# ------------------------------------------------------------------ roles.<role>.result_kinds on the plan path:
# a warning everywhere else becomes RF-18 there, same as any other contract_warnings line (R26-02) -- and an
# empty list, now "not checked", never turns into that refusal either

def test_result_kinds_violation_is_rf18_on_a_plan_create_task(tmp_path):
    from supervisor import plans
    lay = testing.project(tmp_path)
    # create_task carries no `job` block (not a creatable field), so its owner_role's expected kind is always the
    # default, "delivery" -- a role whose list excludes it is the violation a plan can actually produce.
    _write_routing(lay, {"roles": {"engineer": {"active": True, "result_kinds": ["review"]}}})
    make_task(lay, "T1", owner_role="engineer", status="BLOCKED")
    doc = {"plan_schema": 1, "id": "T1.d1", "subject": {"task": "T1"}, "reason": "repair this task",
           "expected_rev": {"T1": 1},
           "operations": [{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                           "owner_role": "engineer",
                                                           "cost_category": "deterministic"}},
                          {"op": "resume", "task": "T1", "as": "ready"}]}
    _, refusals = plans.validate(doc, lay)
    assert refusals and all(r["code"] == "RF-18" for r in refusals)
    assert any("result_kinds" in r["message"] for r in refusals)


def test_result_kinds_empty_list_is_never_an_rf18_refusal_on_a_plan_create_task(tmp_path):
    """The owner human-gate idiom must stay creatable through a plan: RF-18 exists to stop a plan from parking a
    task forever, not to refuse the shipped template's own inbox lane."""
    from supervisor import plans
    lay = testing.project(tmp_path)
    _write_routing(lay, {"roles": {"owner": {"adapter": "human", "active": True, "result_kinds": []},
                                    "engineer": {"active": True}}})
    make_task(lay, "T1", owner_role="engineer", status="BLOCKED")
    doc = {"plan_schema": 1, "id": "T1.d1", "subject": {"task": "T1"}, "reason": "repair this task",
           "expected_rev": {"T1": 1},
           "operations": [{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                           "owner_role": "owner", "cost_category": "human"}},
                          {"op": "resume", "task": "T1", "as": "ready"}]}
    _, refusals = plans.validate(doc, lay)
    assert not any(r["code"] == "RF-18" for r in refusals)


# ------------------------------------------------------------------ resources.<name>.single_job: the plugins that
# shipped from this repository must not go on documenting it as their GPU serialisation mechanism (R26-03)

def test_plugin_docstrings_name_the_real_gpu_serialisation_rule_not_single_job():
    """No package shipped from this repository may still document `single_job` as its GPU serialisation
    mechanism now that R-26 found no reader for it (R26-03): any `__init__.py` that mentions it at all must say
    in the same breath that `limits.max_resource_jobs` plus the job's own `gpu` flag is the real rule."""
    hits = 0
    for init_py in (REPO_ROOT / "packages").glob("*/*/__init__.py"):
        text = init_py.read_text(encoding="utf-8")
        if "single_job" not in text:
            continue
        hits += 1
        assert "limits.max_resource_jobs" in text, init_py
        assert "not read" in text, init_py
    assert hits >= 2


# ------------------------------------------------------------------ read, undocumented: claude_api.eligible_roles,
# job.api_credits -- documented with what the code does

def test_claude_api_eligible_roles_is_documented():
    text = (REPO_ROOT / "docs" / "runbooks" / "claude_api.md").read_text(encoding="utf-8")
    assert "eligible_roles" in text


def test_job_api_credits_is_documented():
    runbook = (REPO_ROOT / "docs" / "runbooks" / "claude_api.md").read_text(encoding="utf-8")
    contract = (REPO_ROOT / "docs" / "TASK_CONTRACT.md").read_text(encoding="utf-8")
    assert "job.api_credits" in runbook or "api_credits" in runbook
    assert "api_credits" in contract
