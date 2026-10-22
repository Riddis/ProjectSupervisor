"""Control Center inbox tests: the "needs you" screen against the FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from supervisor import inbox as inbox_mod
from supervisor import tasks as tasks_mod
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.test_inbox import human_task

CSRF = "test-csrf-value"


@pytest.fixture
def app(layout):
    return create_app(layout)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def auth_client(layout, client):
    client.cookies.set(CSRF_COOKIE, CSRF)
    return client


def post(client, url, **data):
    return client.post(url, data={"csrf_token": CSRF, **data}, follow_redirects=False)


def test_inbox_page_renders_and_groups_by_kind(layout, auth_client):
    inbox_mod.create(layout, for_role="owner", subject="approve plan", summary="see docs", kind="gate",
                     allowed_responses=["APPROVE", "MODIFY", "REJECT"])
    inbox_mod.create(layout, for_role="owner", subject="pick spend", summary="$10 for X", kind="spend")
    r = auth_client.get("/inbox")
    assert r.status_code == 200
    assert "approve plan" in r.text
    assert "pick spend" in r.text
    assert "Gate approvals" in r.text
    assert "Spend proposals" in r.text


def test_h001_style_item_renders_fully(layout, auth_client):
    """An H-001-style item: markdown-ish summary plus Q1..Q8 sub-items (per-topic defaults, not score blocks)."""
    inbox_mod.create(layout, for_role="owner", subject="Approve the architecture & implementation plan",
                     summary="Plan at docs/plan/ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md.\n\n- whole vs cherry-pick\n- keep on D:",
                     kind="gate", allowed_responses=["APPROVE", "MODIFY", "REJECT"])
    item_id = inbox_mod.open_items(layout)[0]["id"]

    from supervisor import store

    def fn(doc):
        return {**doc, "items": [{"id": "Q1", "topic": "disclosure stance", "default": "disclosed"},
                                 {"id": "Q2", "topic": "second labeller", "default": "owner-only"}]}

    store.update(inbox_mod.path_for(layout, item_id), fn, lock_path=layout.state / ".inbox.lock")

    r = auth_client.get("/inbox")
    assert r.status_code == 200
    assert "disclosure stance" in r.text
    assert "<strong>" in r.text  # bold markdown rendered, not left as raw "**"
    assert "<li>" in r.text      # bullet list rendered


def test_answering_an_item_sets_the_task_to_the_next_state(layout, auth_client):
    human_task(layout, "H", subject="decide X", summary="pick one")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    assert tasks_mod.load(layout, "H").status == TaskStatus.WAITING_FOR_HUMAN
    item = inbox_mod.open_items(layout, "owner")[0]

    r = post(auth_client, f"/inbox/{item['id']}/answer", response="ACCEPT")
    assert r.status_code == 303
    assert "flash=applied" in r.headers["location"]
    assert inbox_mod.load(layout, item["id"])["status"] == "answered"

    sup.once()
    assert tasks_mod.load(layout, "H").status == TaskStatus.COMPLETE


def test_modify_text_is_combined_with_the_structured_token(layout, auth_client):
    human_task(layout, "H", subject="decide X", summary="pick one")
    Supervisor(layout, sleep_s=0).once()
    item = inbox_mod.open_items(layout, "owner")[0]

    r = post(auth_client, f"/inbox/{item['id']}/answer", response="MODIFY", text="longer hair")
    assert r.status_code == 303
    answered = inbox_mod.load(layout, item["id"])
    assert answered["answer"]["response"] == "MODIFY longer hair"
    assert answered["answer"]["kind"] == "MODIFY"


def test_invalid_response_rejected(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="s", summary="body",
                            allowed_responses=["ACCEPT", "REJECT"], free_text=False)
    r = post(auth_client, f"/inbox/{item['id']}/answer", response="BOGUS")
    assert r.status_code == 303
    assert "flash=error" in r.headers["location"]
    assert inbox_mod.load(layout, item["id"])["status"] == "open"


def test_empty_answer_rejected(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="s", summary="body")
    r = post(auth_client, f"/inbox/{item['id']}/answer")
    assert r.status_code == 400
    assert inbox_mod.load(layout, item["id"])["status"] == "open"


def test_snooze_then_reopen(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="s", summary="body")
    r = post(auth_client, f"/inbox/{item['id']}/snooze")
    assert r.status_code == 303
    assert inbox_mod.load(layout, item["id"])["status"] == "snoozed"
    assert inbox_mod.open_items(layout) == []

    r2 = auth_client.get("/inbox?tab=archive")
    assert item["id"] in r2.text

    r3 = post(auth_client, f"/inbox/{item['id']}/reopen")
    assert r3.status_code == 303
    assert inbox_mod.load(layout, item["id"])["status"] == "open"


def test_dropped_file_lands_under_uploads_with_a_sidecar_stub(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="need the export", summary="drop the exported file")
    r = auth_client.post(f"/inbox/{item['id']}/upload", data={"csrf_token": CSRF},
                         files={"file": ("export.csv", b"a,b\n1,2\n", "text/csv")}, follow_redirects=False)
    assert r.status_code == 303
    dest = layout.uploads / item["id"] / "export.csv"
    assert dest.exists()
    assert dest.read_bytes() == b"a,b\n1,2\n"
    sidecar = dest.with_name("export.csv.json")
    assert sidecar.exists()
    import json
    stub = json.loads(sidecar.read_text(encoding="utf-8"))
    assert stub["item_id"] == item["id"]
    assert stub["original_filename"] == "export.csv"
    assert stub["status"] == "pending_ingest"


def test_upload_strips_path_characters_from_the_filename_and_stays_contained(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="s", summary="body")
    r = auth_client.post(f"/inbox/{item['id']}/upload", data={"csrf_token": CSRF},
                         files={"file": ("../../x.png", b"x", "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    assert (layout.uploads / item["id"] / "x.png").exists()
    assert not (layout.root.parent / "x.png").exists()


def test_upload_to_an_unknown_item_is_404(layout, auth_client):
    r = auth_client.post("/inbox/H-999/upload", data={"csrf_token": CSRF},
                         files={"file": ("x.png", b"x", "image/png")}, follow_redirects=False)
    assert r.status_code == 404
    assert not layout.uploads.exists()


def test_gate_ack_recorded_with_actor_and_timestamp(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="G0 owner acknowledgement", summary="phase 0 done",
                            kind="gate")

    def fn(d):
        return {**d, "gate_id": "G0"}
    from supervisor import store
    store.update(inbox_mod.path_for(layout, item["id"]), fn, lock_path=layout.state / ".inbox.lock")

    r = post(auth_client, f"/inbox/{item['id']}/gate-ack", note="phase 0 looks good")
    assert r.status_code == 303
    assert "flash=applied" in r.headers["location"]
    from supervisor import gates as gates_mod
    g = gates_mod.load(layout)["gates"]["G0"]
    assert g["owner_ack"]["by"] == "owner-ui"
    assert g["owner_ack"]["note"] == "phase 0 looks good"
    assert g["owner_ack"]["at"]


def test_gate_ack_without_gate_id_rejected(layout, auth_client):
    item = inbox_mod.create(layout, for_role="owner", subject="s", summary="body", kind="gate")
    r = post(auth_client, f"/inbox/{item['id']}/gate-ack")
    assert r.status_code == 400


def test_inbox_badge_count_on_overview(layout, auth_client):
    inbox_mod.create(layout, for_role="owner", subject="s", summary="body")
    r = auth_client.get("/overview")
    assert r.status_code == 200
    assert "/inbox" in r.text
