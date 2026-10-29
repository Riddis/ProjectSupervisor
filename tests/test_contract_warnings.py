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


EXT = '''
from supervisor.ext import Adapter

def tally(root, run_dir, **params):
    return {"status": "COMPLETE", "artifacts": [], "notes": ""}

class Lane(Adapter):
    name = "tally_lane"

def setup(registry):
    registry.python_job("tally", tally)
    registry.adapter(Lane())
'''


TALLY_MANIFEST = '''identity = "tally"
version = "1.0.0"
framework = ">=1.12"
[provides]
python_jobs = ["tally"]
adapters = ["tally_lane"]
'''


def test_a_job_naming_an_unregistered_lane_or_function_is_warned(tmp_path):
    """1.13.0, a skill-trial finding (extension-promote, round 2): nothing checked that a job's adapter or python
    function exists, so a task naming a lane or a job that no extension registers imported in silence and failed only
    when it ran ("unknown python job"). Names an extension's manifest declares count."""
    try:
        lay = testing.project(tmp_path, routing=ROUTING, modules={"tally": EXT},
                              files={"extensions/tally.toml": TALLY_MANIFEST})
        (lay.state / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": [
            contract("T0-01", job={"adapter": "nolane"}),
            contract("T0-02", job={"adapter": "python", "fn": "nojob"}),
            contract("T0-03", job={"adapter": "python", "fn": "tally"}),
            contract("T0-04", job={"adapter": "tally_lane"}),
            contract("T0-05", job={"adapter": "python"}),
        ]}, sort_keys=False), encoding="utf-8")
        found = warnings_of(lay)
        assert any("T0-01" in w and "'nolane'" in w for w in found), found
        assert any("T0-02" in w and "'nojob'" in w for w in found), found
        assert not any(w.startswith(("T0-03", "T0-04", "T0-05")) for w in found), found
    finally:
        testing.reset()


BROKEN = '''
def setup(registry):
    raise RuntimeError("probe failure")
'''

MARKS = '''
import pathlib

def tally(root, run_dir, **params):
    return {"status": "COMPLETE", "artifacts": [], "notes": ""}

def setup(registry):
    pathlib.Path(registry.layout.root, "setup-ran.txt").write_text("x", encoding="utf-8")
    registry.python_job("marks_tally", tally)
'''

MARKS_MANIFEST = '''identity = "marks"
version = "1.0.0"
framework = ">=1.12"
[provides]
python_jobs = ["marks_tally"]
'''


def snapshot(root) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}


def test_dry_run_and_check_write_nothing_even_when_an_extension_fails(tmp_path, monkeypatch):
    """Review 1.13.0 round 2 (P2): task-author and runtime-diagnose promise that `task import --dry-run` writes nothing
    and that `check` only reads, but both set the project's extensions up: a failed optional extension appended
    `extension_error` to state/log.jsonl, and setup is project code that may write anything. Both now read the
    project as it is: no extension set up, no folder created, every byte unchanged, and the warnings still shown."""
    try:
        lay = testing.project(tmp_path, routing=ROUTING, modules={"broken": BROKEN},
                              extensions=[{"name": "broken", "required": False}])
        (lay.state / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": [
            contract("T0-01", job={"adapter": "python", "fn": "missing"})]}, sort_keys=False), encoding="utf-8")
        monkeypatch.setenv("SUPERVISOR_ROOT", str(lay.root))
        before = snapshot(lay.root)
        out = CliRunner().invoke(app, ["task", "import", "--dry-run"])
        assert out.exit_code == 0, out.output
        assert "nothing written" in out.output and "'missing'" in out.output, out.output
        assert snapshot(lay.root) == before
        tasks.import_seed(lay, lay.state / "tasks_seed.yaml")
        testing.reset()
        before = snapshot(lay.root)
        out = CliRunner().invoke(app, ["check"])
        assert out.exit_code == 0, out.output
        assert "WARN T0-01: job fn 'missing'" in out.output, out.output
        assert snapshot(lay.root) == before
        out = CliRunner().invoke(app, ["supervisor"])                   # runtime-diagnose's other read-only step
        assert out.exit_code == 0, out.output
        assert snapshot(lay.root) == before
    finally:
        testing.reset()


def test_a_job_an_extension_manifest_declares_counts_without_running_setup(tmp_path, monkeypatch):
    """The names come from the built-ins and each extension's manifest (`provides`), which declares exactly what its
    setup() registers (G5): no extension code runs."""
    try:
        lay = testing.project(tmp_path, routing=ROUTING, modules={"marks": MARKS},
                              files={"extensions/marks.toml": MARKS_MANIFEST})
        (lay.state / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": [
            contract("T0-01", job={"adapter": "python", "fn": "marks_tally"}),
            contract("T0-02", job={"adapter": "python", "fn": "marks_other"})]}, sort_keys=False), encoding="utf-8")
        monkeypatch.setenv("SUPERVISOR_ROOT", str(lay.root))
        out = CliRunner().invoke(app, ["task", "import", "--dry-run"])
        assert out.exit_code == 0, out.output
        assert "'marks_other'" in out.output and "'marks_tally'" not in out.output, out.output
        assert not (lay.root / "setup-ran.txt").exists()
    finally:
        testing.reset()


def test_an_extension_without_a_manifest_is_named_in_the_warning(tmp_path):
    """An extension without a manifest (still loading in 1.12.x, D1) declares nothing that can be read without running
    it, so a warning says its names were not checked."""
    try:
        lay = project(tmp_path, contract("T0-01", job={"adapter": "python", "fn": "tally"}))
        testing.add_extensions(lay, {"tally": EXT})
        found = warnings_of(lay)
        assert any("T0-01" in w and "'tally'" in w and "not checked against tally" in w for w in found), found
    finally:
        testing.reset()
