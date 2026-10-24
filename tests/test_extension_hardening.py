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


def test_a_removed_extensions_projection_paths_stop_being_excluded(layout):
    """Persisted projection paths belong to the extension that rendered them: once the extension is removed from
    project.yaml its files are ordinary files again, so a change to WEEKLY.md is an undeclared write."""
    _configure(layout.root, ["weekly"], {"weekly": '''
def setup(registry):
    def weekly(layout):
        p = layout.root / "WEEKLY.md"
        p.write_text("# weekly")
        return p
    registry.projection(weekly)
'''})
    from supervisor import render
    render.render_all(layout)
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}
    assert extensions._recorded_projections(layout) == {"weekly": ["WEEKLY.md"]}
    doc = dict(project.load(layout))
    doc["extensions"] = []
    project.write(layout, doc)
    extensions.reset()
    assert extensions.known_projection_paths(layout) == set()
    assert "WEEKLY.md" not in verify.excluded_files(layout)
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "x").write_text("artifact")
    (layout.root / "WEEKLY.md").write_text("edited by hand")
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert not outcome.ok and "WEEKLY.md" in outcome.detail
    doc["extensions"] = ["weekly"]                                        # re-adding the extension restores it
    project.write(layout, doc)
    extensions.reset()
    assert "WEEKLY.md" in verify.excluded_files(layout)


WEEKLY_EXT = '''
def setup(registry):
    def weekly(layout):
        p = layout.root / "%s"
        p.write_text("# view")
        return p
    registry.projection(weekly)
'''


def test_a_renamed_projection_output_retires_the_old_file(layout):
    """A projection that now writes REPORT.md instead of WEEKLY.md: only the current output is excluded; a hand edit
    to the retired WEEKLY.md is an undeclared write again."""
    from supervisor import render
    _configure(layout.root, ["weekly"], {"weekly": WEEKLY_EXT % "WEEKLY.md"})
    render.render_all(layout)
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}
    _configure(layout.root, ["weekly"], {"weekly": WEEKLY_EXT % "REPORT.md"})     # resets the registry
    render.render_all(layout)
    assert extensions.known_projection_paths(layout) == {"REPORT.md"}
    extensions.reset()
    assert verify.excluded_files(layout) >= {"REPORT.md"} and "WEEKLY.md" not in verify.excluded_files(layout)
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "x").write_text("artifact")
    (layout.root / "WEEKLY.md").write_text("edited by hand")
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert not outcome.ok and "WEEKLY.md" in outcome.detail
    (layout.root / "WEEKLY.md").write_text("# view")                         # restore; REPORT.md re-rendering is fine
    before = verify.snapshot(layout, task)
    (layout.root / "REPORT.md").write_text("# view 2")
    assert verify.verify(layout, task, ["x"], before=before).ok


def test_a_1_2_1_projection_cache_is_honoured_until_the_first_full_render(layout):
    """Upgrading from the flat `paths` list: before anything has rendered on the new code, an untouched WEEKLY.md
    is still excluded; after every projection rendered once the unattributed entries are gone (re-recorded)."""
    import json
    from supervisor import render
    _configure(layout.root, ["weekly"], {"weekly": WEEKLY_EXT % "WEEKLY.md"})
    (layout.root / "WEEKLY.md").write_text("# view")
    layout.runtime.mkdir(parents=True, exist_ok=True)
    (layout.runtime / extensions.PROJECTIONS_FILE).write_text(json.dumps({"paths": ["WEEKLY.md", "OLD.md"]}))
    extensions.reset()
    assert extensions.known_projection_paths(layout) >= {"WEEKLY.md", "OLD.md"}
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "x").write_text("artifact")
    (layout.root / "WEEKLY.md").write_text("# view (loop re-rendered it before verify ran)")
    assert verify.verify(layout, task, ["x"], before=before).ok
    render.render_all(layout)                                                # every projection rendered once
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}       # OLD.md: unattributed, gone
    assert "WEEKLY.md" in extensions.load(layout).projection_paths and "OLD.md" not in extensions.load(layout).projection_paths
    extensions.reset()
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}


def test_a_1_2_1_x_by_extension_cache_is_read(layout):
    import json
    _configure(layout.root, ["weekly"], {"weekly": WEEKLY_EXT % "WEEKLY.md"})
    layout.runtime.mkdir(parents=True, exist_ok=True)
    (layout.runtime / extensions.PROJECTIONS_FILE).write_text(json.dumps({"by_extension": {"weekly": ["WEEKLY.md"]}}))
    extensions.reset()
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}


# ------------------------------------------------------------------ 1.4.1: the record follows the current registrations

def _hand_edit_is_caught(layout, retired: str) -> None:
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "x").write_text("artifact")
    (layout.root / retired).write_text("edited by hand")
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert not outcome.ok and retired in outcome.detail, outcome.detail


