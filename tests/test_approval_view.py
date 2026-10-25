"""An approval shows what it approves: the inbox card and the task page render the files frozen in the digest,
the verification and the live reviews of that digest, and warn about declared artifacts the digest does not cover.
A decision record under state/decisions/ is a deliverable and is frozen in the digest like any other file."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from supervisor import inbox as inbox_mod
from supervisor import jobs, tasks
from supervisor import loop as loop_mod
from supervisor.adapters import Result
from supervisor.loop import Supervisor
from supervisor.ui import delivery
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task

DIGEST = "d" * 64


@pytest.fixture
def client(layout):
    c = TestClient(create_app(layout), base_url="http://127.0.0.1")
    c.cookies.set(CSRF_COOKIE, "csrf")
    return c


def delivered(layout, artifacts=("state/decisions/D-1.md", "config/p/*.yaml"), paths=None, review=None):
    """T delivered, verified and reviewed on DIGEST, waiting for the owner, with an approval item open."""
    make_task(layout, "T", human="required", reviewer=["senior_pm"])
    tasks._update(layout, "T", lambda doc: doc.__setitem__("artifacts", list(artifacts)))
    (layout.state / "decisions").mkdir(parents=True, exist_ok=True)
    (layout.state / "decisions" / "D-1.md").write_text("# Decision\n\n| a | b |\n|---|---|\n| <x> | 2 |\n", encoding="utf-8")
    (layout.root / "config" / "p").mkdir(parents=True, exist_ok=True)
    (layout.root / "config" / "p" / "one.yaml").write_text("role: primary\n", encoding="utf-8")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    paths = ["config/p/one.yaml", "state/decisions/D-1.md"] if paths is None else paths
    tasks.record_delivery(layout, "T", DIGEST, actor="test", paths=paths)
    tasks.record_verification(layout, "T", DIGEST, ok=True, detail="artifacts present; declared tests passed")
    report = layout.runs / "T" / "T.a2" / "result.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(review or {"verdict": "ACCEPT", "summary": "Re-derived the totals.",
                                            "findings": [{"category": "quality", "blocking": False,
                                                          "message": "a test is vacuous", "evidence": "x"}]}),
                      encoding="utf-8")
    tasks.record_review(layout, "T", DIGEST, "senior_pm", "ACCEPT", report="runs/T/T.a2/result.json")
    return inbox_mod.create(layout, for_role="owner", subject="Approve delivery: T", summary="T needs approval",
                            kind=inbox_mod.APPROVAL_KIND, allowed_responses=inbox_mod.APPROVAL_RESPONSES,
                            from_="loop", task_id="T", approval_digest=DIGEST)


def test_the_approval_card_shows_the_files_the_review_and_the_verification(layout, client):
    delivered(layout)
    page = client.get("/inbox").text
    assert "config/p/one.yaml" in page and "role: primary" in page          # the file itself, not only its name
    assert "state/decisions/D-1.md" in page and "&lt;x&gt;" in page         # escaped, never rendered as HTML
    assert "Re-derived the totals." in page and "a test is vacuous" in page
    assert "senior pm" in page and "ACCEPT" in page
    assert "declared tests passed" in page
    assert "Not covered by this approval" not in page


def test_a_declared_artifact_outside_the_digest_is_named_on_the_card(layout, client):
    delivered(layout, paths=["config/p/one.yaml"])
    page = client.get("/inbox").text
    assert "Not covered by this approval" in page and "state/decisions/D-1.md" in page


def test_an_approval_for_a_superseded_digest_shows_no_files(layout, client):
    delivered(layout)
    tasks.record_delivery(layout, "T", "e" * 64, actor="test", paths=["config/p/one.yaml"])
    page = client.get("/inbox").text
    assert "no longer the task&#39;s current delivery" in page or "no longer the task's current delivery" in page
    assert "role: primary" not in page


def test_delivered_files_open_by_index_only(layout, client):
    delivered(layout)
    t = tasks.load(layout, "T")
    i = t.paths.index("state/decisions/D-1.md")
    r = client.get(f"/tasks/T/files/{i}")
    assert r.status_code == 200 and r.text.startswith("# Decision")
    assert r.headers["content-type"].startswith("text/plain")
    assert client.get(f"/tasks/T/files/{len(t.paths)}").status_code == 404
    assert client.get("/tasks/T/files/-1").status_code == 404


def test_the_task_page_shows_the_current_delivery(layout, client):
    delivered(layout)
    page = client.get("/tasks/T").text
    assert "Current delivery" in page and "role: primary" in page and "a test is vacuous" in page


def test_uncovered_matches_globs_and_exact_names(layout):
    delivered(layout, artifacts=("config/p/*.yaml", "state/decisions/D-1.md", "docs/missing.md"))
    assert delivery.uncovered(tasks.load(layout, "T")) == ["docs/missing.md"]


# ------------------------------------------------------------------ the digest covers decision records

def test_a_decision_record_is_frozen_in_the_delivery_digest(layout, monkeypatch):
    make_task(layout, "T")
    tasks._update(layout, "T", lambda doc: doc.__setitem__("artifacts", ["x", "state/decisions/D-9.md"]))
    (layout.root / "x").write_text("x", encoding="utf-8")
    (layout.state / "decisions" / "D-9.md").write_text("# D-9\n", encoding="utf-8")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    seen = {}

    class Recorded(Exception):
        pass

    def record(lay, task_id, digest, **kw):               # stop at the delivery: what follows is not under test
        seen.update(kw)
        raise Recorded
    monkeypatch.setattr(loop_mod.tasks_mod, "record_delivery", record)
    rec = jobs.write(layout, {"attempt_id": "T.a1", "task_id": "T", "adapter": "python", "phase": "RUNNING",
                              "spec": {"kind": "delivery"}, "handle": {}})
    with pytest.raises(Recorded):
        Supervisor(layout, sleep_s=0).ingest(rec, Result(status="COMPLETE", artifacts=["state/tasks/T.yaml"]))
    assert seen["paths"] == ["state/decisions/D-9.md", "x"]              # the store file is still dropped
    dropped = [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines()
               if '"artifacts_dropped"' in line]
    assert dropped and dropped[-1]["dropped"] == ["state/tasks/T.yaml"]


def test_record_delivery_keeps_authored_state_and_drops_the_store(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    t = tasks.record_delivery(layout, "T", DIGEST, actor="test",
                              paths=["state/decisions/D-1.md", "state/inbox/H-001.yaml", "a.txt"])
    assert t.paths == ["a.txt", "state/decisions/D-1.md"]
