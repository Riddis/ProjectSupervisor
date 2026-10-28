"""1.12.1, F-B: the supervisor commits its own records. A checkpoint commits exactly a completed task's accepted paths
and its record; everything else the loop keeps in tracked files was never committed: the other task records, the
inbox, issues, gates, applied commands, the seed, the decision records, the projections, the checkpoint's own final
record (written after its commit) and what `supervisor migrate` rewrote. A live project collected over a hundred
uncommitted changes that way, while docs/ARCHITECTURE.md said "the rest is committed by checkpoints".

A state commit names its paths (`git commit -- <paths>`), so the owner's edits elsewhere, staged or not, stay as they
are. It commits `state/` and the projections only when something under `state/` changed (a projection carries a
render stamp, so projections alone would commit on every render), right after a checkpoint and otherwise at most
every `checkpoint.state_interval_minutes`. It never commits a path a task has delivered but not yet had accepted, nor a
path someone else has staged. `checkpoint.state_commits: false` turns it off."""
from __future__ import annotations

import re
import subprocess

import pytest
import yaml

from supervisor import checkpoint, inbox, migrations, state_commit, store, tasks
from supervisor.loop import Supervisor
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


def commits(root) -> int:
    return int(git(root, "rev-list", "--count", "HEAD").stdout.strip())


def dirty(root, *paths) -> list[str]:
    return [line[3:] for line in git(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
            .stdout.splitlines()]


def settings(layout, **values):
    cfg = layout.config / "routing.yaml"
    doc = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}) if cfg.exists() else {}
    doc.setdefault("checkpoint", {}).update(values)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_the_loops_records_are_committed_and_the_owners_edits_left_alone(layout, repo):
    (repo / "notes.md").write_text("the owner's unstaged edit\n")
    (repo / "staged.md").write_text("the owner's staged edit\n")
    git(repo, "add", "staged.md")
    inbox.create(layout, for_role="owner", subject="a question", summary="s")
    tasks.add_note(layout, *_any_task(layout), actor="test")
    out = state_commit.commit(layout, reason="test")
    assert out["status"] == "committed", out
    shown = git(repo, "show", "--name-only", "--format=%s", "HEAD").stdout.splitlines()
    assert shown[0].startswith("state:")
    assert any(p.startswith("state/inbox/") for p in shown) and any(p.startswith("state/tasks/") for p in shown)
    assert "notes.md" not in shown and "staged.md" not in shown
    assert dirty(repo, "state") == []
    status = git(repo, "status", "--porcelain").stdout
    assert "?? notes.md" in status and "A  staged.md" in status        # exactly as the owner left them


def test_nothing_changed_commits_nothing(layout, repo):
    state_commit.commit(layout, reason="first")
    n = commits(repo)
    assert state_commit.commit(layout, reason="again")["status"] == "unchanged"
    assert commits(repo) == n


def test_projections_alone_are_not_committed_but_ride_along_with_state(layout, repo):
    state_commit.commit(layout, reason="baseline")
    n = commits(repo)
    (repo / "STATE.md").write_text("rendered again at a new time\n")
    assert state_commit.commit(layout, reason="render only")["status"] == "unchanged"
    assert commits(repo) == n
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    assert state_commit.commit(layout, reason="state too")["status"] == "committed"
    assert "STATE.md" in git(repo, "show", "--name-only", "--format=", "HEAD").stdout


def test_the_checkpoints_final_record_is_committed_after_it(layout, repo):
    ptask(layout, "A")
    Supervisor(layout, sleep_s=0).once()                   # completes, checkpoints, then commits the loop's records
    t = tasks.load(layout, "A")
    assert t.status == TaskStatus.COMPLETE and (t.model_extra or {})["checkpoint"]["status"] == "committed"
    assert dirty(repo, "state/tasks/A.yaml") == []
    record = yaml.safe_load(git(repo, "show", "HEAD:state/tasks/A.yaml").stdout)
    assert record["checkpoint"]["status"] == "committed" and record["checkpoint"]["commit"]


