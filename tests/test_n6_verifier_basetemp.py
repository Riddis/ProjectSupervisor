"""N6 (2.5.0): `test_argv` left the quotes on a `--basetemp` option's own value, and `_isolate_pytest` honoured an
agent-supplied `--basetemp` instead of always replacing it. A worker's declared command re-ran inside the agent's
own evidence directory, which pytest clears on use - the agent always supplies its own `--basetemp` (the vendored
handoff text, unchanged by this fix); the old guard only filled one in when none was declared."""
from __future__ import annotations

from supervisor import verify


def test_a_quoted_basetemp_value_reaches_argv_with_no_quotes():
    """`shlex.split(posix=False)` leaves `--basetemp="…"` as one token whose own first character is `-`, not a
    quote, so the old code's whole-token check (`t[0] == t[-1]`) never matched it and the inner quotes reached
    pytest's own argv, illegal in a Windows directory name."""
    argv = verify.test_argv('python -m pytest tests --basetemp="D:/work/widget-lib/runs/pytest_tmp"')
    assert argv[-1] == "--basetemp=D:/work/widget-lib/runs/pytest_tmp"
    assert '"' not in argv[-1]


def test_a_single_quoted_basetemp_value_is_also_unquoted():
    argv = verify.test_argv("pytest --basetemp='D:/work/widget-lib/runs/pytest_tmp'")
    assert argv[-1] == "--basetemp=D:/work/widget-lib/runs/pytest_tmp"
    assert "'" not in argv[-1]


def test_an_ordinary_quoted_path_argument_is_still_unquoted_whole():
    """The existing whole-token behaviour (quotes around an entire argument, no `=`) is unchanged."""
    argv = verify.test_argv('pytest "D:/a b/tests"')
    assert argv[-1] == "D:/a b/tests"


def test_isolate_pytest_replaces_an_agent_supplied_basetemp_equals_form(tmp_path):
    """The old guard: `not any(a.startswith("--basetemp") for a in argv)` - present, so it never touched an
    agent-supplied one. The agent's own evidence directory must never be the verifier's scratch (it is cleared on
    use), so the verifier's own basetemp must win unconditionally."""
    argv = ["pytest", "tests", "--basetemp=runs/T1/T1.a3/pytest_tmp"]
    out = verify._isolate_pytest(argv, tmp_path, "verify_T1")
    assert not any(a.startswith("--basetemp=runs/T1/T1.a3") for a in out)
    assert sum(a.startswith("--basetemp=") for a in out) == 1
    assert out[-1] == f"--basetemp={(tmp_path / 'runs' / 'pytest_tmp' / 'verify_T1').as_posix()}"


def test_isolate_pytest_replaces_an_agent_supplied_basetemp_space_form(tmp_path):
    """The two-token form (`--basetemp X`) must also be stripped, not only `--basetemp=X`."""
    argv = ["pytest", "tests", "--basetemp", "runs/T1/T1.a3/pytest_tmp"]
    out = verify._isolate_pytest(argv, tmp_path, "verify_T1")
    assert "runs/T1/T1.a3/pytest_tmp" not in out
    assert "--basetemp" not in out[:-1]
    assert sum(a.startswith("--basetemp=") for a in out) == 1


def test_isolate_pytest_still_fills_in_a_missing_basetemp(tmp_path):
    """Unchanged: a command with no basetemp at all still gets one."""
    out = verify._isolate_pytest(["pytest", "tests"], tmp_path, "verify_T1")
    assert out[-1] == f"--basetemp={(tmp_path / 'runs' / 'pytest_tmp' / 'verify_T1').as_posix()}"


def test_agents_own_basetemp_cannot_defeat_verification_isolation(tmp_path):
    """End to end: a declared command re-runs under the verifier's own basetemp even when the agent declared its
    own evidence directory as `--basetemp`, and the agent's directory is left untouched."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    agent_dir = tmp_path / "runs" / "T1" / "T1.a3" / "pytest_tmp"
    agent_dir.mkdir(parents=True)
    (agent_dir / "sentinel.txt").write_text("the agent's own evidence", encoding="utf-8")
    ok, results = verify.run_declared_tests(
        tmp_path, [{"command": f'python -m pytest -q -p no:cacheprovider tests --basetemp="{agent_dir}"'}],
        allowed=["python -m pytest"])
    assert ok, results
    assert (agent_dir / "sentinel.txt").read_text(encoding="utf-8") == "the agent's own evidence"
    assert (tmp_path / "runs" / "pytest_tmp").is_dir()
