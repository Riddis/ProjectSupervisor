"""The framework/project boundary: project.yaml discovery, `supervisor init`, extensions, doctor and migrate."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from supervisor import __version__, doctor, extensions, migrations, project, template
from supervisor.adapters import REGISTRY
from supervisor.cli import app
from supervisor.paths import Layout, find_root
from tests.conftest import TEMPLATE, make_task

runner = CliRunner()


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A project stamped from the template, with SUPERVISOR_ROOT unset so discovery is exercised."""
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    target = tmp_path / "demo"
    template.stamp(target, name="Demo Project")
    extensions.reset()
    return target


# ------------------------------------------------------------------ discovery and project.yaml

def test_find_root_walks_up_to_project_yaml(fresh, monkeypatch):
    deep = fresh / "docs" / "plan"
    monkeypatch.chdir(deep)
    assert find_root() == fresh.resolve()


def test_find_root_prefers_the_environment(fresh, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(fresh)
    monkeypatch.setenv("SUPERVISOR_ROOT", str(other))
    assert find_root() == other.resolve()


def test_find_root_is_none_outside_a_project(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_root() is None


def test_project_yaml_is_stamped_with_name_and_version(fresh):
    layout = Layout(fresh)
    doc = project.load(layout)
    assert doc["name"] == "Demo Project"
    assert doc["framework_version"] == __version__
    assert project.phase_label(layout, 0) == "Foundations"
    assert project.phase_label(layout, 7) == "Phase 7"
    assert project.ui_port(layout) == 8790
    assert project.version_mismatch(layout) is None


def test_project_defaults_apply_without_a_file(tmp_path):
    layout = Layout(tmp_path)
    assert project.name(layout) == "Untitled project"
    assert project.extensions(layout) == []


def test_template_contains_no_leftover_domain_or_history_references():
    """The template is what a stranger clones: nothing from the project it was extracted from may remain."""
    forbidden = ("persona", "comfy", "ollama", "influencer", "nudenet", "instagram", "AiInfluencer", "studio",
                 "D-015", "D-016", "T0-", "TowerDefense")
    hits = []
    for p in sorted(x for x in TEMPLATE.rglob("*") if x.is_file()):
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        hits += [f"{p.relative_to(TEMPLATE)}: {w}" for w in forbidden if w.lower() in text]
    assert not hits, hits


def test_framework_package_contains_no_leftover_history_references():
    forbidden = ("persona", "comfy", "ollama", "influencer", "nudenet", "instagram", "AiInfluencer", "D-015", "D-016",
                 "plan §", "review round")
    root = Path(__file__).resolve().parents[1] / "supervisor"
    hits = []
    for p in sorted(root.rglob("*.py")) + sorted(root.rglob("*.html")):
        if "template" in p.parts or "_vendor" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        hits += [f"{p.relative_to(root)}: {w}" for w in forbidden if w.lower() in text.lower()]
    assert not hits, hits


# ------------------------------------------------------------------ init

def test_init_stamps_every_template_file_and_the_runtime_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    target = tmp_path / "proj"
    r = runner.invoke(app, ["init", str(target), "--name", "My Thing"])
    assert r.exit_code == 0, r.output
    assert (target / "project.yaml").exists() and (target / ".gitignore").exists()
    assert (target / "state" / "tasks").is_dir() and (target / "state" / "runtime" / "jobs").is_dir()
    assert (target / "extensions" / "__init__.py").exists()
    assert "My Thing" in (target / "README.md").read_text(encoding="utf-8")
    assert "{{" not in (target / "project.yaml").read_text(encoding="utf-8")
    # every template file landed (dot-files renamed)
    stamped = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    for src in template.files():
        rel = src.relative_to(template.TEMPLATE_DIR).as_posix().replace("_dot_", ".")
        assert rel in stamped, rel


def test_init_refuses_to_overwrite_an_existing_project(fresh):
    r = runner.invoke(app, ["init", str(fresh)])
    assert r.exit_code != 0
    (fresh / "README.md").write_text("mine", encoding="utf-8")
    assert template.stamp(fresh, name="x") == []                     # nothing rewritten without --force
    assert (fresh / "README.md").read_text(encoding="utf-8") == "mine"


def test_a_stamped_project_passes_check_and_renders(fresh, monkeypatch):
    monkeypatch.chdir(fresh)
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["render"])
    assert r.exit_code == 0, r.output
    state = (fresh / "STATE.md").read_text(encoding="utf-8")
    assert "Demo Project" in state and "Foundations" in state


# ------------------------------------------------------------------ extensions

EXT = '''
from supervisor.doctor import Check
from supervisor.adapters.base import Adapter, Result
import typer

class EchoAdapter(Adapter):
    name = "echo_lane"
    gpu = True
    def prepare(self, layout, task, attempt_id):
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id}
    def submit(self, spec):
        return {"done": True}
    def alive(self, handle):
        return False
    def collect(self, spec, handle):
        return Result(status="COMPLETE", notes="echo")

cli = typer.Typer()

@cli.command("hello")
def hello():
    typer.echo("hello from the extension")

def setup(registry):
    registry.adapter(EchoAdapter())
    registry.doctor_check(lambda layout: Check("echo", True, "fine"))
    registry.overview_panel(lambda layout: {"title": "Echo", "rows": [("state", "ok")], "progress": (1, 2)})
    def projection(layout):
        (layout.root / "ECHO.md").write_text("# echo\\n")
        return layout.root / "ECHO.md"
    registry.projection(projection)
    registry.cli(cli, "echo")
    registry.python_job("echo_job", lambda root, run_dir, **p: {"status": "COMPLETE", "artifacts": [], "notes": "ran"})
'''


def _install_extension(root: Path, name: str = "echo_ext", code: str = EXT) -> Layout:
    (root / "extensions" / f"{name}.py").write_text(code, encoding="utf-8")
    doc = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    doc["extensions"] = [name]
    project.write(Layout(root), doc)
    extensions.reset()
    return Layout(root)


def test_an_extension_registers_every_kind_of_hook(fresh):
    layout = _install_extension(fresh)
    reg = extensions.load(layout)
    assert reg.loaded == ["echo_ext"] and reg.errors == []
    assert "echo_lane" in REGISTRY and REGISTRY["echo_lane"].gpu
    assert [c.name for c in doctor.run(layout) if c.name == "echo"] == ["echo"]
    from supervisor import render
    paths = render.render_all(layout)
    assert (fresh / "ECHO.md").exists() and any(p.name == "ECHO.md" for p in paths)
    from supervisor.adapters.python import JOBS
    assert "echo_job" in JOBS


def test_extension_overview_panel_reaches_the_control_center(fresh):
    layout = _install_extension(fresh)
    from supervisor.ui.routes.overview import overview_context
    ctx = overview_context(layout)
    assert ctx["project_name"] == "Demo Project"
    assert ctx["panels"][0]["title"] == "Echo" and ctx["panels"][0]["rows"] == [("state", "ok")]


def test_a_broken_extension_is_reported_not_fatal(fresh):
    layout = _install_extension(fresh, "broken", "raise RuntimeError('boom at import')\n")
    reg = extensions.load(layout)
    assert reg.loaded == [] and reg.errors and "boom at import" in reg.errors[0]
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["extensions"].ok is False and "broken" in checks["extensions"].detail


def test_extension_new_scaffolds_and_registers(fresh, monkeypatch):
    monkeypatch.chdir(fresh)
    r = runner.invoke(app, ["extension", "new", "mytool"])
    assert r.exit_code == 0, r.output
    assert (fresh / "extensions" / "mytool.py").exists()
    assert project.extensions(Layout(fresh)) == ["mytool"]
    extensions.reset()
    r = runner.invoke(app, ["extension", "list"])
    assert "loaded mytool" in r.output


def test_extension_cli_group_is_attached(fresh, monkeypatch):
    _install_extension(fresh)
    monkeypatch.chdir(fresh)
    out = subprocess.run([sys.executable, "-m", "supervisor", "echo", "hello"], capture_output=True, text=True,
                         cwd=str(fresh), env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert out.returncode == 0, out.stderr
    assert "hello from the extension" in out.stdout


def test_cost_lane_local_can_be_routed_to_an_extension_lane(fresh):
    layout = _install_extension(fresh)
    cfg = layout.config / "routing.yaml"
    doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    doc["cost_lanes"]["local"] = "echo_lane"
    cfg.write_text(yaml.safe_dump(doc), encoding="utf-8")
    from supervisor import scheduler, tasks as tasks_mod
    make_task(layout, "L")
    tasks_mod._update(layout, "L", lambda d: d.update(cost_category="local"))
    assert scheduler.adapter_name(tasks_mod.load(layout, "L"), layout) == "echo_lane"


# ------------------------------------------------------------------ doctor and migrate

def test_doctor_on_a_fresh_project_reports_project_state_and_routing(fresh):
    checks = {c.name: c for c in doctor.run(Layout(fresh))}
    assert checks["project"].ok and "Demo Project" in checks["project"].detail
    assert str(Layout(fresh).root) in checks["project"].detail       # which tree doctor examined (PHASE4 F3)
    assert checks["state"].ok and checks["routing"].ok and checks["python"].ok
    assert checks["git"].ok is False                                  # not a repo yet: says so, does not crash


def test_doctor_flags_a_framework_version_mismatch_and_migrate_fixes_it(fresh):
    layout = Layout(fresh)
    raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    raw["framework_version"] = "0.9.0"
    project.write(layout, raw)
    cfg = layout.config / "routing.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("max_resource_jobs", "max_gpu_jobs"), encoding="utf-8")
    assert "0.9.0" in doctor.check_project(layout).detail
    notes = migrations.run(layout, dry_run=True)
    assert any("max_gpu_jobs" in n for n in notes) and any("framework_version" in n for n in notes)
    assert "max_gpu_jobs" in cfg.read_text(encoding="utf-8")       # dry run wrote nothing
    migrations.run(layout)
    assert "max_resource_jobs" in cfg.read_text(encoding="utf-8")
    assert project.version_mismatch(layout) is None
    assert migrations.run(layout) == []                             # idempotent


def test_doctor_fails_on_malformed_or_versionless_project_yaml(fresh):
    layout = Layout(fresh)
    raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    del raw["framework_version"]
    project.write(layout, raw)
    check = doctor.check_project(layout)
    assert not check.ok and "framework_version" in check.detail and "supervisor migrate" in check.detail
    notes = migrations.run(layout)
    assert any("unset →" in n for n in notes) and doctor.check_project(layout).ok
    layout.project_file.write_text("name: [unclosed\nframework_version: 1\n", encoding="utf-8")
    check = doctor.check_project(layout)
    assert not check.ok and "malformed" in check.detail
    with pytest.raises(migrations.MigrationError):
        migrations.run(layout)
    assert project.name(layout) == project.DEFAULTS["name"]           # readers still get defaults, never a crash


def test_migrate_without_a_project_yaml_fails_clearly(tmp_path):
    layout = Layout(tmp_path / "nowhere").ensure()
    with pytest.raises(migrations.MigrationError, match="supervisor init"):
        migrations.run(layout)
    assert not layout.project_file.exists()


def test_legacy_approval_responses_are_migrated(fresh):
    from supervisor import tasks as tasks_mod
    layout = Layout(fresh)
    make_task(layout, "T-01", status="IN_PROGRESS")
    p = layout.tasks / "T-01.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    doc["delivery"] = {"digest": "d2", "approvals": [
        {"digest": "d1", "by": "owner", "response": "MORE_LIKE", "at": "2026-01-01T00:00:00+00:00"},
        {"digest": "d2", "by": "owner", "response": "APPROVE", "at": "2026-01-01T00:00:00+00:00"}]}
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (layout.inbox / "H-01.yaml").write_text(yaml.safe_dump({"id": "H-01", "status": "open", "for": "owner",
                                                            "allowed_responses": ["ACCEPT", "MORE_LIKE", "REGENERATE"]}), encoding="utf-8")
    with pytest.raises(tasks_mod.TaskError):
        tasks_mod.load(layout, "T-01")
    notes = migrations.run(layout, dry_run=True)
    assert any("T-01.yaml: response MORE_LIKE → REDO, APPROVE → ACCEPT" in n for n in notes)
    assert "MORE_LIKE" in p.read_text(encoding="utf-8")
    migrations.run(layout)
    t = tasks_mod.load(layout, "T-01")
    assert [a.response.value for a in t.delivery.approvals] == ["REDO", "ACCEPT"]
    assert yaml.safe_load((layout.inbox / "H-01.yaml").read_text(encoding="utf-8"))["allowed_responses"] == ["ACCEPT", "REDO"]
    assert migrations.run(layout) == []


