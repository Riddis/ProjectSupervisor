"""1.13.0, a skill-trial finding (supervisor-setup, round 2): in a fresh project every task with a pytest check failed
verification. Verification gives each pytest run its own `--basetemp=runs/pytest_tmp/<tag>`, and nothing created
`runs/pytest_tmp/`; pytest creates the basetemp itself but not a missing parent, so the run errored before any test.
The parent is now created first."""
from __future__ import annotations

from supervisor import verify


def test_a_pytest_check_runs_in_a_project_without_runs_pytest_tmp(tmp_path):
    root = tmp_path / "fresh"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    assert not (root / "runs").exists()
    ok, results = verify.run_declared_tests(root, [{"command": "python -m pytest -q -p no:cacheprovider tests"}],
                                            allowed=["python -m pytest"])
    assert ok, results
    assert (root / "runs" / "pytest_tmp").is_dir()
