"""supervisor/templates.py: the loader and validator for `config/job_templates.yaml` (RECOVERY_PLAN.md section
3.2, D-027 decision 8). The framework ships none of its own; a project's file is validated whole, never partially,
and the shell-argv rule of section 1.4 is enforced here, before any job block is ever checked against it."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from supervisor import templates


def _write(layout, data):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


# ----------------------------------------------------------------------- the framework ships none of its own

def test_the_framework_ships_no_job_templates():
    import supervisor
    pkg_root = Path(supervisor.__file__).resolve().parent.parent
    assert not (pkg_root / "config" / "job_templates.yaml").exists()


# ----------------------------------------------------------------------- the loader

def test_no_file_loads_no_templates(layout):
    assert templates.load(layout) == {}


def test_a_valid_template_loads(layout):
    _write(layout, {"version": 1, "templates": {"t": {
        "adapter": "process", "argv": [sys.executable, "-c", "pass"], "may_be_set_by": ["owner"], "params": {}}}})
    out = templates.load(layout)
    assert out["t"]["adapter"] == "process"


def test_malformed_yaml_refuses_the_whole_file(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text("templates: [this is not a mapping", encoding="utf-8")
    with pytest.raises(templates.TemplateError):
        templates.load(layout)


def test_process_template_without_argv_is_refused(layout):
    _write(layout, {"version": 1, "templates": {"t": {"adapter": "process", "may_be_set_by": ["owner"]}}})
    with pytest.raises(templates.TemplateError):
        templates.load(layout)


def test_python_template_without_fn_is_refused(layout):
    _write(layout, {"version": 1, "templates": {"t": {"adapter": "python", "may_be_set_by": ["owner"]}}})
    with pytest.raises(templates.TemplateError):
        templates.load(layout)


# ----------------------------------------------------------------------- the shell-argv rule (section 1.4)

def test_shell_argv0_refused_when_not_owner_only(layout):
    _write(layout, {"version": 1, "templates": {"t": {
        "adapter": "process", "argv": ["bash", "-c", "{cmd}"], "may_be_set_by": ["plan"],
        "params": {"cmd": {"type": "str"}}}}})
    with pytest.raises(templates.TemplateError, match="shell"):
        templates.load(layout)


def test_shell_argv0_refused_even_when_owner_and_plan_both_listed(layout):
    _write(layout, {"version": 1, "templates": {"t": {
        "adapter": "process", "argv": ["cmd", "/c", "{cmd}"], "may_be_set_by": ["owner", "plan"],
        "params": {"cmd": {"type": "str"}}}}})
    with pytest.raises(templates.TemplateError, match="shell"):
        templates.load(layout)


def test_shell_argv0_allowed_when_owner_only(layout):
    _write(layout, {"version": 1, "templates": {"t": {
        "adapter": "process", "argv": ["bash", "-c", "{cmd}"], "may_be_set_by": ["owner"],
        "params": {"cmd": {"type": "str"}}}}})
    out = templates.load(layout)
    assert out["t"]["argv"][0] == "bash"


def test_shell_basename_is_recognised_under_a_path_and_an_exe_suffix(layout):
    assert templates.is_shell(r"C:\Windows\System32\cmd.exe")
    assert templates.is_shell("/bin/bash")
    assert not templates.is_shell(sys.executable)
