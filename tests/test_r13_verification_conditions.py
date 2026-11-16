"""R-13 (`docs/reviews/FRAMEWORK_REVIEW_2026-11-14.md`), three of its five parts: allow-list spelling, the
environment a declared command runs under, and the scratch directory every verification leaves behind. (Evidence
mode and PATH/shell resolution are a later release.)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from supervisor import verify


def test_a_python_dash_m_spelling_is_accepted_when_the_bare_form_is_allowed():
    assert verify.permitted(f"{sys.executable} -m ruff check .", ["ruff check"]) is True
    assert verify.permitted(f"{sys.executable} -m pytest tests", ["pytest"]) is True


def test_the_bare_spelling_is_accepted_when_the_python_dash_m_form_is_allowed():
    assert verify.permitted("ruff check .", ["python -m ruff check"]) is True
    assert verify.permitted("pytest tests", ["python -m pytest"]) is True


def test_a_different_module_is_still_refused():
    assert verify.permitted(f"{sys.executable} -m pytest tests", ["ruff check"]) is False
    assert verify.permitted(f"{sys.executable} -m pip install x", ["pytest"]) is False


def test_shell_operators_are_still_refused_whatever_the_spelling():
    assert verify.permitted("pytest tests; rm -rf .", ["pytest"]) is False
    assert verify.permitted(f"{sys.executable} -m pytest tests && echo done", ["python -m pytest"]) is False


def test_a_declared_command_sees_none_of_the_job_control_variables(layout, monkeypatch):
    """`layout` already sets SUPERVISOR_ROOT (conftest.py); a dispatched job also carries SUPERVISOR_ATTEMPT and
    SUPERVISOR_RUN_DIR (adapters/process.py) — none of these are a declared command's business (T3-06.a9)."""
    monkeypatch.setenv("SUPERVISOR_ATTEMPT", "T1.a1")
    monkeypatch.setenv("SUPERVISOR_RUN_DIR", str(layout.root / "runs" / "T1" / "T1.a1"))
    script = layout.root / "check_env.py"
    script.write_text(
        "import os, sys\n"
        "names = ('SUPERVISOR_ATTEMPT', 'SUPERVISOR_RUN_DIR', 'SUPERVISOR_ROOT', 'SUPERVISOR_PROGRESS_FILE')\n"
        "leaked = sorted(k for k in names if k in os.environ)\n"
        "sys.exit(1 if leaked else 0)\n", encoding="utf-8")
    command = f"{sys.executable} check_env.py"
    ok, results = verify.run_declared_tests(layout.root, [{"command": command}], allowed=["python check_env.py"])
    assert ok is True, results


def test_the_report_names_the_interpreter_cwd_and_path_a_command_ran_with(layout):
    (layout.root / "test_ok.py").write_text("def test_ok(tmp_path):\n    assert tmp_path.exists()\n", encoding="utf-8")
    command = f"{sys.executable} -m pytest -q -p no:cacheprovider test_ok.py"
    ok, results = verify.run_declared_tests(layout.root, [{"command": command}], allowed=["python -m pytest"])
    assert ok, results
    obs = results[0]
    assert obs["interpreter"] == sys.executable
    assert obs["cwd"] == str(layout.root)
    assert obs["path"] == os.environ.get("PATH", "")


def test_a_passing_verification_leaves_no_directory_under_the_scratch_parent(layout):
    (layout.root / "test_ok.py").write_text("def test_ok(tmp_path):\n    assert tmp_path.exists()\n", encoding="utf-8")
    command = f"{sys.executable} -m pytest -q -p no:cacheprovider test_ok.py"
    ok, results = verify.run_declared_tests(layout.root, [{"command": command}], allowed=["python -m pytest"])
    assert ok, results
    scratch_parent = layout.root / "runs" / "pytest_tmp"
    assert scratch_parent.is_dir()
    assert list(scratch_parent.iterdir()) == []


def test_a_failing_verification_keeps_its_scratch_directory_and_names_it(layout):
    (layout.root / "test_fail.py").write_text("def test_no(tmp_path):\n    assert False\n", encoding="utf-8")
    command = f"{sys.executable} -m pytest -q -p no:cacheprovider test_fail.py"
    ok, results = verify.run_declared_tests(layout.root, [{"command": command}], allowed=["python -m pytest"])
    assert ok is False
    scratch_parent = layout.root / "runs" / "pytest_tmp"
    kept = list(scratch_parent.iterdir())
    assert len(kept) == 1 and kept[0].is_dir()
    assert results[0].get("basetemp") and Path(results[0]["basetemp"]) == kept[0]
