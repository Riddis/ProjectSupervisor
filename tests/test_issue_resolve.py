"""2.1.0 change 3: `issue.resolve` is the only queued command that closes an issue record (id, resolution,
resolving task, actor) — before this, nothing could, which is why L8's carried issues stay open forever however
the work that caused them turns out. Rendered in ISSUES.md; no other command handler writes state/issues/."""
import inspect

from supervisor import commands, escalation, render, store
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
