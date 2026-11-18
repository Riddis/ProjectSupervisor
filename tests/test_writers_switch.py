"""ATTEMPT_MODEL.md section 8.6 and step 9 of "Order of work": the writers switch. `loop.dispatch` allocates
every delivery/continuation/reroute/revision/verify/review/diagnosis job's identity through `tasks.allocate_job`
instead of counting files; a conversation job is the one exception (§2.2: it backs no Task document for the
allocator to lock, so `dispatch` still counts `jobs.for_task`'s rows for it, by design, not by oversight). The
start-up guard refuses to dispatch on a store holding a record that is neither migration-stamped nor
allocator-written, and every suffix reader `jobs.attempt_no` fed (deleted in this same release) answers from
fields instead.

Each test below is read against a neutral, invented store — no project name, task id or path from any real
project (D-005)."""
from __future__ import annotations

import time
from pathlib import Path

from supervisor import diagnosis as diagnosis_mod
from supervisor import escalation as escalation_mod
from supervisor import jobs as jobs_mod
from supervisor import review as review_mod
from supervisor import tasks as tasks_mod
from supervisor import testing
from supervisor.adapters.base import Adapter, Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

DIGEST = "d" * 64


class ScriptedLane(Adapter):
    """A `claude_cli`-shaped fake that answers by the job's own durable `kind` (already written by `jobs.prepare`
    before `submit()`/`collect()` ever run), not by guessing from the spec — so one registered lane can drive a
    delivery, a verification-free review and a diagnosis alike through one real `Supervisor`."""
    name = "claude_cli"

    def __init__(self, layout, artifacts=()):
        self.layout = layout
        self.artifacts = list(artifacts)

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
               "root": str(layout.root)}

    def submit(self, spec):
        return {"kind": "fake", "attempt_id": spec["attempt_id"]}

    def alive(self, handle):
        return False

    def collect(self, spec, handle):
        rec = jobs_mod.resolve(self.layout, spec["attempt_id"]) or {}
        kind = jobs_mod.kind_of(rec)
        if kind == "review":
            return Result(status="COMPLETE", raw={"verdict": "ACCEPT", "summary": "ok"})
        if kind == "diagnosis":
            return Result(status="COMPLETE", raw={"action": "owner", "summary": "needs an owner decision"})
        for rel in self.artifacts:
            p = Path(spec["root"]) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{spec['attempt_id']}\n", encoding="utf-8")
        return Result(status="COMPLETE", artifacts=self.artifacts)


def _dispatched_delivery(layout, tid):
    """A real attempt-1 delivery session, dispatched through the actual allocator (`Supervisor.dispatch`), the
    way every other test below needs one to build on. Registers no lane of its own — call inside a
    `testing.lane(ScriptedLane(layout))` block."""
    make_task(layout, tid, status="NOT_STARTED")
    sup = Supervisor(layout, sleep_s=0)
    task = tasks_mod.load(layout, tid)
    rec = sup.dispatch(task, "claude_cli", cause="scheduled")
    return sup, rec


# ---------------------------------------------------------------- 1. a fresh task that passes first time


def test_fresh_task_first_pass_one_attempt_three_new_shape_ids_and_flat_run_dirs(layout):
    """`tests=[]` (no declared commands): verified inline the tick the worker claims COMPLETE (§4.1), so this
    task's own evidence is a delivery and a review, both new-shape ids, both flat run directories — the review
    still correctly attaches to attempt 1 with no tracked verify job to join it through."""
    tid = "T-ONE"
    make_task(layout, tid, reviewer=["code_review"], artifacts=["a.txt"], owned_paths=["a.txt"], tests=[])
    with testing.lane(ScriptedLane(layout, artifacts=["a.txt"])):
        testing.drive(layout, iterations=40, until=lambda: tasks_mod.load(layout, tid).status == TaskStatus.COMPLETE)
    t = tasks_mod.load(layout, tid)
    assert t.status == TaskStatus.COMPLETE
    assert len(t.attempts) == 1, [a.model_dump() for a in t.attempts]
    attempt = t.attempts[0]
    assert attempt.n == 1
    ids = sorted(j["id"] for j in attempt.jobs)
    assert ids == [f"{tid}.a1.r1", f"{tid}.a1.s1"]
    kinds = {j["id"]: j["kind"] for j in attempt.jobs}
    assert kinds == {f"{tid}.a1.s1": "delivery", f"{tid}.a1.r1": "review"}
    for job_id in ids:
        run_dir = layout.runs / tid / job_id
        assert run_dir.is_dir(), job_id                 # one flat run directory per job (§2.2)
        rec = jobs_mod.resolve(layout, job_id)
        assert rec["attempt"] == 1 and isinstance(rec["seq"], int)


# ---------------------------------------------------------------- 2/3. continuation and reroute: one session each


