"""2.4.0 G2: a declared command's `cwd` must lie inside the task's own `test_roots`, and the interpreter is chosen
by the verifier (that directory's `.venv` if it has one, else the supervisor's own) — never by what the command's
own argv[0] spells. Invented directories throughout (D-005): "widget-lib" is not a real project."""
import os
import sys
import types
from pathlib import Path

from supervisor import packets, tasks, verify
from tests.conftest import make_task


def _other_checkout(tmp_path) -> Path:
    other = tmp_path.parent / "widget-lib"
    other.mkdir(exist_ok=True)
    (other / "tests").mkdir(exist_ok=True)
    (other / "tests" / "test_widget.py").write_text("def test_only_here():\n    assert True\n", encoding="utf-8")
    return other


def test_a_command_with_cwd_inside_test_roots_runs_there(layout, tmp_path):
    other = _other_checkout(tmp_path)
    command = f"{sys.executable} -m pytest -p no:cacheprovider tests/test_widget.py"
    ok, results = verify.run_declared_tests(layout.root, [{"command": command, "cwd": str(other)}],
                                            roots=(str(other),))
    assert ok is True and results[0]["exit_code"] == 0


def test_the_same_command_without_cwd_fails_as_it_does_today(layout, tmp_path):
    other = _other_checkout(tmp_path)
    command = f"{sys.executable} -m pytest -p no:cacheprovider tests/test_widget.py"
    ok, results = verify.run_declared_tests(layout.root, [{"command": command}], roots=(str(other),))
    assert ok is False


def test_a_cwd_outside_test_roots_is_refused_and_never_run(layout, tmp_path):
    outside = tmp_path.parent / "not-allowed"
    outside.mkdir(exist_ok=True)
    marker = outside / "ran.txt"
    command = f"{sys.executable} -m pytest --version"       # an allowed command; only its cwd is the problem
    ok, results = verify.run_declared_tests(layout.root, [{"command": command, "cwd": str(outside)}], roots=())
    assert ok is False
    assert "test_roots" in results[0]["error"]
    assert str(layout.root) in results[0]["error"]
    assert results[0]["exit_code"] is None
    assert not marker.exists()


def test_cwd_venv_interpreter_is_preferred_when_present(tmp_path):
    cwd = tmp_path / "proj"
    venv_bin = cwd / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    py = venv_bin / ("python.exe" if os.name == "nt" else "python")
    py.write_text("")
    assert verify._cwd_interpreter(cwd) == str(py)


def test_cwd_without_its_own_venv_falls_back_to_the_supervisors_interpreter(tmp_path):
    cwd = tmp_path / "proj2"
    cwd.mkdir()
    assert verify._cwd_interpreter(cwd) == sys.executable


def test_brief_names_the_allowed_test_roots_when_set(layout):
    make_task(layout, "T", test_roots=["C:/work/widget-lib"])
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert "C:/work/widget-lib" in brief and '"cwd"' in brief
    assert "Where your test commands re-run" in brief
    assert f"from `{layout.root}` by default" in brief
    assert "<project root>" not in brief


def test_brief_states_the_project_root_only_rule_when_test_roots_is_empty(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert f"Verification re-runs each command from `{layout.root}` with the supervisor's own interpreter." in brief
    assert "<project root>" not in brief


def test_no_cwd_runs_under_the_supervisors_own_interpreter_even_when_the_root_has_a_venv(layout, monkeypatch):
    """Ruling §3, resolution step 1: "No `cwd` in the entry -> cwd = root, interpreter sys.executable. Unchanged
    from today." A project root that owns its own `.venv` must not change which interpreter an entry without
    `cwd` runs under (the worktree's own `_cwd_interpreter()` test above only exercises a bare directory)."""
    venv_py = layout.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(verify.subprocess, "run", fake_run)

    verify.run_declared_tests(layout.root, [{"command": "pytest -q"}, {"command": "python -m pytest -q"}])

    assert [c[0] for c in calls] == [sys.executable, sys.executable]


def test_a_contract_check_without_cwd_also_runs_under_the_supervisors_own_interpreter(layout, monkeypatch):
    """The same rule for the task's own `checks` (loop.py passes `checks=[c.model_dump() for c in task.checks]`,
    so an unset `cwd` arrives as the key `cwd: None`, not a missing key)."""
    venv_py = layout.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    make_task(layout, "T", checks=[{"command": "pytest -q"}])
    task = tasks.load(layout, "T")
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(verify.subprocess, "run", fake_run)

    outcome = verify.verify(layout, task, [], checks=[c.model_dump() for c in task.checks])

    assert calls and calls[0][0] == sys.executable
    assert outcome.ok