def test_project_write_keeps_the_template_comments(fresh, monkeypatch):
    layout = Layout(fresh)
    before = layout.project_file.read_text(encoding="utf-8")
    monkeypatch.chdir(fresh)
    r = runner.invoke(app, ["extension", "new", "demo_job"])
    assert r.exit_code == 0, r.output
    after = layout.project_file.read_text(encoding="utf-8")
    assert project.extensions(layout) == ["demo_job"] and "demo_job" in after
    for comment in ("# project.yaml - what makes this folder", "# checked by `supervisor doctor`", "# Phase labels only."):
        assert comment in before and comment in after
    assert after.count("#") == before.count("#")


def test_the_old_limit_name_is_still_understood_at_runtime(fresh):
    from supervisor import gate
    layout = Layout(fresh)
    cfg = layout.config / "routing.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("max_resource_jobs: 1", "max_gpu_jobs: 3"), encoding="utf-8")
    assert gate.limits(layout)["max_resource_jobs"] == 3


def test_control_center_start_refuses_on_a_version_mismatch(fresh, monkeypatch):
    from fastapi.testclient import TestClient
    from supervisor.ui.app import create_app
    layout = Layout(fresh)
    raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    raw["framework_version"] = "0.1.0"
    project.write(layout, raw)
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(a))
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set("supervisor_csrf", "t0ken")
    r = client.post("/overview/start", data={"csrf_token": "t0ken"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "supervisor migrate" in r.text
    assert launched == []


def test_a_seed_task_without_status_runs_through_the_loop(fresh, monkeypatch):
    """`task import` on a seed that omits runtime fields (the normal case for a hand-written seed) must yield a task
    the loop can dispatch: the first launch sets IN_PROGRESS from an explicit NOT_STARTED, never from a missing key."""
    from supervisor import tasks as tasks_mod
    from supervisor.adapters.python import job
    from supervisor.loop import Supervisor
    from supervisor.models.task import TaskStatus
    layout = Layout(fresh)

    @job("write_seed_hello")
    def _hello(root, run_dir, **params):
        (root / "hello.txt").write_text("hi", encoding="utf-8")
        return {"status": "COMPLETE", "artifacts": ["hello.txt"], "notes": "ok"}

    (fresh / "state" / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": [{
        "id": "T0-01", "phase": 0, "title": "Hello", "objective": "o", "owner_role": "engineer", "actions": ["a"],
        "artifacts": ["hello.txt"], "acceptance": "hello.txt exists", "cost_category": "deterministic",
        "job": {"adapter": "python", "fn": "write_seed_hello"}}]}), encoding="utf-8")
    r = tasks_mod.import_seed(layout, fresh / "state" / "tasks_seed.yaml")
    assert r["created"] == ["T0-01"]
    assert yaml.safe_load((layout.tasks / "T0-01.yaml").read_text(encoding="utf-8"))["status"] == "NOT_STARTED"
    monkeypatch.setenv("SUPERVISOR_ROOT", str(fresh))
    Supervisor(layout, sleep_s=0).run(max_iterations=3)
    t = tasks_mod.load(layout, "T0-01")
    assert t.status == TaskStatus.COMPLETE, t.history
    assert (fresh / "hello.txt").exists()