def test_a_delivered_path_not_yet_accepted_is_not_committed(layout, repo):
    ptask(layout, "B")
    tasks._update(layout, "B", lambda d: d.update(paths=["state/decisions/D-900-draft.md"],
                                                  status=TaskStatus.WAITING_FOR_REVIEW.value))
    (layout.decisions / "D-900-draft.md").write_text("a draft the reviewers have not seen\n")
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    assert state_commit.commit(layout, reason="test")["status"] == "committed"
    assert dirty(repo, "state/decisions/D-900-draft.md") == ["state/decisions/D-900-draft.md"]


def test_a_state_path_someone_else_staged_stops_the_commit(layout, repo):
    item = inbox.create(layout, for_role="owner", subject="q", summary="s")
    path = next(layout.inbox.glob(f"{item['id']}*.yaml"))
    git(repo, "add", str(path.relative_to(repo)))
    n = commits(repo)
    out = state_commit.commit(layout, reason="test")
    assert out["status"] == "skipped" and "staged" in out["reason"]
    assert commits(repo) == n and git(repo, "diff", "--cached", "--name-only").stdout.strip()


def test_turned_off_nothing_is_committed(layout, repo):
    settings(layout, state_commits=False)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "settings")
    inbox.create(layout, for_role="owner", subject="q", summary="s")
    n = commits(repo)
    assert state_commit.commit(layout, reason="test")["status"] == "skipped"
    Supervisor(layout, sleep_s=0).once()
    assert commits(repo) == n


def test_between_checkpoints_the_loop_commits_at_most_every_interval(layout, repo, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(state_commit, "_now", lambda: now[0])
    inbox.create(layout, for_role="owner", subject="q1", summary="s")
    assert state_commit.maybe(layout, after_checkpoint=False)["status"] == "committed"
    inbox.create(layout, for_role="owner", subject="q2", summary="s")
    now[0] += 60
    assert state_commit.maybe(layout, after_checkpoint=False) is None           # within the 15 minutes
    assert state_commit.maybe(layout, after_checkpoint=True)["status"] == "committed"   # a checkpoint: at once
    inbox.create(layout, for_role="owner", subject="q3", summary="s")
    now[0] += 15 * 60
    assert state_commit.maybe(layout, after_checkpoint=False)["status"] == "committed"


def test_migrate_commits_what_it_rewrote_but_not_a_file_that_had_changes_before(layout, repo):
    """A task record in the old cost category is rewritten by the framework's migration. routing.yaml carries an
    owner's uncommitted edit: migrate's commit leaves it for the owner."""
    ptask(layout, "C")
    record = layout.tasks / "C.yaml"                       # the old value, which the model no longer accepts
    record.write_text(re.sub(r"(?m)^cost_category: .*$", "cost_category: local_gpu",
                             record.read_text(encoding="utf-8")), encoding="utf-8")
    assert "cost_category: local_gpu" in record.read_text(encoding="utf-8")
    (layout.root / "project.yaml").write_text('name: "Demo"\nframework_version: "1.12.0"\n', encoding="utf-8")
    routing = layout.config / "routing.yaml"
    routing.parent.mkdir(parents=True, exist_ok=True)
    routing.write_text("# routing\n", encoding="utf-8")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "old format")
    routing.write_text("# routing\n# the owner's note\n", encoding="utf-8")
    notes = migrations.run(layout)
    assert any("cost_category" in n or "local_gpu" in n for n in notes), notes
    head = git(repo, "show", "--name-only", "--format=%s", "HEAD").stdout.splitlines()
    assert head[0].startswith("migrate:") and "state/tasks/C.yaml" in head and "config/routing.yaml" not in head
    assert dirty(repo, "config/routing.yaml") == ["config/routing.yaml"]
    assert dirty(repo, "state/tasks/C.yaml") == []


def test_no_git_repository_is_a_skip(layout):
    assert state_commit.commit(layout, reason="test")["status"] == "skipped"


def _any_task(layout):
    ptask(layout, "N")
    return ("N", "a note")


def test_a_project_inside_another_repository_commits_nothing_there(tmp_path, monkeypatch):
    from supervisor import testing
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q"); git(outer, "config", "user.email", "t@example.com"); git(outer, "config", "user.name", "t")
    (outer / "README.md").write_text("outer\n"); git(outer, "add", "-A"); git(outer, "commit", "-qm", "init")
    lay = testing.project(outer / "nested")
    inbox.create(lay, for_role="owner", subject="q", summary="s")
    assert state_commit.commit(lay, reason="test")["status"] == "skipped"
    assert commits(outer) == 1
