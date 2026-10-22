"""Execution safeguards: mandatory ownership (exclusive by default), the task contract as the floor, one launch
gate for every path, DRAIN + shutdown, and the bounded review-failure branch."""
import json
import sys
import time

import pytest
import yaml

from supervisor import commands, gate, jobs, runtime, scheduler, shutdown as shutdown_mod, tasks, verify
from supervisor.adapters import REGISTRY
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_loop import proc_task, ptask


@pytest.fixture
def no_capacity_gate(monkeypatch):
    monkeypatch.setattr("supervisor.capacity.require", lambda layout_, engine: None)


def claude_task(layout, tid, owned=None, deps=()):
    make_task(layout, tid, deps=deps)
    if owned is not None:
        tasks._update(layout, tid, lambda doc: doc.__setitem__("owned_paths", list(owned)))
    return tasks.load(layout, tid)


def running_record(layout, task_id, adapter="claude_cli", kind="delivery", attempt="a1"):
    return jobs.write(layout, {"attempt_id": f"{task_id}.{attempt}", "task_id": task_id, "adapter": adapter, "phase": "SUBMITTED",
                               "spec": {"kind": kind}, "handle": {}, "gpu": False, "created": "2026-01-01T00:00:00+00:00"})


# ------------------------------------------------------------------ §1 ownership

def test_a_model_lane_delivery_without_owned_paths_is_exclusive_by_default(layout):
    t = claude_task(layout, "T")
    assert t.owned_paths == []
    assert gate.owned_for(t, "claude_cli", "delivery") == ["."]
    assert gate.owned_for(t, "codex_cli", "delivery") == ["."]
    assert gate.owned_for(t, "python", "delivery") == []          # deterministic jobs own only what they declare
    assert gate.owned_for(t, "claude_cli", "review") == []        # a reviewer writes nothing
    assert gate.owned_for(None, "claude_cli", "conversation") == []


def test_exclusive_mode_subsumes_narrower_roots_and_scoped_paths_are_empty(layout):
    t = claude_task(layout, "T", owned=["docs/x", "."])
    assert t.owned_paths == ["."] and t.exclusive() and t.scoped_paths() == []
    s = claude_task(layout, "S", owned=["docs/x"])
    assert not s.exclusive() and s.scoped_paths() == ["docs/x"]


def test_gate_never_runs_two_writers_whose_ownership_overlaps(layout, no_capacity_gate):
    a = claude_task(layout, "A")                      # exclusive by default
    b = claude_task(layout, "B", owned=["docs/b"])    # scoped
    running = [running_record(layout, "A")]
    assert "overlap" in gate.check(layout, b, "claude_cli", "delivery", running)
    running = [running_record(layout, "B")]
    assert "overlap" in gate.check(layout, a, "claude_cli", "delivery", running)
    c = claude_task(layout, "C", owned=["docs/c"])
    assert gate.check(layout, c, "claude_cli", "delivery", running) is None      # disjoint scoped roots may share
    assert gate.check(layout, a, "claude_cli", "review", running) is None        # a review of anything is fine
    assert gate.check(layout, None, "claude_cli", "conversation", running) is None


def test_gate_refuses_every_launch_unless_the_decision_is_run(layout, no_capacity_gate):
    t = claude_task(layout, "T")
    for decision in ("PAUSE", "DRAIN", "STOP"):
        runtime.set_decision(layout, decision)
        assert gate.check(layout, t, "claude_cli", "delivery", []) == f"decision is {decision}"
        assert gate.check(layout, None, "claude_cli", "conversation", []) == f"decision is {decision}"
    runtime.set_decision(layout, "RUN")
    assert gate.check(layout, t, "claude_cli", "delivery", []) is None


def test_scheduler_plans_one_exclusive_writer_at_a_time(layout, no_capacity_gate):
    claude_task(layout, "A"); claude_task(layout, "B"); claude_task(layout, "C", owned=["docs/c"])
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"limits": {"max_agent_jobs": 3}}))
    picked = [t.id for t, _ in scheduler.plan(layout)]
    assert picked == ["A"]                                     # B and C overlap A's exclusive ownership
    running_record(layout, "C")                                # a scoped writer running
    picked = [t.id for t, _ in scheduler.plan(layout)]
    assert picked == []                                        # exclusive A and B overlap C


