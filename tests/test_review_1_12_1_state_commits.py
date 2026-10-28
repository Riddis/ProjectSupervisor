"""Review 1.12.1, round 1. State-commit defects the reviewer reproduced:

R1 (P1): a task may deliver a directory (the delivery manifest walks it), and the "not yet accepted" exclusion compared
exact path strings, so a file inside a delivered directory was committed before the task's acceptance. The exclusion
now covers everything under a delivered path, whatever form the path is written in (a trailing slash, a backslash,
`./`, the project root itself), for state records and projections alike."""
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
