"""2.9.0: the supported upgrade floor (docs/UPDATING.md), the seven compatibility shims it makes dead (the
framework review's "Compatibility shims that a migration already covers"), and the two handlers the same review's
"Handlers with no producer" named -- `issue.resolve` (phase 6, R-21; confirmed here, not redone) and `lane.reset`
(given one in this task). The words here are invented; none of this names a project."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from supervisor import api_contract as ac
from supervisor import commands, escalation, gate, inbox, migrations, plugins, project, store, tasks, template, testing
from supervisor.adapters import REGISTRY
from supervisor.adapters.claude_cli import model_limits
from supervisor.cli import app
from supervisor.paths import Layout
from tests.conftest import make_task

runner = CliRunner()


@pytest.fixture
def stamped(tmp_path, monkeypatch) -> Layout:
    """A full template project (routing.yaml and friends present with their real defaults), discovered root."""
    target = tmp_path / "demo"
    template.stamp(target, name="Demo")
    monkeypatch.setenv("SUPERVISOR_ROOT", str(target))
    plugins.reset()
    return Layout(target)


def _set_version(layout: Layout, version: str) -> None:
    raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    raw["framework_version"] = version
    project.write(layout, raw)


# --------------------------------------------------------------------- the floor itself

def test_a_project_at_the_floor_loads_is_migrated_and_runs(stamped):
    _set_version(stamped, project.SUPPORTED_FLOOR)
    assert project.load(stamped)["name"] == "Demo"                       # loads
    note = project.version_mismatch(stamped)
    assert note is not None and "supported upgrade floor" not in note    # ordinary mismatch, not the floor message
    notes = migrations.run(stamped)                                      # migrates
    assert any("framework_version" in n for n in notes)
    assert project.version_mismatch(stamped) is None
    from supervisor.loop import Supervisor
    Supervisor(stamped, sleep_s=0).once()                                # runs: one idle turn, no crash


def test_a_project_below_the_floor_fails_with_a_message_naming_the_floor_and_the_path_through_it(stamped):
    _set_version(stamped, "1.13.0")
    note = project.version_mismatch(stamped)
    assert note is not None
    assert project.SUPPORTED_FLOOR in note and "supervisor migrate" in note
    from supervisor import doctor
    check = doctor.check_project(stamped)
    assert not check.ok and project.SUPPORTED_FLOOR in check.detail       # a named, typed refusal, not a traceback


def test_migrate_still_carries_a_below_floor_project_the_rest_of_the_way_in_one_step(stamped):
    """The floor changes what the runtime tolerates silently, not what `supervisor migrate` itself can fix --
    the plugins-rename migration (2.0.0) exists exactly to carry a project up from below the floor
    (tests/test_migrations_plugins.py), so this task's floor must not break that."""
    _set_version(stamped, "1.13.0")
    assert migrations.run(stamped) is not None
    assert project.version_mismatch(stamped) is None


# --------------------------------------------------------------------- the seven shims: each removed, each
# migration (where one exists) still runs

def test_shim_1_gate_max_gpu_jobs_the_routing_limits_rename_migration_still_runs(stamped):
    cfg = stamped.config / "routing.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nlimits:\n  max_gpu_jobs: 3\n", encoding="utf-8")
    assert gate.limits(stamped)["max_resource_jobs"] == gate.DEFAULT_LIMITS["max_resource_jobs"]   # not read any more
    notes = migrations.run(stamped)
    assert any("max_resource_jobs" in n for n in notes)
    assert gate.limits(stamped)["max_resource_jobs"] == 3                                          # migrated, now read


NOTES_MODULE = ("from pydantic import BaseModel\n\n\nclass Settings(BaseModel):\n    base_url: str\n\n\n"
               "def setup(registry):\n    pass\n")
NOTES_MANIFEST = ('identity = "notes"\nversion = "1.0.0"\nframework = ">=1.11"\n[config]\n'
                  'section = "plugins.notes"\nschema = "notes:Settings"\nlegacy_sections = ["notes"]\n')


def test_shim_2_plugin_legacy_sections_and_top_level_gpu_their_migrations_still_run(tmp_path, monkeypatch):
    from supervisor.plugin import _config as pc
    layout = testing.project(tmp_path / "p", modules={"notes": NOTES_MODULE},
                             files={"plugins/notes.toml": NOTES_MANIFEST},
                             routing={"notes": {"base_url": "x"}, "gpu": {"usable_vram_floor_mb": 1}})
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    assert pc.resources(layout) == {"gpu": {}}                            # not read from the old top level any more
    reg = plugins.load(layout)
    assert reg.loaded == [] and any("base_url" in e for e in reg.errors)  # plugins.notes has nothing yet: fails
    notes = migrations.run(layout)
    assert any("plugins.notes" in n for n in notes) and any("resources.gpu" in n for n in notes)
    plugins.reset()
    reg = plugins.load(layout)
    assert reg.loaded == ["notes"], reg.errors                            # migrated: base_url found at the new path
    assert pc.resources(layout) == {"gpu": {"usable_vram_floor_mb": 1}}