# ------------------------------------------------------------------ the framework repo never holds project work

def test_init_refuses_a_target_inside_the_framework_clone(tmp_path, monkeypatch):
    from supervisor import updates
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    inside = updates.framework_dir() / "docs" / "accidental-project"
    r = runner.invoke(app, ["init", str(inside)])
    assert r.exit_code != 0 and "framework" in r.output.replace("\n", "")
    assert not inside.exists()


def test_framework_gitignore_blocks_project_shapes_at_the_root():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for line in ("/project.yaml", "/state/", "/secrets/", "/extensions/", "/agents/", "/config/", "/uploads/"):
        assert line in text, line
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip("not a git checkout")
    # `check-ignore -q` accepts a single pathname only (several -> exit 128, which used to look like "no git checkout")
    for rel in ("project.yaml", "state/x.yaml", "config/routing.yaml", "secrets/k.txt", "agents/x.md", "uploads/f.bin"):
        out = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", rel], capture_output=True, text=True)
        assert out.returncode == 0, f"{rel} is not git-ignored at the framework root: rc={out.returncode} {out.stderr}"


def _seed_task(tid, title):
    return {"id": tid, "phase": 0, "title": title, "objective": "o", "owner_role": "engineer", "actions": ["a"],
            "artifacts": ["out.txt"], "tests": ["t"], "acceptance": "ok", "escalation": "none",
            "human_approval": "none", "cost_category": "deterministic"}


