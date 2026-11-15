"""R-09: one idle loop turn used to parse the whole task store nine times and scan the job directory thirteen
times (plus 352 more per-task globs inside repair_continuations) — 44.8s measured on a store of 365 tasks and
1,380 jobs, against a 5s sleep. The fixture below (about 350 tasks with realistic histories, about 1,400
finished job records) is built by the test itself; it carries no project data.

This file holds the regression — the budget any future change to the turn must stay inside. It is extended by a
second commit with the tests that prove the fix: the C/Python loader (tests/test_store_loader.py), a write made
early in a turn being visible to a later step of the same turn, and the heartbeat's turn_s field."""
from __future__ import annotations

import pathlib
import time

import pytest

from supervisor import jobs as jobs_mod, store
from supervisor.loop import Supervisor
from tests.conftest import make_task

N_TASKS = 350
JOBS_PER_TASK = 4

# Generous: the unfixed turn measured 44.8s on the review's own machine (365 tasks, 1,380 jobs); this fixture is
# slightly smaller and the bound is wide enough to never be flaky on slower hardware while still catching a
# turn that is quadratic (or worse) in store size.
TURN_BUDGET_S = 20.0
MAX_TASK_STORE_LOADS = 1
MAX_JOB_DIR_SCANS = 3


def _build_scale_fixture(layout) -> None:
    for i in range(N_TASKS):
        tid = f"S-{i:04d}"
        history = [{"at": f"2026-{(j % 11) + 1:02d}-01T00:00:0{j}+00:00", "by": "loop",
                    "from": "NOT_STARTED" if j == 0 else "IN_PROGRESS", "to": "IN_PROGRESS" if j == 0 else "IN_PROGRESS",
                    "note": f"turn {j} of a realistic history entry, padded with detail text " + ("x" * 80)}
                   for j in range(6)]
        attempts = [{"id": f"{tid}.a{n}", "resource": "fake_lane", "at": "2026-01-01T00:00:00+00:00",
                     "outcome": "DONE", "note": f"attempt {n} " + ("y" * 40)} for n in range(1, JOBS_PER_TASK + 1)]
        doc = {"id": tid, "phase": 0, "title": f"scale task {i}", "objective": "a generated fixture task " + ("z" * 60),
               "owner_role": "engineer", "actions": ["a"], "artifacts": [f"out/{tid}.txt"], "tests": ["t"],
               "acceptance": "ok", "escalation": "none", "cost_category": "deterministic", "status": "COMPLETE",
               "history": history, "attempts": attempts, "job_seq": JOBS_PER_TASK, "result": "COMPLETE", "paths": [],
               "checkpoint": {"status": "committed", "commit": "0" * 40}}
        store.write_new(layout.tasks / f"{tid}.yaml", doc)
        for n in range(1, JOBS_PER_TASK + 1):
            attempt_id = f"{tid}.a{n}"
            jobs_mod.write(layout, {
                "attempt_id": attempt_id, "task_id": tid, "adapter": "fake_lane", "phase": "DONE",
                "spec": {"adapter": "fake_lane", "task_id": tid, "attempt_id": attempt_id, "kind": "delivery",
                         "run_dir": f"runs/{tid}/{attempt_id}"},
                "handle": None, "created": "2026-01-01T00:00:00+00:00", "finished": "2026-01-01T00:05:00+00:00",
                "result": "COMPLETE",
            })


@pytest.fixture
def scale_layout(layout):
    _build_scale_fixture(layout)
    return layout


def _glob_counts(monkeypatch, layout) -> dict:
    """Every real `Path.glob()` call against the task directory or the job directory, whatever pattern it uses —
    a once-per-task glob (the pre-fix repair_continuations) counts here exactly as a whole-directory one does,
    since both re-enumerate the same files on disk."""
    counts = {"tasks": 0, "jobs": 0}
    real_glob = pathlib.Path.glob

    def wrapped(self, pattern, *a, **kw):
        if self == layout.tasks:
            counts["tasks"] += 1
        elif self == layout.jobs:
            counts["jobs"] += 1
        return real_glob(self, pattern, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "glob", wrapped)
    return counts


