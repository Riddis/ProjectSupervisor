"""2.8.0 step 15 (R-25, ATTEMPT_MODEL.md §4.6): the attempt record carried fields nothing wrote, and the task
document carried per-job detail that drifted from the job record it duplicated. Covers: the five removed fields
(`Attempt.id`, `.extra`, `.model`, `.session_id`, `Task.decisions`) have no reader left in the framework package;
a document written before this release still loads (`extra: allow`); `Attempt.outcome` is finally written, by the
code that closes an attempt (`tasks.advance`); the Task page's Model column reads the job record alone, never
the task document; and the attempt-mapping dry run, which walks every task in a store whether or not it has been
migrated, survives an attempt entry that carries no `id` key at all.
"""
from __future__ import annotations

import ast
import json

import pytest

from supervisor import jobs as jobs_mod
from supervisor import migrations as migrations_mod
from supervisor import tasks as tasks_mod
from supervisor.models.task import Attempt, Task, TaskStatus
from tests.conftest import make_task

# The functions R-25 named as the ones with a visible effect: the Task page's Model column (ui/routes/tasks.py),
# the TASKS.md attempts line (render.py) and the diagnosis brief's recent-attempts list (diagnosis.py). Named
# functions, not whole files or their outer callers, because `t.id`/`t.phase` (Task, not Attempt) are legitimate
# neighbours a few lines away in the same files — a whole-file or whole-caller scan for the bare word `id` would
# flag those too.
_PRODUCTION_READERS = {
    "supervisor/render.py": ("_attempt_line",),
    "supervisor/diagnosis.py": ("_attempt_summary",),
    "supervisor/migrations.py": ("_task_notes",),
    "supervisor/ui/routes/tasks.py": ("_job_row", "attempt_groups"),
}

_REMOVED_ATTEMPT_ATTRS = {"id", "extra", "model", "session_id"}


def _attribute_names(tree: ast.AST) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _function_bodies(tree: ast.AST, names: tuple[str, ...]) -> list[ast.FunctionDef]:
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in found}
    assert not missing, f"function(s) not found, rename this test if they moved: {missing}"
    return found


# ------------------------------------------------------------------ the fields themselves are gone from the model


def test_the_five_fields_are_not_declared_any_more():
    assert not _REMOVED_ATTEMPT_ATTRS & set(Attempt.model_fields)
    assert "at" not in Attempt.model_fields                    # renamed to `opened` (§4.6), not kept beside it
    assert "opened" in Attempt.model_fields and "closed" in Attempt.model_fields
    assert "decisions" not in Task.model_fields


# ------------------------------------------------------------------ no reader in the tree references them


@pytest.mark.parametrize("relpath", sorted(_PRODUCTION_READERS))
def test_no_production_reader_references_a_removed_attempt_attribute(relpath):
    import supervisor
    repo_root = __import__("pathlib").Path(supervisor.__file__).resolve().parents[1]
    source = (repo_root / relpath).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relpath)
    for fn in _function_bodies(tree, _PRODUCTION_READERS[relpath]):
        found = _attribute_names(fn) & _REMOVED_ATTEMPT_ATTRS
        assert not found, f"{relpath}::{fn.name} still references {found} on what used to be an Attempt attribute"


def test_task_decisions_is_never_written():
    import supervisor
    repo_root = __import__("pathlib").Path(supervisor.__file__).resolve().parents[1]
    source = (repo_root / "supervisor" / "tasks.py").read_text(encoding="utf-8")
    assert '"decisions": []' not in source
    assert "'decisions': []" not in source


# ------------------------------------------------------------------ a pre-migration document still loads


def test_a_document_written_before_this_release_still_loads():
    """`extra: allow` tolerates the bytes a pre-step-15 writer left on disk: `id`, `at`, `model`, `session_id`
    and `extra` on an attempt, `decisions` on the task — none of them read back through a declared field, all of
    them present without raising."""
    doc = {"id": "T-OLD", "phase": 0, "title": "t", "objective": "o", "owner_role": "engineer",
           "actions": ["a"], "artifacts": ["x"], "tests": ["t"], "acceptance": "ok",
           "escalation": "none", "cost_category": "deterministic", "status": "COMPLETE",
           "decisions": ["some-old-value"],
           "attempts": [{"id": "T-OLD.a1", "resource": "fake_lane", "at": "2026-01-01T00:00:00+00:00",
                         "outcome": "COMPLETE", "note": "dispatch T-OLD.a1", "model": "opus",
                         "session_id": "sess-1", "extra": {"k": "v"}}],
           "job_seq": 1}
    t = Task.model_validate(doc)                      # must not raise
    assert t.id == "T-OLD"
    dumped = t.attempts[0].model_dump()
    assert dumped["id"] == "T-OLD.a1" and dumped["model"] == "opus"   # tolerated as extra, not a declared field
    assert "id" not in Attempt.model_fields


# ------------------------------------------------------------------ Attempt.outcome is written when an attempt closes


