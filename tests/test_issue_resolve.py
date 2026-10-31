"""2.1.0 change 3: `issue.resolve` is the only queued command that closes an issue record (id, resolution,
resolving task, actor) — before this, nothing could, which is why L8's carried issues stay open forever however
the work that caused them turns out. Rendered in ISSUES.md; no other command handler writes state/issues/."""
import inspect

from supervisor import commands, escalation, render, store, tasks
from tests.conftest import make_task


def test_issue_resolve_closes_the_record_and_shows_resolved_in_the_projection(layout):
    make_task(layout, "T")
    issue = escalation.open_issue(layout, title="repeated worker_failed failure", severity="high",
                                  owner_role="engineer", detail="d", resolution_task="T", signature="s1")
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": issue["id"], "resolution": "fixed by T2-02's job_fix",
                                    "resolving_task": "T2-02"}, actor="senior_pm")
    assert out["ok"] and out["result"] == {"id": issue["id"], "status": "resolved"}
    doc = store.read(layout.issues / f"{issue['id']}.yaml")
    assert doc["status"] == "resolved" and doc["resolution"] == "fixed by T2-02's job_fix"
    assert doc["resolved_by"] == "senior_pm" and doc["resolved_by_task"] == "T2-02" and doc["resolved_at"]
    rendered = render.render_issues(layout).read_text(encoding="utf-8")
    assert "resolved" in rendered and "fixed by T2-02's job_fix" in rendered and "senior_pm" in rendered

    # resolving an already-resolved issue is a no-op: no second write, no duplicate log event
    out2 = commands.submit_or_apply(layout, "issue.resolve",
                                    {"issue_id": issue["id"], "resolution": "again", "resolving_task": "X"},
                                    actor="someone_else")
    assert out2["ok"] and out2["result"] == {"skipped": "already resolved", "id": issue["id"]}
    doc2 = store.read(layout.issues / f"{issue['id']}.yaml")
    assert doc2["resolution"] == "fixed by T2-02's job_fix" and doc2["resolved_by"] == "senior_pm"   # unchanged


def test_issue_resolve_refuses_an_unknown_issue(layout):
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": "I-999", "resolution": "x"}, actor="senior_pm")
    assert out["ok"] is False and "no such issue" in out["error"]


def test_issue_resolve_is_the_only_command_handler_that_writes_state_issues(layout):
    for kind, fn in commands.HANDLERS.items():
        if kind == "issue.resolve":
            continue
        src = inspect.getsource(fn)
        assert "layout.issues" not in src, f"{kind} touches state/issues/ too"


# R3 (docs/reviews/RELEASE_REVIEW_2.1.0.md): `issue_id` is forwarded straight into `layout.issues / f"{id}.yaml"`,
# so an id naming another record overwrites it. Each case below targets a real task record and must be refused
# before any write — the reviewer's probe (runs/T2-05/T2-05.a1/probes.py, observation issue_path_traversal) is the
# first one, verbatim.

def test_issue_resolve_refuses_a_traversal_id_and_leaves_the_task_record_unchanged(layout):
    make_task(layout, "T")
    target = layout.tasks / "T.yaml"
    before = target.read_bytes()
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": "../tasks/T", "resolution": "probe only", "resolving_task": "T"},
                                   actor="probe-reviewer")
    assert out["ok"] is False and "invalid issue id" in out["error"]
    assert target.read_bytes() == before
    assert tasks.load(layout, "T").status.value == "NOT_STARTED"


def test_issue_resolve_refuses_an_absolute_id_and_leaves_the_task_record_unchanged(layout):
    """pathlib's `/` operator discards the left operand entirely when the right one is absolute, so an absolute
    `issue_id` reaches any file on disk through the very same `layout.issues / f'{id}.yaml'` join — a second,
    independent way to the same outcome as the traversal case, not caught by rejecting '..' alone."""
    make_task(layout, "T")
    target = layout.tasks / "T.yaml"
    before = target.read_bytes()
    abs_id = str(layout.tasks / "T")
    assert (layout.issues / f"{abs_id}.yaml") == target          # demonstrates the override before exercising it
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": abs_id, "resolution": "probe only", "resolving_task": "T"},
                                   actor="probe-reviewer")
    assert out["ok"] is False and "invalid issue id" in out["error"]
    assert target.read_bytes() == before
    assert tasks.load(layout, "T").status.value == "NOT_STARTED"


def test_issue_resolve_refuses_a_backslash_separator_id_and_leaves_the_task_record_unchanged(layout):
    """A separator alone, not just the textual '..', must be rejected: backslash is a path separator on Windows
    (the platform this project runs on) even though the id-format check below never special-cases it."""
    make_task(layout, "T")
    target = layout.tasks / "T.yaml"
    before = target.read_bytes()
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": "..\\tasks\\T", "resolution": "probe only", "resolving_task": "T"},
                                   actor="probe-reviewer")
    assert out["ok"] is False and "invalid issue id" in out["error"]
    assert target.read_bytes() == before
    assert tasks.load(layout, "T").status.value == "NOT_STARTED"


def test_issue_resolve_refuses_a_malformed_id_and_writes_nothing(layout):
    """Not a separator, not traversal, not absolute — just the wrong shape (the supported id is exactly the one
    `_next_issue_id` generates, `I-<digits>`). No file for it exists before or after the refusal."""
    candidate = layout.issues / "I-07x.yaml"
    assert not candidate.exists()
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": "I-07x", "resolution": "probe only"}, actor="probe-reviewer")
    assert out["ok"] is False and "invalid issue id" in out["error"]
    assert not candidate.exists()


def test_issue_resolve_refuses_a_contained_record_that_is_not_shaped_like_an_issue(layout):
    """Format and containment both pass, but the record at that path is not an issue (no `severity`, not an
    open/resolved `status`) — the write must still be refused on identity/type, not just on the path string."""
    layout.issues.mkdir(parents=True, exist_ok=True)
    bogus = layout.issues / "I-500.yaml"
    before = store.write_new(bogus, {"id": "I-500", "note": "not an issue", "status": "NOT_STARTED"})
    before_bytes = bogus.read_bytes()
    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": "I-500", "resolution": "probe only"}, actor="probe-reviewer")
    assert out["ok"] is False and "not an issue" in out["error"]
    assert bogus.read_bytes() == before_bytes


def test_issue_resolve_command_replay_is_a_no_op(layout):
    """The queued-command path (not just the direct call): a second submission under the same command id must not
    re-apply, even with a different payload."""
    make_task(layout, "T")
    issue = escalation.open_issue(layout, title="t", severity="low", owner_role="engineer", detail="d",
                                  resolution_task="T", signature="s2")
    commands.submit(layout, "issue.resolve", {"issue_id": issue["id"], "resolution": "fixed", "resolving_task": "T"},
                    actor="probe-reviewer", command_id="replay-once")
    from supervisor.locks import FileLock
    with FileLock(layout.lock, owner="test"):
        first = commands.apply_all(layout)
    doc = store.read(layout.issues / f"{issue['id']}.yaml")
    assert first[0]["ok"] and doc["resolved_by"] == "probe-reviewer" and doc["resolved_by_task"] == "T"
    commands.submit(layout, "issue.resolve", {"issue_id": issue["id"], "resolution": "replacement"},
                    actor="someone-else", command_id="replay-once")
    with FileLock(layout.lock, owner="test"):
        second = commands.apply_all(layout)
    assert second == [] and store.read(layout.issues / f"{issue['id']}.yaml") == doc