def test_reviews_are_not_blocked_by_an_exclusive_writer(layout, no_capacity_gate):
    claude_task(layout, "A")
    running = [running_record(layout, "A")]
    r = claude_task(layout, "R")
    assert gate.check(layout, r, "claude_cli", "review", running) is None


def test_creating_a_writing_task_without_ownership_is_refused(layout):
    doc = {"id": "N1", "phase": 0, "title": "n", "objective": "o", "owner_role": "engineer", "artifacts": ["x"],
           "acceptance": "ok", "cost_category": "claude"}
    with pytest.raises(tasks.TaskError, match="owned_paths"):
        tasks.create(layout, doc)
    assert tasks.create(layout, {**doc, "owned_paths": ["."]})["id"] == "N1"
    det = {**doc, "id": "N2", "cost_category": "deterministic"}
    assert tasks.create(layout, det)["id"] == "N2"            # deterministic work owns only what it declares


def test_import_refuses_new_writing_tasks_without_ownership_but_keeps_existing_ones(layout):
    make_task(layout, "OLD")                                   # legacy record, no ownership
    seed = {"tasks": [
        {"id": "OLD", "phase": 0, "title": "old", "objective": "o", "owner_role": "engineer", "acceptance": "ok", "cost_category": "claude"},
        {"id": "NEW", "phase": 0, "title": "new", "objective": "o", "owner_role": "engineer", "acceptance": "ok", "cost_category": "claude"}]}
    (layout.root / "seed.yaml").write_text(yaml.safe_dump(seed))
    with pytest.raises(tasks.TaskError, match="NEW"):
        tasks.import_seed(layout, layout.root / "seed.yaml")
    seed["tasks"][1]["owned_paths"] = ["src/new"]
    (layout.root / "seed.yaml").write_text(yaml.safe_dump(seed))
    assert tasks.import_seed(layout, layout.root / "seed.yaml")["created"] == ["NEW"]


def test_migrate_ownership_makes_live_writing_tasks_explicitly_exclusive(layout):
    claude_task(layout, "A"); claude_task(layout, "B", owned=["docs/b"]); ptask(layout, "P")
    make_task(layout, "DONE", status="COMPLETE")
    out = tasks.migrate_ownership(layout)
    assert out["migrated"] == ["A"]
    assert tasks.load(layout, "A").owned_paths == ["."]
    assert tasks.load(layout, "B").owned_paths == ["docs/b"]
    assert tasks.load(layout, "P").owned_paths == [] and tasks.load(layout, "DONE").owned_paths == []
    assert tasks.migrate_ownership(layout)["migrated"] == []        # idempotent


def test_repo_scan_runs_for_an_exclusive_task(layout):
    """An undeclared write anywhere fails verification with no scoped roots."""
    t = claude_task(layout, "T", owned=["."])
    (layout.root / "T.txt").write_text("deliverable")
    before = verify.snapshot(layout, t)
    assert before["owned"] == {} and "T.txt" in before["repo"]
    (layout.root / "sneaky.py").write_text("x")
    outcome = verify.verify(layout, t, ["T.txt"], before=before)
    assert outcome.ok is False and "undeclared writes: sneaky.py" in outcome.detail
    (layout.root / "sneaky.py").unlink()
    assert verify.verify(layout, t, ["T.txt"], before=before).ok is True


def test_dispatch_takes_a_snapshot_for_every_writing_job(layout, monkeypatch):
    from supervisor.adapters import process as procad
    t = claude_task(layout, "T")                               # no owned_paths → exclusive → snapshot
    sup = Supervisor(layout, sleep_s=0)
    monkeypatch.setitem(REGISTRY, "claude_cli", REGISTRY["process"])
    tasks._update(layout, "T", lambda d: d.__setitem__("job", {"adapter": "claude_cli", "argv": [sys.executable, "-c", "pass"]}))
    runtime.set_decision(layout, "PAUSE")                      # held: we only care that prepare() took the snapshot
    rec = sup.dispatch(tasks.load(layout, "T"), "claude_cli")
    from pathlib import Path
    assert (Path(rec["spec"]["run_dir"]) / "before_manifest.json").exists()


