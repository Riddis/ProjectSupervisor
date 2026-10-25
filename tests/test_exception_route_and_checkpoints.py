"""The manager → architect → owner exception route, accepted-work checkpoints, and
verification commands as a tracked job."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from supervisor import checkpoint, diagnosis, escalation, gate, inbox, jobs, runtime, store, tasks, verify
from supervisor.adapters import REGISTRY, python as pyad
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_loop import ptask

FAKE_DIAGNOSIS = str(Path(__file__).resolve().parent / "fixtures" / "fake_diagnosis.py")


class FakeDiagnosisClaude(REGISTRY["claude_cli"].__class__):
    name = "claude_cli"

    def command(self, spec):
        argv = super().command(spec)
        return [sys.executable, FAKE_DIAGNOSIS, "--fake-role", spec.get("role", "")] + argv[1:]


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setitem(REGISTRY, "claude_cli", FakeDiagnosisClaude())
    monkeypatch.setattr("supervisor.capacity.require", lambda layout_, engine: None)
    yield
    for k in [k for k in os.environ if k.startswith("FAKE_DIAGNOSIS_ACTION")]:
        os.environ.pop(k, None)


@pyad.job("write_nothing")
def _write_nothing(root, run_dir, **_):
    return {"status": "COMPLETE", "artifacts": [], "notes": "claimed done, wrote nothing"}


@pyad.job("write_artifact2")
def _write_artifact2(root, run_dir, name="out.txt", **_):
    (root / name).write_text("x")
    return {"status": "COMPLETE", "artifacts": [name]}


def failing_task(layout, tid):
    """A python job that always claims COMPLETE without producing its artifact → verification fails every time."""
    make_task(layout, tid)
    tasks._update(layout, tid, lambda d: d.update(job={"adapter": "python", "fn": "write_nothing"}, artifacts=[f"{tid}.txt"]))


def run_until(layout, predicate, timeout=25):
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.once()
        if predicate():
            return sup
        time.sleep(0.1)
    raise AssertionError("condition never became true")


def diag_records(layout, tid):
    return [r for r in jobs.for_task(layout, tid) if (r.get("spec") or {}).get("kind") == "diagnosis"]


# ------------------------------------------------------------------ §1 exception route

def test_exhausted_retries_dispatch_one_manager_diagnosis_not_an_owner_item(layout, fake_claude):
    failing_task(layout, "T")
    sup = run_until(layout, lambda: diag_records(layout, "T"))
    recs = diag_records(layout, "T")
    assert len(recs) == 1 and recs[0]["diag_level"] == "manager" and recs[0]["diag_role"] == "senior_pm"
    assert tasks.load(layout, "T").status in (TaskStatus.FAILED, TaskStatus.BLOCKED)   # stopped, not re-dispatched
    assert inbox.open_items(layout, "owner") == []                      # the owner is not bothered yet
    assert (layout.issues / "I-001.yaml").exists()
    # the diagnosis job reads only: review lane confinement, no ownership, no snapshot
    assert gate.owned_for(tasks.load(layout, "T"), "claude_cli", "diagnosis") == []
    # a replayed escalation (same signature, same level) spends no second diagnosis
    assert diagnosis.dispatch(sup, tasks.load(layout, "T"), "verification", "x", recs[0]["diag_signature"], "manager", "I-001") is None
    assert len(diag_records(layout, "T")) == 1


def test_requeue_diagnosis_puts_the_task_back_with_guidance_and_a_fresh_retry_budget(layout, fake_claude):
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "requeue"
    failing_task(layout, "T")
    run_until(layout, lambda: any(r.get("diag_action") for r in diag_records(layout, "T")))
    t = tasks.load(layout, "T")
    guidance = (t.model_extra or {}).get("guidance")
    assert guidance and "declared path" in guidance[0]["text"] and guidance[0]["by"] == "senior_pm"
    from supervisor import packets
    assert packets.GUIDANCE_HEADING in packets.delivery_brief(layout, t)
    state = json.loads((layout.runtime / "escalation" / "T.json").read_text())
    assert state["resets"] == 1 and state["revisions"] <= 1          # fresh budget (the synchronous job may already be retrying)
    hist = [h for h in t.history if h["to"] == "READY" and "requeue" in h["note"]]
    assert hist, "the task was re-queued by the diagnosis"
    issue = store.read(layout.issues / "I-001.yaml")
    assert issue["diagnosis"][0]["action"] == "requeue"
    assert inbox.open_items(layout, "owner") == []


def test_owner_diagnosis_opens_exactly_one_inbox_item(layout, fake_claude):
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "owner"
    failing_task(layout, "T")
    run_until(layout, lambda: inbox.open_items(layout, "owner"))
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1 and items[0]["task_id"] == "T" and "spend on the GPU" in items[0]["summary"]
    assert items[0]["allowed_responses"] == ["REQUEUE", "CANCEL", "DEFER"]
    assert tasks.load(layout, "T").status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
    Supervisor(layout, sleep_s=0).once(); Supervisor(layout, sleep_s=0).once()
    assert len(inbox.open_items(layout, "owner")) == 1               # stays one


def test_manager_hands_a_design_question_to_the_architect_once(layout, fake_claude):
    os.environ["FAKE_DIAGNOSIS_ACTION_SENIOR_PM"] = "architect"
    os.environ["FAKE_DIAGNOSIS_ACTION_CHIEF_ARCHITECT"] = "defer"
    failing_task(layout, "T")
    run_until(layout, lambda: any(r.get("diag_level") == "architect" and r.get("diag_action") for r in diag_records(layout, "T")))
    levels = [(r["diag_level"], r.get("diag_action")) for r in diag_records(layout, "T")]
    assert levels == [("manager", "architect"), ("architect", "defer")]
    t = tasks.load(layout, "T")
    assert t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
    assert any("deferred" in n["note"] and "missing runtime" in n["note"] for n in (t.model_extra or {}).get("notes", []))
    assert inbox.open_items(layout, "owner") == []


def test_a_garbage_diagnosis_falls_through_to_the_owner_without_looping(layout, fake_claude):
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "GARBAGE"
    failing_task(layout, "T")
    run_until(layout, lambda: inbox.open_items(layout, "owner"))
    Supervisor(layout, sleep_s=0).once()
    assert len(diag_records(layout, "T")) == 1                        # no second attempt at diagnosing
    events = [json.loads(e)["event"] for e in layout.log.read_text().splitlines()]
    assert "diagnosis_failed" in events


def test_an_architect_may_not_escalate_to_the_architect_again(layout, fake_claude):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    rec = jobs.write(layout, {"attempt_id": "T.a9", "task_id": "T", "adapter": "claude_cli", "phase": "RUNNING",
                              "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": "architect", "diag_role": "chief_architect",
                              "diag_signature": "s", "diag_issue": None})
    out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "architect", "summary": "s"}))
    assert out["action"] == "owner" and len(inbox.open_items(layout, "owner")) == 1


def test_diagnosis_budget_sends_the_third_requeue_to_the_owner(layout, fake_claude):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "BLOCKED")
    sup = Supervisor(layout, sleep_s=0)
    for n in range(1, 4):
        rec = jobs.write(layout, {"attempt_id": f"T.a{n}", "task_id": "T", "adapter": "claude_cli", "phase": "RUNNING",
                                  "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": "manager", "diag_role": "senior_pm",
                                  "diag_signature": f"s{n}", "diag_issue": None})
        out = diagnosis.ingest(sup, rec, Result(status="COMPLETE", raw={"kind": "diagnosis", "action": "requeue", "summary": "again",
                                                                          "guidance": "try harder"}))
        tasks.set_status(layout, "T", "BLOCKED") if n < 3 else None
        if n <= diagnosis.DIAGNOSIS_BUDGET:
            assert out["action"] == "requeue"
        else:
            assert out["action"] == "owner"
    assert len(inbox.open_items(layout, "owner")) == 1
    # and escalation itself goes straight to the owner once the budget is spent
    outcome = escalation.record_failure(sup, "T", "verification", "problem a")
    outcome = escalation.record_failure(sup, "T", "verification", "problem a")
    assert outcome["action"] == "escalate" and outcome.get("owner", {}).get("action") == "owner"
    assert len(diag_records(layout, "T")) == 3                        # no fourth diagnosis


def test_diagnosis_route_can_be_disabled_in_routing(layout, fake_claude):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"escalation": {"diagnosis": False}}))
    failing_task(layout, "T")
    run_until(layout, lambda: list(layout.issues.glob("I-*.yaml")))
    Supervisor(layout, sleep_s=0).once()
    assert diag_records(layout, "T") == []


def test_a_diagnosis_launch_does_not_flip_the_blocked_task_to_in_progress(layout, fake_claude, monkeypatch, tmp_path):
    """The fake manager is held until the launch state has been checked: without the hold it could answer, and the
    same loop iteration ingest its diagnosis, before the assertions ran (a test race, not a loop defect)."""
    release = tmp_path / "release-diagnosis"
    monkeypatch.setenv("FAKE_DIAGNOSIS_HOLD", str(release))
    failing_task(layout, "T")
    try:
        sup = run_until(layout, lambda: diag_records(layout, "T"))
        rec = diag_records(layout, "T")[0]
        assert rec["phase"] in ("SUBMITTED", "RUNNING")
        assert tasks.load(layout, "T").status in (TaskStatus.FAILED, TaskStatus.BLOCKED)   # never IN_PROGRESS
    finally:
        release.write_text("go", encoding="utf-8")
    deadline = time.time() + 25
    while time.time() < deadline and jobs.read(layout, rec["attempt_id"])["phase"] != "DONE":
        sup.once()
        time.sleep(0.1)
    assert jobs.read(layout, rec["attempt_id"])["phase"] == "DONE"                     # released, then ingested


# ------------------------------------------------------------------ §2 checkpoints

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


@pytest.fixture
def repo(layout):
    root = layout.root
    assert git(root, "init", "-q").returncode == 0
    git(root, "config", "user.email", "t@example.com"); git(root, "config", "user.name", "tests")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("runs/\nstate/runtime/\nstate/log.jsonl\nstate/.lock\n")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "init")
    return root


def complete_task(layout, tid, name=None):
    ptask(layout, tid, name=name)
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, tid).status == TaskStatus.COMPLETE
    return tasks.load(layout, tid)


def test_accepted_work_is_committed_exactly_as_accepted(layout, repo):
    (layout.root / "unrelated.txt").write_text("owner's own edit, must not be swept in")
    t = complete_task(layout, "A")                      # the loop step after completion checkpoints it
    t = tasks.load(layout, "A")
    cp = (t.model_extra or {}).get("checkpoint")
    assert cp and cp["status"] == "committed" and cp["paths"] == ["A.txt"] and cp["digest"] == t.delivery.digest
    log = git(repo, "log", "--format=%s", "-n", "1").stdout.strip()
    assert log.startswith("A: A (delivery " + t.delivery.digest[:12])
    shown = git(repo, "show", "--stat", "--format=", "HEAD").stdout
    assert "A.txt" in shown and "state/tasks/A.yaml" in shown and "unrelated.txt" not in shown
    assert "unrelated.txt" in git(repo, "status", "--short").stdout   # left for the owner, untouched
    # idempotent: nothing more happens on later iterations
    Supervisor(layout, sleep_s=0).once()
    assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"


def test_a_changed_accepted_file_refuses_the_checkpoint_and_opens_an_issue(layout, repo, monkeypatch):
    # stop the loop's own checkpoint step so we can tamper between acceptance and checkpoint
    monkeypatch.setattr("supervisor.loop.checkpoint_mod.run", lambda layout_, *a, **k: [])
    t = complete_task(layout, "A")
    (layout.root / "A.txt").write_text("tampered after acceptance")
    monkeypatch.undo()
    out = checkpoint.run(layout)
    assert out and out[0]["status"] == "refused"
    cp = tasks.load(layout, "A").model_extra["checkpoint"]
    assert cp["status"] == "refused" and cp["expected"] == t.delivery.digest and cp["found"] != cp["expected"]
    issues = list(layout.issues.glob("I-*.yaml"))
    assert len(issues) == 1 and "accepted contents changed" in issues[0].read_text()
    assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"   # nothing committed
    assert checkpoint.run(layout) == []                                        # not retried on its own


def test_ignored_paths_are_listed_not_committed_and_pre_since_tasks_are_left_alone(layout, repo):
    layout.config.mkdir(parents=True, exist_ok=True)
    complete_task(layout, "OLD")
    cp_old = tasks.load(layout, "OLD").model_extra["checkpoint"]
    assert cp_old["status"] == "committed"
    # a task whose only delivered path is git-ignored
    (layout.root / "runs").mkdir(exist_ok=True)
    ptask(layout, "R", name="runs/R.txt")
    Supervisor(layout, sleep_s=0).once()
    cp = tasks.load(layout, "R").model_extra["checkpoint"]
    assert cp["status"] == "skipped" and cp["ignored"] == ["runs/R.txt"]
    # since: tasks completed before the cut-off are not checkpointed
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"checkpoint": {"since": "2999-01-01T00:00:00+00:00"}}))
    ptask(layout, "N")
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "N").status == TaskStatus.COMPLETE
    assert not (tasks.load(layout, "N").model_extra or {}).get("checkpoint")
    assert checkpoint.pending(layout) == []


def test_no_git_repository_is_a_recorded_skip(layout):
    complete_task(layout, "A")
    cp = tasks.load(layout, "A").model_extra["checkpoint"]
    assert cp["status"] == "skipped" and "git" in cp["reason"]


# ------------------------------------------------------------------ §3 verification as a tracked job

def _pytest_file(layout, name, body):
    (layout.root / name).write_text(body, encoding="utf-8")
    return f"{sys.executable} -m pytest -p no:cacheprovider {name}"


def verify_records(layout, tid):
    return [r for r in jobs.for_task(layout, tid) if (r.get("spec") or {}).get("kind") == "verify"]


def checked_task(layout, tid, body):
    make_task(layout, tid)
    cmd = _pytest_file(layout, f"t_{tid}.py", body)
    tasks._update(layout, tid, lambda d: d.update(job={"adapter": "python", "fn": "write_artifact2", "params": {"name": f"{tid}.txt"}},
                                                  artifacts=[f"{tid}.txt"], checks=[{"command": cmd}]))
    return cmd


def test_commands_run_in_a_tracked_verify_job_and_pass(layout):
    cmd = checked_task(layout, "T", "def test_ok():\n    assert True\n")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.IN_PROGRESS and t.delivery.digest and t.delivery.verification is None   # awaiting the job
    recs = verify_records(layout, "T")
    assert len(recs) == 1 and recs[0]["adapter"] == "process" and recs[0]["verify_digest"] == t.delivery.digest
    assert recs[0]["phase"] in ("SUBMITTED", "RUNNING")
    run_until(layout, lambda: tasks.load(layout, "T").status == TaskStatus.COMPLETE, timeout=60)
    v = tasks.load(layout, "T").delivery.verification
    assert v.ok and "declared tests passed" in v.detail and v.report and v.report.endswith("verification.json")
    report = json.loads((layout.root / v.report).read_text())
    assert report["tests"][0]["command"] == cmd and report["tests"][0]["exit_code"] == 0


def test_a_failing_check_in_the_verify_job_fails_verification_and_escalates(layout):
    checked_task(layout, "T", "def test_no():\n    assert False\n")
    run_until(layout, lambda: tasks.load(layout, "T").delivery.verification is not None, timeout=60)
    t = tasks.load(layout, "T")
    assert t.delivery.verification.ok is False and "required check failed" in t.delivery.verification.detail
    state = json.loads((layout.runtime / "escalation" / "T.json").read_text())
    assert state["revisions"] == 1 and "T.a1" in state["events"]          # keyed by the delivery attempt, not the verify job


def test_artifact_only_verification_stays_inline(layout):
    ptask(layout, "P")
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE and verify_records(layout, "P") == []


def test_verify_job_runs_under_drain_so_delivered_work_gets_verified_before_stopping(layout, monkeypatch):
    from supervisor import shutdown as shutdown_mod
    monkeypatch.setattr(shutdown_mod, "run_command", lambda argv: (0, ""))
    checked_task(layout, "T", "def test_ok():\n    assert True\n")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    assert verify_records(layout, "T")
    runtime.set_decision(layout, "DRAIN")
    assert gate.check(layout, tasks.load(layout, "T"), "process", "verify", []) is None       # allowed under DRAIN
    assert gate.check(layout, tasks.load(layout, "T"), "python", "delivery", []) == "decision is DRAIN"
    deadline = time.time() + 60
    decision = "DRAIN"
    while time.time() < deadline and decision == "DRAIN":
        time.sleep(0.2)
        decision = sup.once()
    assert decision == "STOP" and tasks.load(layout, "T").status == TaskStatus.COMPLETE


def test_inline_verification_when_the_job_mode_is_off(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"verification_job": {"as_job": False}}))
    checked_task(layout, "T", "def test_ok():\n    assert True\n")
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "T").status == TaskStatus.COMPLETE and verify_records(layout, "T") == []


def test_a_stale_verify_result_is_ignored_when_a_newer_delivery_exists(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.record_delivery(layout, "T", "n" * 64, actor="test", paths=["T.txt"])
    sup = Supervisor(layout, sleep_s=0)
    rec = jobs.write(layout, {"attempt_id": "T.a5", "task_id": "T", "adapter": "process", "phase": "RUNNING",
                              "spec": {"kind": "verify", "run_dir": str(layout.runs / "T" / "T.a5")}, "handle": {},
                              "verify_digest": "o" * 64, "verify_delivery": "T.a4"})
    sup.ingest_verify(rec, Result(status="COMPLETE", raw={"verification": {"ok": True, "detail": "x", "report": {}}}))
    assert tasks.load(layout, "T").delivery.verification is None