def test_outcome_is_accepted_when_a_verified_and_reviewed_attempt_completes(layout):
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks_mod.allocate_job(layout, "A", "delivery", new_attempt=True, cause="scheduled", resource="claude_cli")
    t = tasks_mod.load(layout, "A")
    assert t.attempts[0].outcome == "open"

    tasks_mod.record_delivery(layout, "A", "d1")
    tasks_mod.record_verification(layout, "A", "d1", ok=True)
    assert tasks_mod.advance(layout, "A").status == TaskStatus.WAITING_FOR_REVIEW
    tasks_mod.record_review(layout, "A", "d1", "code_review", "ACCEPT")
    t = tasks_mod.advance(layout, "A")
    assert t.status == TaskStatus.COMPLETE
    assert t.attempts[0].outcome == "accepted"
    assert t.attempts[0].closed is not None


def test_outcome_is_failed_when_verification_fails(layout):
    make_task(layout, "A", status="IN_PROGRESS")
    tasks_mod.allocate_job(layout, "A", "delivery", new_attempt=True, cause="scheduled", resource="claude_cli")
    tasks_mod.record_delivery(layout, "A", "d1")
    tasks_mod.record_verification(layout, "A", "d1", ok=False, detail="tests failed")
    t = tasks_mod.advance(layout, "A")
    assert t.status == TaskStatus.FAILED
    assert t.attempts[0].outcome == "failed"
    assert t.attempts[0].closed is not None


def test_outcome_is_rejected_when_a_review_is_not_accept_and_a_new_attempt_follows(layout):
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks_mod.allocate_job(layout, "A", "delivery", new_attempt=True, cause="scheduled", resource="claude_cli")
    tasks_mod.record_delivery(layout, "A", "d1")
    tasks_mod.record_verification(layout, "A", "d1", ok=True)
    assert tasks_mod.advance(layout, "A").status == TaskStatus.WAITING_FOR_REVIEW
    tasks_mod.record_review(layout, "A", "d1", "code_review", "REVISE")
    t = tasks_mod.advance(layout, "A")
    assert t.status == TaskStatus.IN_PROGRESS
    assert t.attempts[0].outcome == "rejected"
    assert t.attempts[0].closed is not None

    # the revision opens a fresh attempt, independent of the one just closed
    tasks_mod.allocate_job(layout, "A", "delivery", new_attempt=True, cause="revision", resource="claude_cli")
    t = tasks_mod.load(layout, "A")
    assert len(t.attempts) == 2
    assert t.attempts[0].outcome == "rejected"          # the closed one is untouched
    assert t.attempts[1].outcome == "open"              # the fresh one starts open


# ------------------------------------------------------------------ the model is read from the job record alone


def test_the_task_page_model_column_reads_only_the_job_record(layout):
    from supervisor.ui.routes.tasks import attempt_groups
    make_task(layout, "A", status="IN_PROGRESS")
    job_id_obj = tasks_mod.allocate_job(layout, "A", "delivery", new_attempt=True, cause="scheduled",
                                        resource="claude_cli")
    job_id = job_id_obj.id
    jobs_mod.prepare(layout, "A", job_id, "claude_cli", {"adapter": "claude_cli"},
                     kind=job_id_obj.kind, attempt=job_id_obj.attempt, seq=job_id_obj.seq, cause=job_id_obj.cause)
    jobs_mod.set_phase(layout, job_id, "DONE", result="COMPLETE", finished="now", model="opus")
    t = tasks_mod.load(layout, "A")

    groups = attempt_groups(layout, t)
    row = groups[0]["rows"][0]
    assert row["model"] == "opus"                       # read straight off the live job record

    # the stored `jobs[]` entry itself never carries a model at all (ATTEMPT_MODEL.md §4.6) — only the live job
    # record does, which is exactly what the assertion above already depends on
    assert "model" not in t.attempts[0].jobs[0]


# ------------------------------------------------------------------ the mapping dry run over an allocator-written store


def test_the_attempt_mapping_dry_run_reads_an_attempt_that_has_no_id(layout, tmp_path):
    """The attempt-mapping dry run (ATTEMPT_MODEL.md §8) walks every task in the store, with no filter for the
    shape its attempts are in, and its per-job note map is the last reader that joined on `Attempt.id`. An
    allocator-written entry (§2.7) carries no `id` key at all, so that map has to read the stored dump: through a
    declared field the read raises instead of finding nothing, and the whole dry run dies on it."""
    make_task(layout, "A", status="IN_PROGRESS")
    job = tasks_mod.allocate_job(layout, "A", "delivery", new_attempt=True, cause="scheduled",
                                 resource="claude_cli")
    jobs_mod.prepare(layout, "A", job.id, "claude_cli", {"adapter": "claude_cli"},
                     kind=job.kind, attempt=job.attempt, seq=job.seq, cause=job.cause)
    t = tasks_mod.load(layout, "A")
    assert "id" not in t.attempts[0].model_dump()           # the allocator never writes one

    assert migrations_mod._task_notes(t) == {}               # nothing to join on, and nothing raised

    result = migrations_mod.dry_run_attempts(layout, tmp_path / "out")
    assert result["totals"]["records"] == 1
    mapping = json.loads((tmp_path / "out" / "mapping.json").read_text(encoding="utf-8"))
    assert mapping[job.id]["kind"] == "delivery"
