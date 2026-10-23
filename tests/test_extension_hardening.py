"""Extension hardening: a failed required extension blocks the loop and the packet screens; extension modules are
isolated per project; an adapter's exception fails its own attempt, not the loop; extension projections are
rendered views for verification; doctor reports policy config problems."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from supervisor import doctor, extensions, jobs, policy, project, runtime, tasks, verify
from supervisor.adapters import REGISTRY, Result
from supervisor.adapters.base import Adapter, register
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from supervisor.paths import Layout
from tests.conftest import make_task


def _configure(root: Path, entries: list, modules: dict[str, str]) -> Layout:
    """Write extensions/<name>.py for each module and list `entries` in project.yaml."""
    (root / "extensions").mkdir(parents=True, exist_ok=True)
    for name, code in modules.items():
        (root / "extensions" / f"{name}.py").write_text(code, encoding="utf-8")
    doc = dict(project.load(Layout(root)))
    doc["name"] = doc.get("name") or "proj"
    doc["extensions"] = entries
    project.write(Layout(root), doc)
    extensions.reset()
    return Layout(root).ensure()


PRIVACY = '''
from supervisor.policy import PolicyViolation
def setup(registry):
    def screen(layout, text, where):
        if "CUSTOMER-RECORD" in text:
            raise PolicyViolation(f"{where} refused: customer record")
    registry.packet_screen(screen)
'''


# ------------------------------------------------------------------ 1. a failed extension blocks, never silently drops out

def test_a_failing_required_extension_blocks_the_loop_and_the_packets(layout):
    _configure(layout.root, ["privacy"], {"privacy": "import nosuchmodule_xyz\n" + PRIVACY})
    reg = extensions.load(layout)
    assert reg.loaded == [] and reg.blocking and "privacy" in reg.blocking[0]
    with pytest.raises(policy.PolicyViolation, match="privacy"):
        policy.assert_packet_clean("brief with CUSTOMER-RECORD text", layout, "delivery packet")
    with pytest.raises(policy.PolicyViolation, match="privacy"):
        policy.assert_packet_clean("an ordinary brief", layout)        # the screen is missing: nothing is clean
    make_task(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    with pytest.raises(extensions.ExtensionError, match="privacy"):
        sup.once()
    assert sup.run(max_iterations=1) == 1
    doc = runtime.read(layout)
    assert doc["status"] == "BLOCKED" and "privacy" in doc["reason"] and "required extension" in doc["reason"]
    assert tasks.load(layout, "A").status == TaskStatus.NOT_STARTED       # nothing was dispatched
    assert not list(layout.jobs.glob("*.json"))
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["extensions"].ok is False and "LOOP BLOCKED" in checks["extensions"].detail


def test_a_malformed_project_yaml_blocks_like_a_failed_required_extension(layout):
    """An unparseable project.yaml hides its extensions: list — including the policy screens the project relies on.
    It must block the loop and the packets, never fall back to 'no extensions'."""
    _configure(layout.root, ["privacy"], {"privacy": PRIVACY})
    assert extensions.load(layout).loaded == ["privacy"]
    project.path(layout).write_text("name: [unterminated\nextensions:\n  - privacy\n", encoding="utf-8")
    extensions.reset()
    reg = extensions.load(layout)
    assert reg.loaded == [] and reg.blocking and "project.yaml" in reg.blocking[0] and "malformed" in reg.blocking[0]
    with pytest.raises(policy.PolicyViolation, match="project.yaml"):
        policy.assert_packet_clean("brief with CUSTOMER-RECORD text", layout, "delivery packet")
    with pytest.raises(policy.PolicyViolation, match="project.yaml"):
        policy.assert_packet_clean("an ordinary brief", layout)
    make_task(layout, "M")
    assert Supervisor(layout, sleep_s=0).run(max_iterations=1) == 1
    doc = runtime.read(layout)
    assert doc["status"] == "BLOCKED" and "project.yaml" in doc["reason"]
    assert tasks.load(layout, "M").status == TaskStatus.NOT_STARTED and not list(layout.jobs.glob("*.json"))
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["project"].ok is False and checks["extensions"].ok is False


def test_an_explicitly_optional_extension_failing_is_a_warning_only(layout):
    _configure(layout.root, [{"name": "privacy", "required": False}], {"privacy": "raise RuntimeError('boom')\n"})
    reg = extensions.load(layout)
    assert reg.errors and not reg.blocking and reg.blocking_reason() is None
    assert policy.assert_packet_clean("an ordinary brief", layout) == "an ordinary brief"
    sup = Supervisor(layout, sleep_s=0)
    assert sup.once() == "RUN"
    assert runtime.read(layout)["status"] != "BLOCKED"
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["extensions"].ok is False and "optional" in checks["extensions"].detail


def test_a_loaded_screen_still_refuses_what_it_is_for(layout):
    _configure(layout.root, ["privacy"], {"privacy": PRIVACY})
    assert extensions.load(layout).loaded == ["privacy"]
    with pytest.raises(policy.PolicyViolation, match="customer record"):
        policy.assert_packet_clean("brief with CUSTOMER-RECORD text", layout, "delivery packet")
    assert policy.assert_packet_clean("an ordinary brief", layout) == "an ordinary brief"


# ------------------------------------------------------------------ 2. extension modules are isolated per project

SHARED = 'MARK = {mark!r}\ndef setup(registry):\n    registry.overview_panel(lambda layout: {{"title": MARK, "rows": []}})\n'


def test_two_projects_with_the_same_extension_name_load_their_own_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    a = _configure(tmp_path / "a", ["shared_ext"], {"shared_ext": SHARED.format(mark="A")})
    b = _configure(tmp_path / "b", ["shared_ext"], {"shared_ext": SHARED.format(mark="B")})
    extensions.reset()
    ra, rb = extensions.load(a), extensions.load(b)
    assert ra.loaded == ["shared_ext"] and rb.loaded == ["shared_ext"], (ra.errors, rb.errors)
    assert ra.overview_panels[0](a)["title"] == "A"
    assert rb.overview_panels[0](b)["title"] == "B"
    import sys
    assert str(a.extensions) not in sys.path and str(b.extensions) not in sys.path
    assert extensions.load(a) is ra                                     # idempotent: same registry, no second setup


def test_an_extension_named_like_a_stdlib_module_loads_the_project_file(layout):
    _configure(layout.root, ["json"], {"json": 'def setup(registry):\n    registry.overview_panel(lambda l: {"title": "mine", "rows": []})\n'})
    reg = extensions.load(layout)
    assert reg.loaded == ["json"] and reg.errors == [], reg.errors
    assert reg.overview_panels[0](layout)["title"] == "mine"
    assert json.dumps({"still": "stdlib"}) == '{"still": "stdlib"}'      # the real json is untouched


def test_sibling_extension_modules_import_each_other_relatively(layout):
    _configure(layout.root, ["main_ext"], {
        "helpers": "VALUE = 42\n",
        "main_ext": "from .helpers import VALUE\ndef setup(registry):\n    registry.overview_panel(lambda l: {'title': str(VALUE), 'rows': []})\n"})
    reg = extensions.load(layout)
    assert reg.loaded == ["main_ext"], reg.errors
    assert reg.overview_panels[0](layout)["title"] == "42"


def test_a_non_identifier_extension_name_is_rejected(layout):
    _configure(layout.root, ["../evil", "ok_ext"], {"ok_ext": "def setup(registry): pass\n"})
    reg = extensions.load(layout)
    assert reg.loaded == ["ok_ext"]
    assert any("invalid extension name" in e for e in reg.errors)


# ------------------------------------------------------------------ 3. adapter exceptions fail the attempt, not the loop

class BoomAdapter(Adapter):
    name = "boom_lane"
    mode = "prepare"

    def prepare(self, layout, task, attempt_id):
        if self.mode == "prepare":
            raise RuntimeError("prepare exploded")
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir)}

    def recover(self, spec):
        return None

    def submit(self, spec):
        if self.mode == "submit":
            raise RuntimeError("submit exploded")
        return {"h": 1}

    def alive(self, handle):
        return False

    def collect(self, spec, handle):
        if self.mode == "collect":
            raise RuntimeError("collect exploded")
        return Result(status="COMPLETE")


def _boom_task(layout, tid):
    make_task(layout, tid)
    tasks._update(layout, tid, lambda d: d.update(job={"adapter": "boom_lane"}))


@pytest.mark.parametrize("stage", ["prepare", "submit", "collect"])
def test_a_throwing_adapter_fails_its_attempt_and_the_loop_goes_on(layout, stage):
    register(BoomAdapter())
    REGISTRY["boom_lane"].mode = stage
    _boom_task(layout, "X")
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        assert sup.once() == "RUN"
    assert sup.errors == []                                              # the loop's error budget is untouched
    assert runtime.read(layout)["status"] == "RUNNING"
    rec = jobs.read(layout, "X.a1")
    assert rec["phase"] == "DONE" and rec["result"] == "FAILED" and f"{stage} exploded" in rec["error"]
    task = tasks.load(layout, "X")
    assert task.status == TaskStatus.FAILED
    assert f"{stage} exploded" in (task.delivery.verification.detail or "")
    assert len(list(layout.jobs.glob("X.*.json"))) == 1                  # no retry storm
    events = [json.loads(l) for l in layout.log.read_text().splitlines()]
    assert any(e["event"] == "adapter_error" and e.get("stage") == stage for e in events)
    assert not any(e["event"] == "loop_error" for e in events)


def test_a_throwing_adapter_does_not_block_the_run_loop(layout):
    register(BoomAdapter())
    REGISTRY["boom_lane"].mode = "prepare"
    for tid in "ABCDEFG":
        _boom_task(layout, tid)
    code = Supervisor(layout, sleep_s=0).run(max_iterations=6)
    assert code == 0
    assert runtime.read(layout)["status"] == "STOPPED"                   # max iterations, never BLOCKED
    assert all(tasks.load(layout, t).status == TaskStatus.FAILED for t in "ABCDEFG")


# ------------------------------------------------------------------ 4. extension projections are rendered views

def test_an_extension_projection_changing_during_an_attempt_is_not_an_undeclared_write(layout):
    _configure(layout.root, ["weekly"], {"weekly": '''
def setup(registry):
    def weekly(layout):
        p = layout.root / "WEEKLY.md"
        p.write_text("# weekly " + str(len(list(layout.tasks.glob("*.yaml")))))
        return p
    registry.projection(weekly)
'''})
    from supervisor import render
    render.render_all(layout)                                            # the projection's path is learned here
    assert "WEEKLY.md" in verify.excluded_files(layout)
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    assert "WEEKLY.md" not in before["repo"]
    (layout.root / "x").write_text("artifact")
    make_task(layout, "U")                                               # the loop renders every tick: the view changes
    render.render_all(layout)
    assert (layout.root / "WEEKLY.md").read_text().endswith("2")
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert outcome.ok, outcome.detail


def test_a_learned_projection_survives_a_restart_and_reaches_a_fresh_process(layout):
    """What a render learned is written to state/runtime: after the registry is reset (a new loop process, or the
    verify child) the projection is still excluded, so an untouched WEEKLY.md never comes back as an undeclared
    write."""
    _configure(layout.root, ["weekly"], {"weekly": '''
def setup(registry):
    def weekly(layout):
        p = layout.root / "WEEKLY.md"
        p.write_text("# weekly " + str(len(list(layout.tasks.glob("*.yaml")))))
        return p
    registry.projection(weekly)
'''})
    from supervisor import render
    render.render_all(layout)
    assert "WEEKLY.md" in extensions.known_projection_paths(layout)
    extensions.reset()                                                   # a new process: nothing learned in memory
    assert "WEEKLY.md" in extensions.load(layout).projection_paths and "WEEKLY.md" in verify.excluded_files(layout)
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "x").write_text("artifact")
    make_task(layout, "U")
    extensions.reset()
    render.render_all(layout)
    extensions.reset()
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert outcome.ok, outcome.detail
    (layout.root / "stray.md").write_text("not a projection")
    assert not verify.verify(layout, task, ["x"], before=before).ok     # a real undeclared write is still caught


def test_a_projection_declared_up_front_is_excluded_before_any_render(layout):
    _configure(layout.root, ["weekly"], {"weekly": '''
def setup(registry):
    registry.projection(lambda layout: layout.root / "WEEKLY.md", path="WEEKLY.md")
'''})
    assert "WEEKLY.md" in verify.excluded_files(layout)
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "WEEKLY.md").write_text("rendered later")
    (layout.root / "x").write_text("artifact")
    assert verify.verify(layout, task, ["x"], before=before).ok


def test_a_projection_learned_after_the_before_snapshot_is_still_excluded(layout):
    _configure(layout.root, ["weekly"], {"weekly": '''
def setup(registry):
    def weekly(layout):
        (layout.root / "WEEKLY.md").write_text("v2")
        return layout.root / "WEEKLY.md"
    registry.projection(weekly)
'''})
    (layout.root / "WEEKLY.md").write_text("v1")
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)                               # WEEKLY.md still in this manifest
    assert "WEEKLY.md" in before["repo"]
    from supervisor import render
    render.render_all(layout)
    (layout.root / "x").write_text("artifact")
    assert verify.verify(layout, task, ["x"], before=before).ok


def test_an_undeclared_write_is_still_caught_next_to_a_projection(layout):
    _configure(layout.root, ["weekly"], {"weekly": '''
def setup(registry):
    registry.projection(lambda layout: layout.root / "WEEKLY.md", path="WEEKLY.md")
'''})
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "WEEKLY.md").write_text("view")
    (layout.root / "x").write_text("artifact")
    (layout.root / "sneaky.md").write_text("not declared")
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert outcome.ok is False and "sneaky.md" in outcome.detail and "WEEKLY.md" not in outcome.detail


# ------------------------------------------------------------------ 5. doctor shows policy config problems

def test_doctor_reports_policy_config_problems(layout):
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["policy"].ok is True
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / policy.CONFIG_NAME).write_text("- not\n- a mapping\n", encoding="utf-8")
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["policy"].ok is False and "not a mapping" in checks["policy"].detail
    assert checks["policy"].detail == "; ".join(policy.config_problems(layout))[:300]
    (layout.config / policy.CONFIG_NAME).write_text(yaml.safe_dump({"secrets": {"min_value_length": 12}}), encoding="utf-8")
    checks = {c.name: c for c in doctor.run(layout)}
    assert checks["policy"].ok is True
