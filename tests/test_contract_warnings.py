"""1.13.0, a skill-trial finding (task-author, role-change): the contract checks a skill names must catch what it says
they catch. `supervisor task import --dry-run` (and a real import) and `supervisor check` accepted, in silence:
- an `owner_role` or a `reviewer` that config/routing.yaml does not know, which the task then waited on forever;
- a role that is inactive (`active: false`), which parks the task;
- a check command outside `verification.allowed_test_commands`, which fails only at verification, after the work.

They now warn about each, naming the task. Warnings, not refusals, so a project whose seed already holds one keeps
importing."""
from __future__ import annotations

import yaml
from typer.testing import CliRunner

from supervisor import tasks, testing
from supervisor.cli import app

ROUTING = {"roles": {"engineer": {"adapter": "python"}, "auditor": {"adapter": "python"},
                     "sleeper": {"adapter": "python", "active": False}},
           "verification": {"allowed_test_commands": ["python -m pytest"]}}


def contract(tid: str, **fields) -> dict:
    doc = {"id": tid, "phase": 0, "title": tid, "objective": "o", "owner_role": "engineer", "depends_on": [],
           "actions": ["a"], "artifacts": [f"{tid}.txt"], "tests": ["t"], "checks": [], "owned_paths": ["."],
           "reviewer": [], "acceptance": "a", "escalation": "e", "human_approval": "none",
           "cost_category": "deterministic", "job": {"adapter": "python", "fn": "noop"}}
    doc.update(fields)
    return doc


def project(tmp_path, *seed):
    lay = testing.project(tmp_path, routing=ROUTING)
    (lay.state / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": list(seed)}, sort_keys=False), encoding="utf-8")
    return lay


def warnings_of(lay) -> list[str]:
    return tasks.import_seed(lay, lay.state / "tasks_seed.yaml", dry_run=True).get("warnings", [])


def test_an_unknown_owner_or_reviewer_is_warned(tmp_path):
    lay = project(tmp_path, contract("T0-01", owner_role="nobody", reviewer=["ghost", "auditor"]))
    found = warnings_of(lay)
    assert any("T0-01" in w and "owner_role 'nobody'" in w for w in found), found
    assert any("T0-01" in w and "reviewer 'ghost'" in w for w in found), found
    assert not any("auditor" in w for w in found), found


def test_an_inactive_role_is_warned(tmp_path):
    found = warnings_of(project(tmp_path, contract("T0-01", owner_role="sleeper")))
    assert any("T0-01" in w and "sleeper" in w and "inactive" in w for w in found), found


def test_a_check_outside_the_allowed_list_is_warned(tmp_path):
    lay = project(tmp_path, contract("T0-01", checks=[{"command": "make test"}, {"command": "python -m pytest -q"}]))
    found = warnings_of(lay)
    assert any("T0-01" in w and "'make test'" in w and "allowed_test_commands" in w for w in found), found
    assert not any("pytest" in w for w in found), found


def test_a_sound_contract_has_no_warning(tmp_path):
    assert warnings_of(project(tmp_path, contract("T0-01", reviewer=["auditor"]))) == []


def test_supervisor_check_reports_them_and_still_passes(tmp_path, monkeypatch):
    lay = project(tmp_path, contract("T0-01", owner_role="nobody"), contract("T0-02", owner_role="sleeper"))
    tasks.import_seed(lay, lay.state / "tasks_seed.yaml")
    monkeypatch.setenv("SUPERVISOR_ROOT", str(lay.root))
    out = CliRunner().invoke(app, ["check"])
    assert out.exit_code == 0, out.output
    assert "WARN T0-01: owner_role 'nobody'" in out.output and "WARN T0-02" in out.output, out.output