# ------------------------------------------------------------------ §2 the contract is the floor

def test_a_worker_cannot_narrow_the_declared_artifacts(layout):
    ptask(layout, "T", name="only.txt")                        # the job writes only.txt and reports it
    tasks._update(layout, "T", lambda d: d.__setitem__("artifacts", ["only.txt", "also-required.txt"]))
    Supervisor(layout, sleep_s=0).once()
    t = tasks.load(layout, "T")
    assert t.status != TaskStatus.COMPLETE                      # (a revision may already be queued by escalation)
    assert t.delivery.verification.ok is False and "also-required.txt" in t.delivery.verification.detail


def test_a_worker_may_add_artifacts_beyond_the_contract(layout):
    ptask(layout, "T", name="extra.txt")
    tasks._update(layout, "T", lambda d: d.__setitem__("artifacts", []))     # contract names nothing; worker reports extra.txt
    Supervisor(layout, sleep_s=0).once()
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.COMPLETE and t.paths == ["extra.txt"]


def _pytest_file(layout, name, body):
    (layout.root / name).write_text(body, encoding="utf-8")
    return f"{sys.executable} -m pytest -p no:cacheprovider {name}"


def test_required_checks_run_even_when_the_worker_reports_only_passing_tests(layout):
    make_task(layout, "T")
    failing = _pytest_file(layout, "t_required.py", "def test_no():\n    assert False\n")
    tasks._update(layout, "T", lambda d: d.__setitem__("checks", [{"command": failing, "timeout_s": 120}]))
    task = tasks.load(layout, "T")
    assert task.checks[0].timeout_s == 120
    passing = [{"command": _pytest_file(layout, "t_pass.py", "def test_ok():\n    assert True\n"), "exit_code": 0}]
    outcome = verify.verify(layout, task, [], declared_tests=passing, checks=[c.model_dump() for c in task.checks])
    assert outcome.ok is False and "required check failed" in outcome.detail
    cmds = [r["command"] for r in outcome.report["tests"]]
    assert cmds[0] == failing and len(cmds) == 2


def test_checks_accept_plain_strings_and_appear_in_the_brief(layout):
    from supervisor import packets
    make_task(layout, "T")
    tasks._update(layout, "T", lambda d: d.update(checks=["python -m pytest tests/test_x.py"], owned_paths=["src/x"]))
    brief = packets.delivery_brief(layout, tasks.load(layout, "T"))
    assert "Required checks" in brief and "python -m pytest tests/test_x.py" in brief
    assert "Write scope" in brief and "src/x" in brief
    tasks._update(layout, "T", lambda d: d.update(owned_paths=["."]))
    assert "Exclusive" in packets.delivery_brief(layout, tasks.load(layout, "T"))


# ------------------------------------------------------------------ §3 one launch gate

def test_pause_holds_prepared_records_and_a_resume_launches_them(layout):
    """A PREPARED record found by reconcile() — a crashed dispatch or a revision queued mid-pause — must not launch
    under PAUSE; it waits with a recorded reason and goes on resume."""
    ptask(layout, "A")
    task = tasks.load(layout, "A")
    spec = REGISTRY["python"].prepare(layout, task, "A.a1")
    jobs.prepare(layout, "A", "A.a1", "python", spec)
    runtime.set_decision(layout, "PAUSE")
    sup = Supervisor(layout, sleep_s=0)
    sup.once(); sup.once()
    rec = jobs.read(layout, "A.a1")
    assert rec["phase"] == "PREPARED" and rec["held"] == "decision is PAUSE"
    held = [json.loads(e) for e in layout.log.read_text().splitlines() if '"launch_held"' in e]
    assert len(held) == 1                                      # logged once per reason, not per iteration
    commands.submit(layout, "loop.resume", {}, actor="owner")
    sup.once()
    assert jobs.read(layout, "A.a1")["phase"] == "DONE" and jobs.read(layout, "A.a1").get("held") is None
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE


