"""“Issues never close by themselves”:
`issue.resolve` was the only close path and had no CLI subcommand and no UI route — only `supervisor cmd
issue.resolve` — so 14 issues in the records sat open with every task they named already terminal. The fix marks
an issue `recovered`, with the attempt and digest that did it, the moment the task it names reaches COMPLETE — a
status distinct from an explicit `resolved`, so the task completing never silently closes the issue — and adds
the entry points: `supervisor issue resolve` and a Control Center Issues page."""
from typer.testing import CliRunner

from supervisor import commands, escalation, render, store, tasks
from supervisor.cli import app
from tests.conftest import make_task


def _open_issue_for(layout, task_id, signature="sig"):
    return escalation.open_issue(layout, title="worker run failed repeatedly", severity="high", owner_role="engineer",
                                 detail="d", resolution_task=task_id, signature=signature)


def test_completing_the_named_task_recovers_the_issue_with_its_delivery(layout):
    make_task(layout, "T", status="IN_PROGRESS")
    issue = _open_issue_for(layout, "T")

    tasks.record_delivery(layout, "T", "d1")
    tasks.record_verification(layout, "T", "d1", ok=True)
    t = tasks.advance(layout, "T", attempt_id="T.a3")

    assert t.status.value == "COMPLETE"
    doc = store.read(layout.issues / f"{issue['id']}.yaml")
    assert doc["status"] == "recovered"
    assert doc["recovered_attempt"] == "T.a3" and doc["recovered_digest"] == "d1" and doc["recovered_at"]


def test_a_recovered_issue_is_not_counted_as_open_work(layout):
    make_task(layout, "T", status="IN_PROGRESS")
    _open_issue_for(layout, "T")
    tasks.record_delivery(layout, "T", "d1")
    tasks.record_verification(layout, "T", "d1", ok=True)
    tasks.advance(layout, "T", attempt_id="T.a3")

    lines = render.state_lines(layout)
    issues_line = next(ln for ln in lines if "Issues" in ln)
    assert "0 open" in issues_line and "1 recovered" in issues_line and "0 resolved" in issues_line


def test_recovery_does_not_silently_close_the_issue_an_explicit_resolve_still_moves_it_to_resolved(layout):
    make_task(layout, "T", status="IN_PROGRESS")
    issue = _open_issue_for(layout, "T")
    tasks.record_delivery(layout, "T", "d1")
    tasks.record_verification(layout, "T", "d1", ok=True)
    tasks.advance(layout, "T", attempt_id="T.a3")
    assert store.read(layout.issues / f"{issue['id']}.yaml")["status"] == "recovered"   # not resolved by itself

    out = commands.submit_or_apply(layout, "issue.resolve",
                                   {"issue_id": issue["id"], "resolution": "confirmed fixed"}, actor="owner")
    assert out["ok"] and out["result"]["status"] == "resolved"
    assert store.read(layout.issues / f"{issue['id']}.yaml")["status"] == "resolved"


def test_cli_issue_resolve_queues_the_command(layout):
    make_task(layout, "T")
    issue = _open_issue_for(layout, "T")
    r = CliRunner().invoke(app, ["issue", "resolve", issue["id"], "--resolution", "fixed by hand"])
    assert r.exit_code == 0, r.output
    assert store.read(layout.issues / f"{issue['id']}.yaml")["status"] == "resolved"


def test_control_center_resolve_action_queues_the_command(layout):
    from fastapi.testclient import TestClient

    from supervisor.ui.app import create_app
    from supervisor.ui.auth import CSRF_COOKIE

    make_task(layout, "T")
    issue = _open_issue_for(layout, "T")
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set(CSRF_COOKIE, "test-csrf")
    r = client.post(f"/issues/{issue['id']}/resolve",
                    data={"csrf_token": "test-csrf", "resolution": "via the control center"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.read(layout.issues / f"{issue['id']}.yaml")["status"] == "resolved"
