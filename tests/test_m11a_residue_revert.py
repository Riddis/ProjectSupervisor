"""M11(a), first shape only (2.5.0): a write inside a verification window the attempt under verification did not
make. Of three shapes (the attempt's own cleanup of a previous attempt's edit of an unowned path; another attempt
running concurrently; a human edit outside Settings), this release builds the loop's revert for the first shape
only - its own recurrence ("two edits, two cleanups" in an earlier release): a rejected attempt's write outside its
owned paths stayed in the tree, so the RETRY's own before-manifest saw the residue, and the retry's innocent
restore of it was then charged as *its* own undeclared write - the exact defect this file reproduces failing, then
fixes.

The second shape (a concurrently dispatched attempt) is explicitly left out: a prior release ruled it out in
writing ("concurrent-attempt blame for undeclared writes — out ... a new mechanism and a larger release than this
one") and 2.5.0 does not reopen it - no regression is owed for a shape that stays unbuilt, and it stays named here
rather than silently dropped. The third shape (a human edit outside Settings) is not mechanically separable from
the first by this same mechanism, and `test_a_human_edit_of_an_unowned_path_is_also_reverted` below shows that the
revert does not need to tell them apart to be safe: it quarantines before it restores, so a wrongly reverted human
edit is recoverable, never silently lost."""
from __future__ import annotations

import subprocess

import pytest

from supervisor import tasks, verify
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


@pytest.fixture
def repo(layout):
    """A real git repository at the project root: the mechanism restores a reverted path's content from `git show
    HEAD:<path>`, gated on the recorded sha256 - the framework already assumes git in the project root (it
    checkpoints the store itself), so the test gives it one."""
    root = layout.root
    assert git(root, "init", "-q").returncode == 0
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "tests")
    git(root, "config", "commit.gpgsign", "false")
    (root / "other").mkdir(parents=True, exist_ok=True)
    (root / "other" / "shared.txt").write_text("original", encoding="utf-8")
    (root / ".gitignore").write_text("runs/\nstate/\n.venv/\n", encoding="utf-8")
    git(root, "add", "-A")
    assert git(root, "commit", "-q", "-m", "init").returncode == 0
    return root


@pytest.fixture
def scoped(layout, repo):
    make_task(layout, "T")

    def fn(doc):
        doc["owned_paths"] = ["owned"]
        doc["artifacts"] = ["owned/out.txt"]
        doc["job"] = {"adapter": "python", "fn": "noop"}

    tasks._update(layout, "T", fn)
    tasks.set_status(layout, "T", "IN_PROGRESS")
    (layout.root / "owned").mkdir(exist_ok=True)
    return tasks.load(layout, "T")


def test_a_rejected_attempts_own_residue_is_reverted_and_quarantined(layout, scoped, repo):
    """a1 writes an unowned path outside its scope; rejected for exactly that. The loop must revert the residue at
    rejection, quarantining a1's own content under its run directory (nothing is deleted)."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a1 edit", encoding="utf-8")   # a1's own undeclared write
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False and "other/shared.txt" in outcome1.detail
    assert outcome1.report.get("residue", {}).get("other/shared.txt")

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    sup = Supervisor(layout, sleep_s=0)
    sup.record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    # the tree is clean again - restored to what it was before a1 ever touched it
    assert (layout.root / "other" / "shared.txt").read_text(encoding="utf-8") == "original"
    # nothing is deleted: a1's own content is quarantined under its own run dir
    assert (run_dir1 / "undeclared" / "other" / "shared.txt").read_text(encoding="utf-8") == "a1 edit"
    log_text = (layout.state / "log.jsonl").read_text(encoding="utf-8")
    assert '"event": "residue_reverted"' in log_text and '"path": "other/shared.txt"' in log_text


def test_the_retrys_own_cleanup_of_already_reverted_residue_is_not_charged(layout, scoped, repo):
    """The exact recurrence: a2 is dispatched after a1's rejection and, unaware the loop already fixed it, 'cleans
    up' the unowned path itself (rewrites it back to the original value). Before the fix this is a
    genuine diff against a2's own before-snapshot (which still showed a1's residue) and a2 is wrongly rejected for
    an undeclared write. After the fix, a1's rejection already reverted the path, so a2's before-snapshot is already
    clean and its own identical rewrite is a no-op - no undeclared change, no false rejection."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a1 edit", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    sup = Supervisor(layout, sleep_s=0)
    sup.record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    # a2 dispatches: its own before-manifest is taken AFTER a1's rejection-triggered revert
    before2 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 2", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("original", encoding="utf-8")   # a2's own "cleanup"
    outcome2 = verify.verify(layout, task, ["owned/out.txt"], before=before2)
    assert outcome2.ok is True, outcome2.detail


def test_a_human_edit_of_an_unowned_path_is_also_reverted(layout, scoped, repo):
    """§5.1: the revert cannot tell a human's edit of an unowned path from an agent's - and must not pretend to. A
    change to `other/shared.txt` present in a rejected attempt's own manifest is reverted and quarantined the same
    way whatever made it, which is what makes the mechanism safe for a human edit too: nothing is deleted, so a
    wrongly reverted edit is recoverable from the attempt's own run directory."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "shared.txt").write_text("a human edited this, not the agent", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    Supervisor(layout, sleep_s=0).record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    assert (layout.root / "other" / "shared.txt").read_text(encoding="utf-8") == "original"
    assert (run_dir1 / "undeclared" / "other" / "shared.txt").read_text(encoding="utf-8") == \
        "a human edited this, not the agent"


def test_a_path_git_cannot_account_for_is_left_exactly_as_found_and_named(layout, scoped, repo):
    """A before-digest git's own HEAD does not match (here: the path was never committed at all, so `git show`
    fails) is never guessed at - left exactly as found, and named on the task rather than silently dropped."""
    task = scoped
    run_dir1 = layout.runs / "T" / "T.a1"
    run_dir1.mkdir(parents=True)
    (layout.root / "other" / "uncommitted.txt").write_text("already here, never committed", encoding="utf-8")
    before1 = verify.snapshot(layout, task)
    (layout.root / "owned" / "out.txt").write_text("delivered 1", encoding="utf-8")
    (layout.root / "other" / "uncommitted.txt").write_text("the attempt's own edit", encoding="utf-8")
    outcome1 = verify.verify(layout, task, ["owned/out.txt"], before=before1)
    assert outcome1.ok is False

    tasks.record_delivery(layout, "T", "dg1", actor="loop", paths=["owned/out.txt"])
    rec1 = {"task_id": "T", "attempt_id": "T.a1", "adapter": "python", "spec": {"run_dir": str(run_dir1)}}
    Supervisor(layout, sleep_s=0).record_outcome("T", "dg1", outcome1, rec1, substantive=False, worker_status="FAILED")

    # left exactly as found: never reverted, never quarantined, never deleted
    assert (layout.root / "other" / "uncommitted.txt").read_text(encoding="utf-8") == "the attempt's own edit"
    assert not (run_dir1 / "undeclared" / "other" / "uncommitted.txt").exists()
    t = tasks.load(layout, "T")
    notes = (t.model_extra or {}).get("notes") or []
    assert any("not reverted" in (n.get("note") or "") and "other/uncommitted.txt" in (n.get("note") or "")
              for n in notes)