def test_capacity_continuation_is_a_session_of_one_attempt(layout):
    with testing.lane(ScriptedLane(layout)):
        sup, rec = _dispatched_delivery(layout, "T-CONT")
        sup.requeue(rec, Result(status="DEFERRED", error="capacity", reset_epoch=time.time() + 60,
                                raw={"measured_window": True}), "capacity limit")
    t = tasks_mod.load(layout, "T-CONT")
    assert len(t.attempts) == 1 and t.attempts[0].n == 1
    ids = [j["id"] for j in t.attempts[0].jobs]
    assert ids == ["T-CONT.a1.s1", "T-CONT.a1.s2"]
    second = jobs_mod.resolve(layout, "T-CONT.a1.s2")
    assert second["attempt"] == 1 and second["cause"] == "continuation" and second["parent"] == rec["attempt_id"]


def test_lane_reroute_is_a_session_of_one_attempt(layout):
    # two distinctly named fakes: the refused lane and the one it reroutes to
    fake_a, fake_b = ScriptedLane(layout), ScriptedLane(layout)
    fake_b.name = "claude_cli_b"
    with testing.lane(fake_a), testing.lane(fake_b):
        sup, rec = _dispatched_delivery(layout, "T-REROUTE")
        sup.reroute(rec, "claude_cli_b", "lane unavailable")
    t = tasks_mod.load(layout, "T-REROUTE")
    assert len(t.attempts) == 1 and t.attempts[0].n == 1
    ids = [j["id"] for j in t.attempts[0].jobs]
    assert ids == ["T-REROUTE.a1.s1", "T-REROUTE.a1.s2"]
    second = jobs_mod.resolve(layout, "T-REROUTE.a1.s2")
    assert second["attempt"] == 1 and second["cause"] == "reroute" and second["parent"] == rec["attempt_id"]
    assert second["adapter"] == "claude_cli_b"


# ---------------------------------------------------------------- 4. a revision opens an attempt


def test_revision_opens_a_new_attempt(layout):
    with testing.lane(ScriptedLane(layout)):
        sup, rec = _dispatched_delivery(layout, "T-REV")
        jobs_mod.set_phase(layout, rec["attempt_id"], "DONE", result="FAILED", finished="now", error="bad")
        task = tasks_mod.load(layout, "T-REV")
        escalation_mod.dispatch_revision(sup, task, "verification", "bad output", False)
    t = tasks_mod.load(layout, "T-REV")
    assert [a.n for a in t.attempts] == [1, 2]
    second_attempt = next(a for a in t.attempts if a.n == 2)
    assert second_attempt.cause == "revision"
    job = jobs_mod.resolve(layout, second_attempt.jobs[0]["id"])
    assert job["attempt"] == 2 and job["cause"] == "revision" and job["seq"] == 1


# ---------------------------------------------------------------- 5/6. re-verification and a second review round


def test_reverification_of_unchanged_bytes_opens_no_new_attempt(layout):
    with testing.lane(ScriptedLane(layout)):
        sup, rec = _dispatched_delivery(layout, "T-REVERIFY")
        task = tasks_mod.load(layout, "T-REVERIFY")
        v1 = sup.dispatch_verify(task, rec, DIGEST, [], [])
        v2 = sup.dispatch_verify(task, rec, DIGEST, [], [])
    t = tasks_mod.load(layout, "T-REVERIFY")
    assert len(t.attempts) == 1 and t.attempts[0].n == 1
    assert v1["attempt"] == 1 and v1["seq"] == 1 and v1.get("cause") is None
    assert v2["attempt"] == 1 and v2["seq"] == 2 and v2["cause"] == "reverify"
    assert v2["parent"] == rec["attempt_id"]


def test_second_review_round_on_same_digest_opens_no_new_attempt(layout):
    tid = "T-REREVIEW"
    make_task(layout, tid, reviewer=["code_review"])
    tasks_mod.allocate_job(layout, tid, "delivery", attempt=1, cause="scheduled")   # seeds counters.attempt = 1
    tasks_mod.record_delivery(layout, tid, DIGEST, actor="test", paths=[])
    task = tasks_mod.load(layout, tid)
    sup = Supervisor(layout, sleep_s=0)
    with testing.lane(ScriptedLane(layout)):
        r1 = review_mod.dispatch(sup, task, "code_review")
        r2 = review_mod.dispatch(sup, task, "code_review")
    assert r1["attempt"] == 1 and r1.get("cause") is None
    assert r2["attempt"] == 1 and r2["cause"] == "rereview"
    t = tasks_mod.load(layout, tid)
    assert len(t.attempts) == 1 and t.attempts[0].n == 1


# ---------------------------------------------------------------- 7. a diagnosis before any delivery is task-scoped


def test_diagnosis_before_any_delivery_is_task_scoped(layout):
    tid = "T-DIAG"
    make_task(layout, tid, status="BLOCKED")
    task = tasks_mod.load(layout, tid)
    sup = Supervisor(layout, sleep_s=0)
    with testing.lane(ScriptedLane(layout)):
        rec = diagnosis_mod.dispatch(sup, task, "worker_blocked", "detail", "sig-1", level="manager")
    assert rec is not None
    assert rec["attempt_id"] == f"{tid}.d1"
    assert rec.get("attempt") is None and rec.get("scope") == "task"
    assert tasks_mod.load(layout, tid).attempts == []       # never folded into attempts[] (§2.5)


