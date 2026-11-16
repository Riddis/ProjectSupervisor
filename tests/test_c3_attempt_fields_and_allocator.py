"""C-3 (and its migration prerequisites): every job id used to be named by counting files, in two places, so a
lost job record could make the next dispatch reuse an existing id (R-03's root cause reopened for every new
kind) and an attempt number meant nothing. Step 1 of the fix: the fields the rest of the release keys on, and
one allocator that mints a job's identity from a monotonic counter block kept in the task document, under the
task lock. Nothing calls it yet — this file is the proof that it works before anything is wired to it.
"""
from __future__ import annotations

import pytest

from supervisor import jobs as jobs_mod
from supervisor import tasks as tasks_mod
from supervisor.models.task import Task
from tests.conftest import make_task


def test_allocate_job_never_repeats_an_id_across_kinds_and_attempts(layout):
    make_task(layout, "T-1", status="IN_PROGRESS")

    delivery1 = tasks_mod.allocate_job(layout, "T-1", "delivery", attempt=1, cause="scheduled")
    verify1 = tasks_mod.allocate_job(layout, "T-1", "verify", attempt=1, cause="scheduled")
    review1 = tasks_mod.allocate_job(layout, "T-1", "review", attempt=1, cause="scheduled")
    diag1 = tasks_mod.allocate_job(layout, "T-1", "diagnosis", attempt=1, cause="diagnose")
    diag_task_scoped = tasks_mod.allocate_job(layout, "T-1", "diagnosis", cause="diagnose")
    delivery2 = tasks_mod.allocate_job(layout, "T-1", "delivery", attempt=2, cause="revision")

    allocated = [delivery1, verify1, review1, diag1, diag_task_scoped, delivery2]
    ids = [a.id for a in allocated]
    assert len(ids) == len(set(ids)), f"an id was repeated: {ids}"

    assert delivery1.id == "T-1.a1.s1"
    assert verify1.id == "T-1.a1.v1"
    assert review1.id == "T-1.a1.r1"
    assert diag1.id == "T-1.a1.d1"
    assert diag_task_scoped.id == "T-1.d1" and diag_task_scoped.attempt is None and diag_task_scoped.scope == "task"
    assert delivery2.id == "T-1.a2.s1"

    # a second job of the same kind inside the same attempt advances the seq, not the id shape
    verify1b = tasks_mod.allocate_job(layout, "T-1", "verify", attempt=1, cause="reverify")
    assert verify1b.id == "T-1.a1.v2"
    assert verify1b.id not in ids


def test_a_lost_job_record_between_two_allocations_cannot_cause_a_reuse(layout):
    make_task(layout, "T-2", status="IN_PROGRESS")

    first = tasks_mod.allocate_job(layout, "T-2", "delivery", attempt=1, cause="scheduled")
    jobs_mod.prepare(layout, "T-2", first.id, "fake_lane", {"adapter": "fake_lane"},
                     kind=first.kind, attempt=first.attempt, seq=first.seq, cause=first.cause)
    # the record is lost (a torn write, an antivirus lock, a manual cleanup, R-03) between two allocations
    jobs_mod.path(layout, first.id).unlink()

    second = tasks_mod.allocate_job(layout, "T-2", "delivery", attempt=1, cause="continuation")
    assert second.id != first.id
    assert second.id == "T-2.a1.s2"


def test_old_shaped_task_document_still_loads():
    doc = {"id": "T-3", "phase": 0, "title": "t", "objective": "o", "owner_role": "engineer",
           "actions": ["a"], "artifacts": ["x"], "tests": ["t"], "acceptance": "ok",
           "escalation": "none", "cost_category": "deterministic", "status": "COMPLETE",
           "attempts": [{"id": "T-3.a1", "resource": "fake_lane", "at": "2026-01-01T00:00:00+00:00",
                         "outcome": "COMPLETE", "note": "dispatch T-3.a1"}],
           "job_seq": 1}
    t = Task.model_validate(doc)
    assert t.attempts[0].id == "T-3.a1"
    assert t.counters == {}


def test_old_shaped_job_record_still_loads(layout):
    old = {"attempt_id": "T-4.a1", "task_id": "T-4", "adapter": "fake_lane", "phase": "DONE",
          "spec": {"adapter": "fake_lane"}, "handle": None, "created": "2026-01-01T00:00:00+00:00",
          "result": "COMPLETE"}
    jobs_mod.write(layout, dict(old))

    rec = jobs_mod.resolve(layout, "T-4.a1")
    assert rec is not None
    assert rec["task_id"] == "T-4"
    assert rec.get("attempt") is None and rec.get("kind") is None and rec.get("seq") is None

    assert jobs_mod.resolve(layout, "T-4.no-such-job") is None