def test_idle_turn_loads_the_store_once_and_scans_jobs_a_bounded_number_of_times(scale_layout, monkeypatch):
    layout = scale_layout
    sup = Supervisor(layout, sleep_s=0)
    counts = _glob_counts(monkeypatch, layout)
    start = time.perf_counter()
    sup.once()
    elapsed = time.perf_counter() - start
    print(f"R-09 scale fixture ({N_TASKS} tasks, {N_TASKS * JOBS_PER_TASK} jobs): one idle turn took "
          f"{elapsed:.3f}s; task dir scanned {counts['tasks']}x, job dir scanned {counts['jobs']}x")
    assert counts["tasks"] == MAX_TASK_STORE_LOADS, (
        f"the task store was scanned {counts['tasks']}x in one idle turn, expected {MAX_TASK_STORE_LOADS}")
    assert counts["jobs"] <= MAX_JOB_DIR_SCANS, (
        f"the job directory was scanned {counts['jobs']}x in one idle turn, expected at most {MAX_JOB_DIR_SCANS}")
    assert elapsed < TURN_BUDGET_S, f"one idle turn took {elapsed:.1f}s, expected under {TURN_BUDGET_S}s"


# ---- the fix: a write earlier in a turn is visible to a step later in the same turn, and the turn's duration
# reaches the heartbeat. These need supervisor.tasks.turn_snapshot() / supervisor.jobs.turn_snapshot(), which do
# not exist before the fix commit — the budget test above is the regression; these are the fix's own proof.


def test_a_write_made_early_in_the_turn_is_visible_to_a_later_step(layout):
    """tasks._update() and jobs.write() keep an open turn snapshot current: a step that writes a task or a job
    record is seen by every step still to come in the same turn, with no second real scan to make it so."""
    from supervisor import tasks as tasks_mod
    make_task(layout, "A", status="IN_PROGRESS")
    tasks_snapshot = tasks_mod.load_all(layout)
    jobs_snapshot = jobs_mod.open_records(layout)
    with tasks_mod.turn_snapshot(tasks_snapshot), jobs_mod.turn_snapshot(jobs_snapshot):
        tasks_mod.add_attempt(layout, "A", "fake_lane", note="dispatch")      # an early step's write
        jobs_mod.prepare(layout, "A", "A.a1", "fake_lane", {"adapter": "fake_lane"})
        later_tasks = tasks_mod.load_all(layout)              # a later step's own call — must see the write above
        later_jobs = jobs_mod.open_records(layout)
    assert [a.id for a in later_tasks["A"].attempts] == ["A.a1"]
    assert [r["attempt_id"] for r in later_jobs] == ["A.a1"]


def test_a_dispatch_is_visible_to_every_step_later_in_the_same_turn(layout, monkeypatch):
    """The scenario once() actually produces: a dispatch (Supervisor.dispatch, exactly what dispatch_delivery()
    calls) writes the attempt and the job record; dispatch_reviews(), request_approvals(), sweep_stalled() and
    the rest, called right after in the same turn, must see it — without re-scanning either directory to do so."""
    from supervisor import tasks as tasks_mod, testing
    from supervisor.loop import Supervisor
    make_task(layout, "A", status="IN_PROGRESS", job={"adapter": "fake_lane"})
    with testing.lane(testing.FakeLane("fake_lane")):
        sup = Supervisor(layout, sleep_s=0)
        tasks_snapshot = tasks_mod.load_all(layout)
        jobs_snapshot = jobs_mod.open_records(layout)
        with tasks_mod.turn_snapshot(tasks_snapshot), jobs_mod.turn_snapshot(jobs_snapshot):
            sup.dispatch(tasks_snapshot["A"], "fake_lane")
            counts = _glob_counts(monkeypatch, layout)         # only the two calls below are being measured
            later_tasks = tasks_mod.load_all(layout)
            later_jobs = jobs_mod.open_records(layout)
    assert counts == {"tasks": 0, "jobs": 0}, f"a later step re-scanned the store: {counts}"
    attempt_id = later_tasks["A"].attempts[0].id
    assert any(r["attempt_id"] == attempt_id for r in later_jobs)


def test_heartbeat_carries_the_last_turns_duration(layout):
    from supervisor import runtime
    from supervisor.loop import Supervisor
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    doc = runtime.read(layout)
    assert isinstance(doc.get("turn_s"), (int, float)), doc.get("turn_s")
    assert doc["turn_s"] >= 0
