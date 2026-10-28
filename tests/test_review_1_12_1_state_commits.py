"""Review 1.12.1, round 1. Two state-commit defects the reviewer reproduced:

R1 (P1): a task may deliver a directory (the delivery manifest walks it), and the "not yet accepted" exclusion compared
exact path strings, so a file inside a delivered directory was committed before the task's acceptance. The exclusion
now covers everything under a delivered path, whatever form the path is written in (a trailing slash, a backslash,
`./`, the project root itself), for state records and projections alike.

R2 (P2): a failed commit (a pre-commit hook refusing it, say) left the files it had staged in the index, and every later
attempt read them as someone else's staging and skipped. A state commit now builds its commit in a temporary index of
its own and, once it has committed, sets only its paths in the project's index; when anything fails, the project's
index is exactly as it was."""
from __future__ import annotations

import subprocess

import pytest

from supervisor import inbox, state_commit, tasks
from supervisor.models.task import TaskStatus
from tests.test_loop import ptask


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


@pytest.fixture
def repo(layout):
    root = layout.root
    assert git(root, "init", "-q").returncode == 0
    git(root, "config", "user.email", "t@example.com"); git(root, "config", "user.name", "tests")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("runs/\nstate/runtime/\nstate/.lock\nstate/**/*.lock\nstate/**/*.lock.owner\n")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "init")
    return root


def in_head(root, path) -> bool:
    return git(root, "cat-file", "-e", f"HEAD:{path}").returncode == 0


def draft_task(layout, paths):
    ptask(layout, "DRAFT")
    tasks._update(layout, "DRAFT", lambda d: d.update(paths=paths, status=TaskStatus.WAITING_FOR_REVIEW.value))


@pytest.mark.parametrize("delivered", ["state/decisions", "state/decisions/", "state\\decisions", "./state/decisions"])
def test_a_file_inside_a_delivered_directory_waits_for_acceptance(layout, repo, delivered):
    """The reviewer's probe: the task delivered the directory, and a file under it is not yet accepted."""
    draft_task(layout, [delivered])
    layout.decisions.mkdir(parents=True, exist_ok=True)
    (layout.decisions / "draft.md").write_text("unaccepted draft\n")
    out = state_commit.commit(layout, reason="probe")
    assert out["status"] == "committed" and "state/tasks/DRAFT.yaml" in out["paths"], out
    assert "state/decisions/draft.md" not in out["paths"]
    assert not in_head(repo, "state/decisions/draft.md")


def test_a_task_delivering_the_project_root_holds_back_every_file(layout, repo):
    draft_task(layout, ["."])
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    (repo / "STATE.md").write_text("rendered\n")
    assert state_commit.commit(layout, reason="probe")["status"] == "unchanged"


def test_a_delivered_directory_holds_back_a_projection_inside_it_too(layout, repo, monkeypatch):
    monkeypatch.setattr(state_commit, "projections", lambda layout_: ["views/BOARD.md"])
    draft_task(layout, ["views"])
    (repo / "views").mkdir()
    (repo / "views" / "BOARD.md").write_text("a projection inside the delivered folder\n")
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    out = state_commit.commit(layout, reason="probe")
    assert out["status"] == "committed" and "views/BOARD.md" not in out["paths"]


def test_a_refused_commit_leaves_the_index_as_it_was_and_a_retry_commits(layout, repo):
    """The reviewer's probe: a pre-commit hook refuses the first commit, then is removed."""
    (repo / "owner.md").write_text("the owner's staged file\n")
    git(repo, "add", "owner.md")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(b"#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    first = state_commit.commit(layout, reason="probe")
    assert first["status"] == "failed", first
    assert git(repo, "diff", "--cached", "--name-only").stdout.split() == ["owner.md"]   # only the owner's staging
    hook.unlink()
    retry = state_commit.commit(layout, reason="probe")
    assert retry["status"] == "committed", retry
    assert any(p.startswith("state/inbox/") for p in retry["paths"])
    assert git(repo, "diff", "--cached", "--name-only").stdout.split() == ["owner.md"]   # still the owner's, untouched
    assert not in_head(repo, "owner.md")
    assert git(repo, "status", "--porcelain", "--", "state").stdout.strip() == ""        # index and HEAD agree


def test_after_a_commit_the_index_matches_it_for_its_paths(layout, repo):
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    out = state_commit.commit(layout, reason="probe")
    assert out["status"] == "committed"
    for p in out["paths"]:
        assert git(repo, "diff", "--cached", "--name-only", "--", p).stdout.strip() == "", p
        assert git(repo, "diff", "--name-only", "--", p).stdout.strip() == "", p
