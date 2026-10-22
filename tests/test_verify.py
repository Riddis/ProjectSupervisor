import sys

from supervisor import tasks, verify
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def test_containment_violations_empty_when_unrestricted(layout):
    assert verify.containment_violations(["a/b.txt"], [], layout.root) == []


def test_containment_violations_flags_paths_outside_owned_roots(layout):
    (layout.root / "a").mkdir()
    (layout.root / "b").mkdir()
    assert verify.containment_violations(["a/x.txt", "b/y.txt"], ["a"], layout.root) == ["b/y.txt"]


def test_verify_fails_on_write_outside_owned_paths(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt", "elsewhere/leak.txt"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    (layout.root / "owned" / "out.txt").write_text("ok")
    (layout.root / "elsewhere").mkdir()
    (layout.root / "elsewhere" / "leak.txt").write_text("sneaky")
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, ["owned/out.txt", "elsewhere/leak.txt"])
    assert outcome.ok is False and "elsewhere/leak.txt" in outcome.detail


def test_verify_passes_when_everything_stays_inside_owned_paths(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    (layout.root / "owned" / "out.txt").write_text("ok")
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    outcome = verify.verify(layout, task, ["owned/out.txt"], before=before)
    assert outcome.ok is True


def test_undeclared_change_inside_owned_paths_fails_verification(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("declared")
    (layout.root / "owned" / "side_effect.txt").write_text("not declared")
    outcome = verify.verify(layout, task, ["owned/out.txt"], before=before)
    assert outcome.ok is False and "side_effect.txt" in outcome.detail


def _pytest_file(layout, name: str, body: str) -> str:
    """A real (tiny) pytest file the verifier can re-run, so the test exercises the allow-listed lane."""
    p = layout.root / name
    p.write_text(body, encoding="utf-8")
    return f"{sys.executable} -m pytest -p no:cacheprovider {name}"


def test_declared_tests_are_actually_re_run_not_trusted(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    passing = [{"command": _pytest_file(layout, "t_pass.py", "def test_ok():\n    assert True\n"), "exit_code": 0}]
    outcome = verify.verify(layout, task, [], declared_tests=passing)
    assert outcome.ok is True and outcome.report["tests"][0]["exit_code"] == 0

    # self-reports 0, really fails
    lying = [{"command": _pytest_file(layout, "t_fail.py", "def test_no():\n    assert False\n"), "exit_code": 0}]
    outcome = verify.verify(layout, task, [], declared_tests=lying)
    assert outcome.ok is False and "declared test failed" in outcome.detail


def test_a_command_outside_the_allow_list_is_a_failure_and_is_never_executed(layout):
    """Declared commands come from model output: verification refuses anything that is not a test runner instead of
    handing it to a shell in the supervisor process."""
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    marker = layout.root / "pwned.txt"
    command = f"{sys.executable} -c \"open(r'{marker}','w').write('x')\""
    outcome = verify.verify(layout, task, [], declared_tests=[{"command": command, "exit_code": 0}])
    assert outcome.ok is False and "allowed_test_commands" in outcome.detail
    assert not marker.exists()


def test_permitted_matches_an_interpreter_by_basename(layout):
    allowed = verify.ALLOWED_TEST_COMMANDS
    assert verify.permitted(r'"C:\x\.venv\Scripts\python.exe" -m pytest tests', allowed) is True
    assert verify.permitted("pytest tests/test_verify.py", allowed) is True
    assert verify.permitted("python -m pytest tests && curl http://x", allowed) is False
    assert verify.permitted("", allowed) is False


def test_a_blank_or_malformed_test_entry_fails_instead_of_being_skipped(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, [], declared_tests=[{"command": "   ", "exit_code": 0}, {}])
    assert outcome.ok is False and "malformed test entry" in str(outcome.report["tests"])


def test_an_unclosed_quote_fails_verification_instead_of_crashing_it(layout):
    """shlex.split() raises on an unclosed quote; that call sat outside the exception
    handler and used to propagate out of verify(), killing the whole loop iteration instead of failing just
    this delivery's test evidence."""
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, [], declared_tests=[{"command": 'python -m pytest "unterminated', "exit_code": 0}])
    assert outcome.ok is False and "unparsable command" in str(outcome.report["tests"])


def test_bare_pytest_and_ruff_are_pinned_to_the_supervisor_interpreter(layout):
    """Allow-listed bare runners used to execute whatever is first on PATH, unlike the python/py entries, which
    were already pinned."""
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    passing = [{"command": _pytest_file(layout, "t_bare.py", "def test_ok():\n    assert True\n")
               .replace(f"{sys.executable} -m pytest", "pytest"), "exit_code": 0}]
    outcome = verify.verify(layout, task, [], declared_tests=passing)
    assert outcome.ok is True and outcome.report["tests"][0]["exit_code"] == 0


def test_a_task_that_declares_tests_is_not_verified_without_test_evidence(layout):
    make_task(layout, "T")                      # conftest tasks declare tests: ["t"]
    task = tasks.load(layout, "T")
    assert verify.verify(layout, task, [], declared_tests=[], require_tests=True).ok is False
    assert verify.verify(layout, task, [], declared_tests=[], require_tests=False).ok is True


def test_undeclared_write_outside_owned_paths_fails_even_when_not_declared(layout):
    """The containment gap code_review found: an agent that simply omits the file from `artifacts` used to be
    invisible, because both manifests were scoped to owned_paths."""
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    (layout.root / "elsewhere").mkdir()
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("declared")
    (layout.root / "elsewhere" / "leak.txt").write_text("never declared")
    outcome = verify.verify(layout, task, ["owned/out.txt"], before=before)
    assert outcome.ok is False and "elsewhere/leak.txt" in outcome.detail


def test_concurrent_work_in_another_tasks_owned_paths_is_not_this_deliverys_fault(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    (layout.root / "other").mkdir()
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("declared")
    (layout.root / "other" / "theirs.txt").write_text("another task's job")
    assert verify.verify(layout, task, ["owned/out.txt"], before=before).ok is False
    assert verify.verify(layout, task, ["owned/out.txt"], before=before, ignore=("other",)).ok is True


def test_excluded_trees_are_not_evidence_about_the_agent(layout):
    """The supervisor rewrites state/, runs/ and the rendered views while an attempt runs."""
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["owned"]
    tasks._update(layout, "T", fn)
    (layout.root / "owned").mkdir()
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)
    (layout.root / "runs" / "noise.txt").write_text("run dir churn")
    (layout.root / "state" / "noise.yaml").write_text("x: 1")
    (layout.root / "TASKS.md").write_text("rendered")
    assert verify.verify(layout, task, [], before=before).ok is True


def test_a_link_or_escaping_owned_path_is_a_failure_not_a_raised_valueerror(layout):
    """contract.manifest() raises on links and on paths escaping the root; snapshot() is called from dispatch and
    verify() from ingest, where a raise would stop the supervisor instead of failing the task."""
    make_task(layout, "T")
    def fn(doc):
        doc["owned_paths"] = ["../outside-the-repo"]
    tasks._update(layout, "T", fn)
    task = tasks.load(layout, "T")
    before = verify.snapshot(layout, task)                 # must not raise
    assert "escapes root" in before["error"]
    outcome = verify.verify(layout, task, [], before=before)
    assert outcome.ok is False and "snapshot failed" in outcome.detail


def test_reviewer_empty_and_human_required_waits_for_human_after_verification(layout):
    """Acceptance test from T0-10: a task with reviewer=[] and human_approval=required stays WAITING_FOR_HUMAN
    after a passing verification (tasks._evaluate / advance(), not new code — confirming the existing contract)."""
    make_task(layout, "T", reviewer=(), human="required")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.record_delivery(layout, "T", "d" * 64, actor="test", paths=["x"])
    tasks.record_verification(layout, "T", "d" * 64, True, detail="ok")
    t = tasks.advance(layout, "T")
    assert t.status == TaskStatus.WAITING_FOR_HUMAN