# ---------------------------------------------------------------- 8. an old id and an old URL still resolve


def test_old_id_and_old_url_still_resolve(layout):
    tid = "T-OLD"
    make_task(layout, tid, status="IN_PROGRESS")
    stored, old_id = tasks_mod.add_attempt(layout, tid, "claude_cli", note="dispatch")
    run_dir = layout.runs / tid / old_id
    run_dir.mkdir(parents=True)
    jobs_mod.prepare(layout, tid, old_id, "claude_cli", {"kind": "delivery", "run_dir": str(run_dir)})
    jobs_mod.set_phase(layout, old_id, "DONE", result="COMPLETE", finished="now",
                       migrated={"attempts": {"at": "2026-01-01T00:00:00+00:00"}})

    rec = jobs_mod.resolve(layout, old_id)
    assert rec is not None and rec["phase"] == "DONE"

    from supervisor.ui.routes.tasks import attempt_groups
    t = tasks_mod.load(layout, tid)
    groups = attempt_groups(layout, t)
    assert any(r["job_id"] == old_id for g in groups for r in g["rows"])

    from supervisor.adapters.claude_cli import resolve_handoff
    assert resolve_handoff(layout, tid, old_id, "delivery") is None     # attempt 1, session 1: nothing to read


# ---------------------------------------------------------------- 9. a replayed dispatch creates no duplicate job


def test_replayed_dispatch_after_a_crash_creates_no_duplicate_child_job(layout):
    with testing.lane(ScriptedLane(layout)):
        sup, rec = _dispatched_delivery(layout, "T-REPLAY")
        result = Result(status="DEFERRED", error="capacity", reset_epoch=time.time() + 60,
                        raw={"measured_window": True})
        sup.requeue(rec, result, "capacity limit")
        before = sorted(r["attempt_id"] for r in jobs_mod.for_task(layout, "T-REPLAY"))
        sup.requeue(rec, result, "capacity limit")             # the replay: same rec, same result, same reason
        after = sorted(r["attempt_id"] for r in jobs_mod.for_task(layout, "T-REPLAY"))
    assert after == before == ["T-REPLAY.a1.s1", "T-REPLAY.a1.s2"]


# ---------------------------------------------------------------- 10. the start-up guard
#
# "Start-up" names the supervisor *process* (`supervisor run`), not every `Supervisor()` object a test or a
# library caller builds directly — the suite's own fixtures plant a bare, un-migrated job record for a dozen
# unrelated scenarios having nothing to do with migration readiness, and none of those must start refusing to
# dispatch because of it. The guard itself is `cli.run()`'s own first precondition, checked before it ever
# constructs a `Supervisor` or enters the loop, exercised here through the real CLI command (`CliRunner`) so the
# test is of the actual entry point, not a copy of its call.


def test_an_unmigrated_job_record_refuses_to_dispatch_and_names_the_fix(layout):
    from typer.testing import CliRunner

    from supervisor.cli import app
    bad_path = layout.jobs / "T-BAD.a1.json"
    jobs_mod.write(layout, {"attempt_id": "T-BAD.a1", "task_id": "T-BAD", "adapter": "claude_cli", "phase": "DONE",
                            "spec": {"kind": "delivery"}, "handle": None, "result": "COMPLETE"})
    assert bad_path.exists()
    make_task(layout, "T-TRIGGER")
    result = CliRunner(env={"SUPERVISOR_ROOT": str(layout.root)}).invoke(app, ["run", "--once"])
    assert result.exit_code == 1
    assert bad_path.name in result.output
    assert "supervisor migrate attempts" in result.output
    assert jobs_mod.for_task(layout, "T-TRIGGER") == []     # the refused run dispatched nothing


def test_a_migrated_or_allocator_written_store_passes_the_guard(layout):
    """The guard's other half, stated directly against the function `cli.run()` calls: a store where every
    record is migration-stamped or allocator-written never raises."""
    from supervisor.loop import _check_writer_guard
    jobs_mod.write(layout, {"attempt_id": "T-OK.a1", "task_id": "T-OK", "adapter": "claude_cli", "phase": "DONE",
                            "spec": {"kind": "delivery"}, "handle": None, "result": "COMPLETE",
                            "migrated": {"attempts": {"at": "2026-01-01T00:00:00+00:00"}}})
    jobs_mod.prepare(layout, "T-OK", "T-OK.a2.s1", "claude_cli", {"kind": "delivery"},
                     kind="delivery", attempt=2, seq=1)
    _check_writer_guard(layout)             # must not raise


def test_a_conversation_job_record_is_exempt_from_the_guard(layout):
    """A conversation job backs no Task document and stays outside the attempt model entirely (§2.2): it never
    gains `kind`/`seq` from the allocator and is never migration-stamped, and that is not a defect the guard
    should ever report."""
    from supervisor.loop import _check_writer_guard
    jobs_mod.write(layout, {"attempt_id": "chat.a1", "task_id": "chat", "adapter": "claude_cli", "phase": "DONE",
                            "spec": {"kind": "conversation"}, "handle": None, "result": "COMPLETE"})
    _check_writer_guard(layout)             # must not raise
