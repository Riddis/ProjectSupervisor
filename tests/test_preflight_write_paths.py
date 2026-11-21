"""The preflight runs whenever a job block is written -- by `task.create`, a patch, `task.set_job`, or a plan
(RECOVERY_PLAN.md section 3.3) -- never only at the gate. One test per writer, each exercising a template-based
job so a wrong parameter is caught at the write itself rather than at the first launch."""
from __future__ import annotations

import sys

import yaml

from supervisor import commands, plans, tasks as tasks_mod
from tests.conftest import make_task

PY = sys.executable


def _write_templates(layout, templates: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def _good_template(**extra):
    out = {"adapter": "process", "argv": [PY, "-c", "pass", "{path}"], "may_be_set_by": ["owner", "plan"],
           "params": {"path": {"type": "path", "roots": ["."], "must_exist": True}}}
    out.update(extra)
    return out


def test_create_attaches_a_preflight(layout):
    (layout.root / "ok.txt").write_text("x", encoding="utf-8")
    _write_templates(layout, {"t": _good_template()})
    tasks_mod.create(layout, {"id": "T1", "phase": 0, "title": "t", "objective": "o", "owner_role": "engineer",
                              "artifacts": ["x"], "cost_category": "deterministic",
                              "job": {"adapter": "process", "template": "t", "params": {"path": "ok.txt"}}},
                     actor="owner")
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is not None and t.job.preflight.ok is True


def test_create_records_a_failed_preflight_rather_than_refusing_the_create(layout):
    """A create is not refused by a failed preflight (RECOVERY_PLAN.md section 3.3 binds the launch, not the
    write); the gate is where a bad job block stops, so the record is visible instead of lost."""
    _write_templates(layout, {"t": _good_template()})
    tasks_mod.create(layout, {"id": "T1", "phase": 0, "title": "t", "objective": "o", "owner_role": "engineer",
                              "artifacts": ["x"], "cost_category": "deterministic",
                              "job": {"adapter": "process", "template": "t", "params": {"path": "missing.txt"}}},
                     actor="owner")
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight.ok is False
    assert t.job.preflight.detail["code"] == "path_missing"


def test_patch_task_re_runs_the_preflight(layout):
    (layout.root / "ok.txt").write_text("x", encoding="utf-8")
    _write_templates(layout, {"t": _good_template()})
    make_task(layout, "T1", job={"adapter": "process", "template": "t", "params": {"path": "ok.txt"}})
    tasks_mod.patch_task(layout, "T1", {"job": {"adapter": "process", "template": "t", "params": {"path": "missing.txt"}}},
                         actor="owner")
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight.ok is False
    assert t.job.preflight.detail["code"] == "path_missing"


def test_task_set_job_command_re_runs_the_preflight(layout):
    (layout.root / "ok.txt").write_text("x", encoding="utf-8")
    _write_templates(layout, {"t": _good_template()})
    make_task(layout, "T1")
    commands.apply_job(layout, "T1", {"adapter": "process", "template": "t", "params": {"path": "ok.txt"}}, "owner")
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is not None and t.job.preflight.ok is True


def test_a_plans_set_job_effect_re_runs_the_preflight(layout):
    (layout.root / "ok.txt").write_text("x", encoding="utf-8")
    _write_templates(layout, {"t": _good_template()})
    make_task(layout, "T1", job={"adapter": "process", "template": "t", "params": {"path": "ok.txt"}})
    plans._effect_set_job(layout, {"op": "set_job", "task": "T1", "template": "t", "params": {"path": "missing.txt"}},
                          {"subject": {"task": "T1"}})
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight.ok is False
    assert t.job.preflight.detail["code"] == "path_missing"