def test_a_changed_declaration_retires_the_old_file(layout):
    """Declared paths used to accumulate: WEEKLY.md -> REPORT.md kept both excluded."""
    from supervisor import render
    decl = '''
def setup(registry):
    def weekly(layout):
        p = layout.root / "%s"
        p.write_text("# view")
        return p
    registry.projection(weekly, path="%s")
'''
    _configure(layout.root, ["weekly"], {"weekly": decl % ("WEEKLY.md", "WEEKLY.md")})
    render.render_all(layout)
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}
    _configure(layout.root, ["weekly"], {"weekly": decl % ("REPORT.md", "REPORT.md")})
    assert extensions.load(layout).projection_paths >= {"REPORT.md"}
    assert extensions.known_projection_paths(layout) == {"REPORT.md"}           # before any render
    extensions.reset()
    assert "WEEKLY.md" not in verify.excluded_files(layout)
    _hand_edit_is_caught(layout, "WEEKLY.md")


def test_a_removed_projection_of_a_configured_extension_stops_being_excluded(layout):
    from supervisor import render
    _configure(layout.root, ["weekly"], {"weekly": WEEKLY_EXT % "WEEKLY.md"})
    render.render_all(layout)
    assert extensions.known_projection_paths(layout) == {"WEEKLY.md"}
    _configure(layout.root, ["weekly"], {"weekly": "def setup(registry):\n    pass\n"})   # still configured
    assert extensions.load(layout).projection_paths == set()
    assert extensions.known_projection_paths(layout) == set()
    _hand_edit_is_caught(layout, "WEEKLY.md")


def test_legacy_entries_go_when_no_projection_is_registered(layout):
    import json
    _configure(layout.root, ["noviews"], {"noviews": "def setup(registry):\n    pass\n"})
    (layout.root / "OLD.md").write_text("# old view")
    layout.runtime.mkdir(parents=True, exist_ok=True)
    (layout.runtime / extensions.PROJECTIONS_FILE).write_text(json.dumps({"paths": ["OLD.md"]}))
    extensions.reset()
    assert extensions.load(layout).projection_paths == set()
    assert extensions.known_projection_paths(layout) == set()
    _hand_edit_is_caught(layout, "OLD.md")


def test_legacy_entries_stay_while_an_extension_fails_to_load(layout):
    import json
    _configure(layout.root, [{"name": "broken", "required": False}], {"broken": "def setup(registry):\n    raise RuntimeError('x')\n"})
    layout.runtime.mkdir(parents=True, exist_ok=True)
    (layout.runtime / extensions.PROJECTIONS_FILE).write_text(json.dumps({"paths": ["OLD.md"]}))
    extensions.reset()
    assert "OLD.md" in extensions.load(layout).projection_paths    # its registrations are unknown: keep the exclusion


def test_same_named_callbacks_get_distinct_records(layout):
    """Two lambdas used to share one `<ext>:<lambda>` key: rendering B retired A's record."""
    from supervisor import render
    _configure(layout.root, ["views"], {"views": '''
def _writer(name):
    def view(layout):
        p = layout.root / name
        p.write_text("# " + name + str(len(list(layout.tasks.glob("*.yaml")))))
        return p
    return view

def setup(registry):
    registry.projection(lambda layout: (layout.root / "A.md").write_text("a") and layout.root / "A.md")
    registry.projection(lambda layout: (layout.root / "B.md").write_text("b") and layout.root / "B.md")
    registry.projection(_writer("C.md"))
    registry.projection(_writer("D.md"))
'''})
    render.render_all(layout)
    reg = extensions.load(layout)
    assert reg._projection_keys == ["views:<lambda>", "views:<lambda>#2", "views:view", "views:view#2"]
    assert extensions.known_projection_paths(layout) == {"A.md", "B.md", "C.md", "D.md"}
    extensions.reset()
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "x").write_text("artifact")
    for name in "ABCD":
        (layout.root / f"{name}.md").write_text("re-rendered")
    outcome = verify.verify(layout, task, ["x"], before=before)
    assert outcome.ok, outcome.detail


def test_an_explicit_projection_name_is_the_identity_and_must_be_unique(layout):
    _configure(layout.root, ["views"], {"views": '''
def setup(registry):
    registry.projection(lambda layout: layout.root / "A.md", path="A.md", name="alpha")
    registry.projection(lambda layout: layout.root / "B.md", path="B.md", name="beta")
'''})
    assert extensions.load(layout)._projection_keys == ["views:alpha", "views:beta"]
    _configure(layout.root, [{"name": "dup", "required": False}], {"dup": '''
def setup(registry):
    registry.projection(lambda layout: layout.root / "A.md", name="alpha")
    registry.projection(lambda layout: layout.root / "B.md", name="alpha")
'''})
    assert any("registered twice" in e for e in extensions.load(layout).errors)
