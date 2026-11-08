"""2.4.0 G1: `Task.evidence` separates "what a reviewer judges" (`tests`, prose) from "must the delivery report a
command". Invented task ids and paths throughout."""
import sys

from supervisor import packets, tasks, verify
from supervisor.adapters import python as pyad
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def test_requires_commands_default_auto_matches_the_pre_2_4_0_rule(layout):
    make_task(layout, "T", cost_category="claude")           # make_task's default tests=["t"]
    task = tasks.load(layout, "T")
    assert task.evidence == "auto"
    assert task.requires_commands(model_lane=True) is True
    assert task.requires_commands(model_lane=False) is False


def test_a_task_with_tests_and_default_evidence_still_fails_without_commands_on_a_model_lane(layout):
    make_task(layout, "T", cost_category="claude")
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, [], declared_tests=[], require_tests=task.requires_commands(True))
    assert outcome.ok is False and "no test evidence" in outcome.detail


def test_evidence_review_needs_no_command_and_tests_empty_verifies_green(layout):
    make_task(layout, "T", evidence="review", cost_category="claude")   # tests still ["t"] by default
    task = tasks.load(layout, "T")
    assert task.requires_commands(model_lane=True) is False
    outcome = verify.verify(layout, task, [], declared_tests=[], require_tests=task.requires_commands(True))
    assert outcome.ok is True


def test_evidence_review_still_fails_on_a_reported_command_that_fails(layout):
    make_task(layout, "T", evidence="review", cost_category="claude")
    task = tasks.load(layout, "T")
    (layout.root / "t_fail.py").write_text("def test_no():\n    assert False\n", encoding="utf-8")
    failing = [{"command": f"{sys.executable} -m pytest -p no:cacheprovider t_fail.py", "exit_code": 0}]
    outcome = verify.verify(layout, task, [], declared_tests=failing, require_tests=task.requires_commands(True))
    assert outcome.ok is False


def test_evidence_commands_requires_a_command_even_on_a_deterministic_lane_with_no_tests(layout):
    make_task(layout, "T", evidence="commands", tests=[])    # deterministic cost_category, make_task's default
    task = tasks.load(layout, "T")
    assert task.requires_commands(model_lane=False) is True
    outcome = verify.verify(layout, task, [], declared_tests=[], require_tests=task.requires_commands(False))
    assert outcome.ok is False and "no test evidence" in outcome.detail


@pyad.job("g1_write_widget")
def _write_widget(root, run_dir, **_):
    (root / "widget.txt").write_text("ok")
    return {"status": "COMPLETE", "artifacts": ["widget.txt"]}


def test_evidence_commands_failure_names_the_evidence_choice_not_prose_tests(layout):
    """F2: under `evidence: commands` with `tests: []` the task declares no tests at all - the failure must not
    claim it does, and must instead name the planner's `evidence` choice that actually demands the command."""
    make_task(layout, "T", evidence="commands", tests=[])
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, [], declared_tests=[], require_tests=task.requires_commands(False))
    assert outcome.ok is False
    assert outcome.detail.startswith("no test evidence")
    assert "the task declares tests" not in outcome.detail
    assert "evidence: commands" in outcome.detail


def test_model_lane_default_evidence_failure_still_names_the_true_reason(layout):
    """The one case where "the task declares tests" is actually true: prose tests on a model lane, default
    (`auto`) evidence. F2's reword must keep this sentence true here, and must not mention `evidence: commands`."""
    make_task(layout, "T", cost_category="claude")           # evidence="auto" (default), tests=["t"] (the default)
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, [], declared_tests=[], require_tests=task.requires_commands(True))
    assert outcome.ok is False
    assert outcome.detail.startswith("no test evidence")
    assert "declares tests" in outcome.detail
    assert "evidence: commands" not in outcome.detail


def test_loop_honours_evidence_commands_on_a_deterministic_lane(layout):
    """loop.ingest must pass `task.requires_commands(model_lane)` to verify(), not the old inferred rule: a
    deterministic job with `evidence: commands` and no declared tests must not complete without a command."""
    make_task(layout, "T", evidence="commands", tests=[], cost_category="deterministic")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "g1_write_widget"}
        doc["artifacts"] = ["widget.txt"]
    tasks._update(layout, "T", fn)
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                      # dispatch + ingest: a deterministic lane completes synchronously
    assert tasks.load(layout, "T").status != TaskStatus.COMPLETE


def test_brief_names_command_evidence_as_required_when_the_task_declares_tests(layout):
    make_task(layout, "T", cost_category="claude")
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert "## Tests (at least one of these must be a command the supervisor can re-run)" in brief
    assert "This task needs command evidence" in brief


def test_brief_names_no_command_required_under_evidence_review(layout):
    make_task(layout, "T", evidence="review", cost_category="claude")
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert "## Tests (judged by reading the artifacts; no command is required)" in brief
    assert "No command is required for this task" in brief