def test_task_import_dry_run_validates_and_writes_nothing(fresh, monkeypatch):
    """PHASE4 F4: `task import --dry-run` reports what an import would create and refresh, runs the same validation,
    and touches neither the task store nor the log, so it is safe while a supervisor runs."""
    monkeypatch.chdir(fresh)
    seed = fresh / "state" / "tasks_seed.yaml"
    seed.write_text(yaml.safe_dump({"tasks": [_seed_task("A", "first")]}), encoding="utf-8")
    assert runner.invoke(app, ["task", "import"]).exit_code == 0
    log_before = (fresh / "state" / "log.jsonl").read_text(encoding="utf-8")
    store_before = (fresh / "state" / "tasks" / "A.yaml").read_text(encoding="utf-8")
    seed.write_text(yaml.safe_dump({"tasks": [_seed_task("A", "renamed"), _seed_task("B", "second")]}), encoding="utf-8")

    r = runner.invoke(app, ["task", "import", "--update-contracts", "A", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would create 1 (B), update 1 (A); nothing written" in r.output
    assert not (fresh / "state" / "tasks" / "B.yaml").exists()
    assert (fresh / "state" / "tasks" / "A.yaml").read_text(encoding="utf-8") == store_before
    assert (fresh / "state" / "log.jsonl").read_text(encoding="utf-8") == log_before

    seed.write_text(yaml.safe_dump({"tasks": [{**_seed_task("C", "bad"), "depends_on": ["NOPE"]}]}), encoding="utf-8")
    r = runner.invoke(app, ["task", "import", "--dry-run"])
    assert r.exit_code != 0 and "seed invalid" in r.output
    r = runner.invoke(app, ["task", "import", "--seed", "../elsewhere.yaml", "--dry-run"])
    assert r.exit_code != 0 and "inside the project" in r.output


def test_local_gpu_migration_rewrites_only_the_field_and_keeps_line_endings(fresh):
    """PHASE4 F5: the 1.0.0 migration reported every file that mentions local_gpu (a note, the seed's header comment)
    on every run while writing identical text, and its write_text turned LF files into CRLF on Windows."""
    layout = Layout(fresh)
    make_task(layout, "G", status="IN_PROGRESS")
    make_task(layout, "N", status="IN_PROGRESS")
    g, n = layout.tasks / "G.yaml", layout.tasks / "N.yaml"
    g.write_bytes(g.read_bytes().replace(b"\r\n", b"\n").replace(b"cost_category: claude",
                                                                   b"cost_category: local_gpu")       # LF file
                  + b"review_note: |\n  the old contract read\n  cost_category: local_gpu\n"     # an example in a note
                  + b"acceptance_example: \"line one\n  cost_category: local_gpu\"\n")           # in a quoted string
    n_text = n.read_text(encoding="utf-8") + "notes:\n- moved off local_gpu last week\n"
    n.write_bytes(n_text.replace("\n", "\r\n").encode("utf-8"))                                       # CRLF file
    seed = layout.root / "state" / "tasks_seed.yaml"
    seed.write_bytes(b"# tasks once ran on local_gpu\r\ntasks:\r\n- id: S\r\n  cost_category: 'local_gpu'  # gpu\r\n")
    n_before = n.read_bytes()

    notes = [x for x in migrations.run(layout) if "local_gpu" in x]
    assert sorted(notes) == ["state/tasks/G.yaml: cost_category local_gpu → local",
                             "state/tasks_seed.yaml: cost_category local_gpu → local"]
    assert b"cost_category: local\n" in g.read_bytes() and b"\r\n" not in g.read_bytes()
    assert g.read_bytes().count(b"cost_category: local_gpu") == 2      # both examples kept as written (review P2)
    assert yaml.safe_load(g.read_text(encoding="utf-8"))["cost_category"] == "local"
    assert seed.read_bytes() == b"# tasks once ran on local_gpu\r\ntasks:\r\n- id: S\r\n  cost_category: 'local'  # gpu\r\n"
    assert n.read_bytes() == n_before                               # a mention in a note is not the field
    assert migrations.run(layout) == []                             # the repeat run reports nothing


def test_local_gpu_migration_applies_each_span_once_in_file_order(fresh):
    """Review of 1.8.4, second round (P2): a YAML alias returns its anchor's span twice, and the root mapping's field
    is listed before the seed's tasks although it stands after them; applying those spans as listed rewrote text the
    previous replacement had shifted."""
    layout = Layout(fresh)
    seed = layout.root / "state" / "tasks_seed.yaml"
    seed.write_bytes(b"tasks:\n- id: A\n  cost_category: &gpu local_gpu\n- id: B\n  cost_category: *gpu\n"
                     b"- id: C\n  cost_category: local_gpu\ncost_category: local_gpu  # root, after the tasks\n")
    migrations.run(layout)
    assert seed.read_bytes() == (b"tasks:\n- id: A\n  cost_category: &gpu local\n- id: B\n  cost_category: *gpu\n"
                                 b"- id: C\n  cost_category: local\ncost_category: local  # root, after the tasks\n")
    doc = yaml.safe_load(seed.read_text(encoding="utf-8"))
    assert [t["cost_category"] for t in doc["tasks"]] == ["local", "local", "local"]
    assert migrations.run(layout) == []


def test_local_gpu_migration_keeps_anchor_names_and_tags(fresh):
    """Review of 1.8.4, third round (P2): a scalar's span starts at its anchor and tag, so `&local_gpu local_gpu`
    lost its anchor name (and `*local_gpu` then pointed at nothing)."""
    layout = Layout(fresh)
    seed = layout.root / "state" / "tasks_seed.yaml"
    seed.write_bytes(b"tasks:\n- id: A\n  cost_category: &local_gpu local_gpu\n- id: B\n  cost_category: *local_gpu\n"
                     b"- id: C\n  cost_category: &x_local_gpu !!str 'local_gpu'  # tagged\n")
    migrations.run(layout)
    assert seed.read_bytes() == (b"tasks:\n- id: A\n  cost_category: &local_gpu local\n"
                                 b"- id: B\n  cost_category: *local_gpu\n"
                                 b"- id: C\n  cost_category: &x_local_gpu !!str 'local'  # tagged\n")
    doc = yaml.safe_load(seed.read_text(encoding="utf-8"))
    assert [t["cost_category"] for t in doc["tasks"]] == ["local", "local", "local"]
    assert migrations.run(layout) == []


def test_project_write_keeps_a_crlf_project_yaml_crlf(fresh):
    layout = Layout(fresh)
    layout.project_file.write_bytes(layout.project_file.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    raw["framework_version"] = "0.9.0"
    project.write(layout, raw)
    data = layout.project_file.read_bytes()
    assert b"0.9.0" in data and data.count(b"\n") == data.count(b"\r\n")
