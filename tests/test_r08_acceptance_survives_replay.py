"""R-08: acceptance must survive a replay and a git error.

A replayed task.approve/task.verify/task.review must leave the task at the status its own records imply, not stuck
wherever a crash between the durable write and advance() left it. A checkpoint that fails once on a transient git
error must be retried on a later turn without anyone clearing a field. A checkpoint that refuses a delivery whose
accepted bytes changed must keep those bytes out of every state commit that follows, not only out of its own."""
from __future__ import annotations

import subprocess

import pytest

from supervisor import checkpoint, commands, state_commit, tasks
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_loop import ptask


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


# ------------------------------------------------------------------ a replayed command finishes the transition

def test_a_replayed_approval_reaches_complete(layout):
    make_task(layout, "A", human="required", status="IN_PROGRESS", tests=[])
    digest = "a" * 64
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    assert tasks.load(layout, "A").status == TaskStatus.WAITING_FOR_HUMAN
    # the first pass wrote the approval and died before advance(); the command is replayed
    tasks.record_approval(layout, "A", digest, "ACCEPT", "owner", "", command_id="cmd-1")
    out = commands.HANDLERS["task.approve"](layout, {"task_id": "A", "digest": digest, "response": "ACCEPT",
                                                      "_id": "cmd-1"}, "owner")
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE, "the replay must finish what the record implies"
    assert out.get("status") == "COMPLETE"


def test_a_replayed_verification_reaches_complete(layout):
    make_task(layout, "A", tests=[])
    tasks.set_status(layout, "A", "IN_PROGRESS")
    digest = "b" * 64
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)         # the first pass died before advance()
    assert tasks.load(layout, "A").status == TaskStatus.IN_PROGRESS
    out = commands.HANDLERS["task.verify"](layout, {"task_id": "A", "digest": digest, "ok": True,
                                                     "_id": "cmd-2"}, "verify")
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE
    assert out.get("status") == "COMPLETE"


def test_a_replayed_review_reaches_complete(layout):
    make_task(layout, "A", reviewer=["chief_architect"], tests=[])
    tasks.set_status(layout, "A", "IN_PROGRESS")
    digest = "c" * 64
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    assert tasks.load(layout, "A").status == TaskStatus.WAITING_FOR_REVIEW
    tasks.record_review(layout, "A", digest, "chief_architect", "ACCEPT")   # the first pass died before advance()
    out = commands.HANDLERS["task.review"](layout, {"task_id": "A", "digest": digest, "role": "chief_architect",
                                                     "verdict": "ACCEPT", "_id": "cmd-3"}, "chief_architect")
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE
    assert out.get("status") == "COMPLETE"


def test_the_loop_reconciles_a_task_whose_records_already_satisfy_completion(layout):
    """No command is replayed here at all: the durable records exist, advance() was simply never called (the same
    crash window, caught a different way) — the loop's own turn must notice and finish the transition on its own."""
    make_task(layout, "A", tests=[])
    tasks.set_status(layout, "A", "IN_PROGRESS")
    digest = "d" * 64
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    assert tasks.load(layout, "A").status == TaskStatus.IN_PROGRESS
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE


# ------------------------------------------------------------------ a checkpoint survives one transient git error

def test_a_transient_checkpoint_failure_is_retried_on_a_later_turn_and_committed(layout, repo, monkeypatch):
    monkeypatch.setattr("supervisor.loop.checkpoint_mod.run", lambda layout_, *a, **k: [])
    ptask(layout, "A")
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE
    monkeypatch.undo()

    real_git = checkpoint.run_git
    calls = {"n": 0}

    def flaky(root, *args):
        if args[:1] == ("add",) and calls["n"] == 0:
            calls["n"] += 1
            return 1, "fatal: Unable to create '.git/index.lock': File exists."
        return real_git(root, *args)
    monkeypatch.setattr(checkpoint, "run_git", flaky)

    first = checkpoint.run(layout)
    assert first and first[0]["status"] == "failed"
    assert tasks.load(layout, "A").model_extra["checkpoint"]["status"] == "failed"

    second = checkpoint.run(layout)                 # a later turn: nobody cleared anything
    assert second and second[0]["status"] == "committed", second
    assert tasks.load(layout, "A").model_extra["checkpoint"]["status"] == "committed"


# ------------------------------------------------------------------ a refused checkpoint's bytes never ride along

def test_a_refused_checkpoints_changed_bytes_never_enter_a_state_commit(layout, repo, monkeypatch):
    monkeypatch.setattr("supervisor.loop.checkpoint_mod.run", lambda layout_, *a, **k: [])
    ptask(layout, "A", name="state/decisions/D-900.md")
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE
    monkeypatch.undo()

    (layout.root / "state" / "decisions" / "D-900.md").write_text("tampered after acceptance", encoding="utf-8")

    refused = checkpoint.run(layout)
    assert refused and refused[0]["status"] == "refused"

    out = state_commit.commit(layout, reason="test")
    shown = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines() if out["status"] == "committed" else []
    assert "state/decisions/D-900.md" not in shown
    assert git(repo, "status", "--porcelain", "--", "state/decisions/D-900.md").stdout.strip(), \
        "the tampered bytes must stay uncommitted, not swept into the loop's own bookkeeping commit"