def test_a_revision_dispatched_during_pause_waits_for_resume(layout, monkeypatch):
    from supervisor import escalation
    ptask(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    runtime.set_decision(layout, "PAUSE")
    sup = Supervisor(layout, sleep_s=0)
    escalation.record_failure(sup, "T", "verification", "missing artifacts: T.txt")
    recs = jobs.open_records(layout)
    assert len(recs) == 1 and recs[0]["phase"] == "PREPARED" and recs[0]["held"] == "decision is PAUSE"
    runtime.set_decision(layout, "RUN")
    sup.once()
    assert tasks.load(layout, "T").status == TaskStatus.COMPLETE


def test_finished_work_is_still_collected_under_pause(layout):
    proc_task(layout, "P", seconds=0.1)
    sup = Supervisor(layout, sleep_s=0)
    sup.dispatch(tasks.load(layout, "P"), "process")
    time.sleep(1.5)
    runtime.set_decision(layout, "PAUSE")
    sup.once()
    assert jobs.read(layout, "P.a1")["phase"] == "DONE" and tasks.load(layout, "P").status == TaskStatus.COMPLETE


# ------------------------------------------------------------------ §4 drain and shutdown

@pytest.fixture
def fake_os(monkeypatch):
    calls = []
    monkeypatch.setattr(shutdown_mod, "run_command", lambda argv: (calls.append(argv), (0, "ok"))[1])
    return calls


def test_drain_lets_running_work_finish_then_stops_and_powers_off(layout, fake_os):
    proc_task(layout, "P", seconds=1.0)
    ptask(layout, "Q")                                         # eligible, must NOT start during the drain
    sup = Supervisor(layout, sleep_s=0)
    sup.dispatch(tasks.load(layout, "P"), "process")
    r = commands.submit_or_apply(layout, "loop.drain", {"shutdown": True}, actor="owner-ui")
    assert r["ok"] and runtime.read(layout)["decision"] == "DRAIN" and runtime.read(layout)["shutdown_on_drain"] is True
    assert sup.once() == "DRAIN"
    assert runtime.read(layout)["status"] == "DRAINING" and runtime.is_live(layout)
    assert tasks.load(layout, "Q").status == TaskStatus.NOT_STARTED
    deadline = time.time() + 10
    decision = "DRAIN"
    while time.time() < deadline and decision == "DRAIN":
        time.sleep(0.2)
        decision = sup.once()
    assert decision == "STOP"
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE           # finished, ingested and verified first
    assert tasks.load(layout, "Q").status == TaskStatus.NOT_STARTED
    assert fake_os and fake_os[0][0] == "shutdown" and fake_os[0] == shutdown_mod.shutdown_argv()
    rt = runtime.read(layout)
    assert rt["decision"] == "STOP" and rt["shutdown_issued"] is True and rt["status"] == "STOPPED"
    assert shutdown_mod.pending(layout) is not None
    events = [json.loads(e)["event"] for e in layout.log.read_text().splitlines()]
    assert "drain_complete" in events and "system_shutdown" in events


def test_drain_without_shutdown_only_stops(layout, fake_os):
    ptask(layout, "A")
    runtime.set_decision(layout, "DRAIN")
    assert Supervisor(layout, sleep_s=0).once() == "STOP"
    assert fake_os == [] and runtime.read(layout).get("shutdown_issued") is None
    assert tasks.load(layout, "A").status == TaskStatus.NOT_STARTED


def test_resume_cancels_a_drain_and_an_os_shutdown_counting_down(layout, fake_os):
    runtime.set_decision(layout, "DRAIN", shutdown_on_drain=True)
    shutdown_mod.schedule(layout)
    assert runtime.read(layout)["shutdown_issued"] is True
    r = commands.submit_or_apply(layout, "loop.resume", {}, actor="owner-ui")
    assert r["ok"] and r["result"]["shutdown"]["cancelled"] is True
    rt = runtime.read(layout)
    assert rt["decision"] == "RUN" and rt["shutdown_issued"] is False and rt["shutdown_on_drain"] is False
    assert fake_os[-1] == shutdown_mod.cancel_argv()
    assert shutdown_mod.pending(layout) is None


def test_pause_or_stop_disarms_a_pending_shutdown_on_drain(layout):
    runtime.set_decision(layout, "DRAIN", shutdown_on_drain=True)
    runtime.set_decision(layout, "PAUSE")
    assert runtime.read(layout)["shutdown_on_drain"] is False


def test_system_shutdown_refuses_under_a_live_supervisor(layout, fake_os):
    runtime.heartbeat(layout, "RUNNING")
    r = commands.submit_or_apply(layout, "system.shutdown", {}, actor="owner-ui")
    assert r["ok"] is False and "drain" in r["error"] and fake_os == []
    runtime.heartbeat(layout, "STOPPED")
    r = commands.submit_or_apply(layout, "system.shutdown", {}, actor="owner-ui")
    assert r["ok"] is True and fake_os[0] == shutdown_mod.shutdown_argv()


def test_a_failed_shutdown_command_is_recorded_not_hidden(layout, monkeypatch):
    monkeypatch.setattr(shutdown_mod, "run_command", lambda argv: (1, "access denied"))
    out = shutdown_mod.schedule(layout)
    assert out["ok"] is False and runtime.read(layout)["shutdown_issued"] is False
    assert "access denied" in runtime.read(layout)["shutdown_error"]


# ------------------------------------------------------------------ §5 bounded review failures

def test_a_review_that_keeps_failing_blocks_the_task_instead_of_looping(layout):
    from supervisor import review
    make_task(layout, "T", reviewer=["code_review"])
    tasks.set_status(layout, "T", "IN_PROGRESS")
    digest = "d" * 64
    tasks.record_delivery(layout, "T", digest, actor="test", paths=["T.txt"])
    tasks.record_verification(layout, "T", digest, True, detail="ok")
    tasks.advance(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    for n in (1, 2):
        rec = jobs.write(layout, {"attempt_id": f"T.r{n}", "task_id": "T", "adapter": "codex_cli", "phase": "RUNNING",
                                  "spec": {"kind": "review"}, "handle": {}, "review_role": "code_review", "review_digest": digest})
        review.ingest(sup, rec, Result(status="FAILED", error="reviewer crashed"))
        jobs.set_phase(layout, f"T.r{n}", "DONE")
        assert jobs.read(layout, f"T.r{n}")["failed_review"] is True
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.BLOCKED and "failed 2x" in t.history[-1]["note"]
    assert review.pending(layout) == []                        # BLOCKED: nothing re-dispatched
    issues = list(layout.issues.glob("I-*.yaml"))
    assert len(issues) == 1 and "review keeps failing" in issues[0].read_text()
    # the first failure alone does not block
    make_task(layout, "U", reviewer=["code_review"])
    tasks.set_status(layout, "U", "IN_PROGRESS")
    tasks.record_delivery(layout, "U", digest, actor="test", paths=["U.txt"])
    tasks.record_verification(layout, "U", digest, True, detail="ok")
    tasks.advance(layout, "U")
    rec = jobs.write(layout, {"attempt_id": "U.r1", "task_id": "U", "adapter": "codex_cli", "phase": "RUNNING",
                              "spec": {"kind": "review"}, "handle": {}, "review_role": "code_review", "review_digest": digest})
    review.ingest(sup, rec, Result(status="COMPLETE", raw={}))  # no verdict at all
    assert tasks.load(layout, "U").status == TaskStatus.WAITING_FOR_REVIEW


def test_a_human_adapter_record_waiting_for_the_owner_does_not_hold_the_drain(layout, fake_os, monkeypatch):
    make_task(layout, "H")
    monkeypatch.setattr(REGISTRY["human"], "collect", lambda spec, handle: None)     # the owner has not answered
    jobs.write(layout, {"attempt_id": "H.a1", "task_id": "H", "adapter": "human", "phase": "SUBMITTED",
                        "spec": {"kind": "delivery"}, "handle": {}, "gpu": False, "created": "2026-01-01T00:00:00+00:00"})
    runtime.set_decision(layout, "DRAIN", shutdown_on_drain=True)
    assert Supervisor(layout, sleep_s=0).once() == "STOP"
    assert fake_os and fake_os[0] == shutdown_mod.shutdown_argv()