def test_shim_3_the_1_2_1_projection_cache_self_heals_without_the_removed_fallback(stamped):
    stamped.runtime.mkdir(parents=True, exist_ok=True)
    (stamped.runtime / plugins.PROJECTIONS_FILE).write_text(json.dumps({"paths": ["OLD.md"]}), encoding="utf-8")
    plugins.reset()
    assert plugins.known_projection_paths(stamped) == set()               # no plugin configured: nothing to carry
    from supervisor import render
    render.render_all(stamped)                                           # one render: self-heals regardless
    assert (stamped.runtime / plugins.PROJECTIONS_FILE).exists()


def test_shim_4_a_spec_without_idle_s_no_longer_gets_the_pre_1_7_0_policy_lookup(tmp_path):
    """Every spec a model lane prepares has carried `idle_s` since 1.7.0 (supervisor.adapters.process); one that
    does not (impossible for a job still open, by the floor's own liveness argument) now just reads it as `None`
    from the base ProcessAdapter path instead of being moved onto the current policy."""
    import time
    spec = {"root": str(tmp_path), "role": "engineer", "timeout_s": 3600}       # no idle_s: pre-1.7.0 shape
    handle = {"deadline": time.time() + 3600}
    deadline, idle_s, _ = model_limits(REGISTRY["claude_cli"], spec, handle)
    assert idle_s is None and deadline == handle["deadline"]


def test_shim_5_a_pre_1_4_0_question_shaped_decision_item_is_no_longer_specially_recognised():
    item = {"task_id": "T", "kind": "question", "from": "escalation",
           "allowed_responses": list(inbox.DECISION_RESPONSES)}
    assert not inbox.is_decision(item)
    assert inbox.is_decision({"task_id": "T", "kind": inbox.DECISION_KIND})      # the current shape still is


def test_shim_6_a_tag_naming_only_the_pre_2_0_0_contract_files_reads_as_no_contract(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "docs" / "extension_api.json").write_text(json.dumps({"format": 1, "names": {}}), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "pre-2.0.0")
    git("tag", "-a", "v1.0.0", "-m", "1.0.0")
    assert ac.contract_at(repo, "v1.0.0") is None
    assert ac.document_at(repo, "v1.0.0") is None


def test_shim_7_migrate_ownership_is_gone_and_ownership_errors_still_refuses_a_task_with_no_owned_paths(stamped):
    assert not hasattr(tasks, "migrate_ownership")
    r = runner.invoke(app, ["task", "migrate-ownership"])
    assert r.exit_code != 0
    seed = {"tasks": [{"id": "A", "phase": 0, "title": "t", "objective": "o", "owner_role": "engineer",
                       "acceptance": "ok", "cost_category": "claude"}]}      # no owned_paths: a writing lane
    (stamped.root / "seed.yaml").write_text(yaml.safe_dump(seed), encoding="utf-8")
    with pytest.raises(tasks.TaskError, match="owned_paths"):
        tasks.import_seed(stamped, stamped.root / "seed.yaml")


# --------------------------------------------------------------------- the two handlers: an entry point each,
# the generic command path still working

def test_issue_resolve_already_has_a_cli_entry_point_from_phase_6(stamped):
    """Not this task's work (R-21, phase 6) -- confirmed still true before giving `lane.reset` the same shape."""
    make_task(stamped, "T")
    issue = escalation.open_issue(stamped, title="x", severity="high", owner_role="engineer", detail="d",
                                  resolution_task="T", signature="sig")
    r = runner.invoke(app, ["issue", "resolve", issue["id"], "--resolution", "fixed"])
    assert r.exit_code == 0, r.output
    assert store.read(stamped.issues / f"{issue['id']}.yaml")["status"] == "resolved"


def test_lane_reset_now_has_a_cli_entry_point(stamped):
    r = runner.invoke(app, ["lane", "reset", "process"])
    assert r.exit_code == 0, r.output
    assert "reset" in r.output
    r2 = runner.invoke(app, ["lane", "reset", "not-a-real-lane"])
    assert r2.exit_code != 0


def test_the_generic_command_path_still_works_for_both_handlers(stamped):
    out = commands.submit_or_apply(stamped, "lane.reset", {"lane": "process"}, actor="owner")
    assert out.get("ok") and out["result"] == {"cleared": False}

    make_task(stamped, "T")
    issue = escalation.open_issue(stamped, title="x", severity="high", owner_role="engineer", detail="d",
                                  resolution_task="T", signature="sig2")
    out2 = commands.submit_or_apply(stamped, "issue.resolve",
                                    {"issue_id": issue["id"], "resolution": "fixed by hand"}, actor="owner")
    assert out2.get("ok") and out2["result"]["status"] == "resolved"
