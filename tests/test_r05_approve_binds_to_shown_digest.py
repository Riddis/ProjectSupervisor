"""R-05: approving must bind to the delivery the owner's page actually showed, not to whatever is current on disk
when the form arrives. A delivery that changes while the page is open must make the old form refuse, not approve
the new bytes unseen — from the task page and from the inbox's approval card."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from supervisor import inbox as inbox_mod
from supervisor import tasks
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


@pytest.fixture
def client(layout):
    c = TestClient(create_app(layout), base_url="http://127.0.0.1")
    c.cookies.set(CSRF_COOKIE, "csrf")
    return c


def _ready(layout, digest):
    make_task(layout, "A", human="required")
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", digest, actor="test", paths=[])
    tasks.record_verification(layout, "A", digest, ok=True)
    tasks.advance(layout, "A")


def test_a_page_rendered_for_an_older_digest_is_refused_when_a_newer_delivery_lands(layout, client):
    _ready(layout, DIGEST_A)
    page = client.get("/tasks/A").text
    assert DIGEST_A[:16] in page                                     # the page shows the digest it will post back

    tasks.record_delivery(layout, "A", DIGEST_B, actor="test", paths=[])   # a newer delivery lands while the page is open
    tasks.record_verification(layout, "A", DIGEST_B, ok=True)
    tasks.advance(layout, "A")

    r = client.post("/tasks/A/approve", data={"csrf_token": "csrf", "response": "ACCEPT", "note": "",
                                              "digest": DIGEST_A}, follow_redirects=False)
    assert r.status_code in (303, 409, 422)
    t = tasks.load(layout, "A")
    assert t.status.value == "WAITING_FOR_HUMAN"
    assert t.delivery.approvals == []


def test_a_page_rendered_for_the_current_digest_still_approves(layout, client):
    _ready(layout, DIGEST_A)
    r = client.post("/tasks/A/approve", data={"csrf_token": "csrf", "response": "ACCEPT", "note": "",
                                              "digest": DIGEST_A}, follow_redirects=False)
    assert r.status_code == 303
    t = tasks.load(layout, "A")
    assert t.status.value == "COMPLETE"
    assert [a.digest for a in t.delivery.approvals] == [DIGEST_A]


def _inbox_card(layout, digest):
    _ready(layout, digest)
    return inbox_mod.create(layout, for_role="owner", subject="Approve delivery: A", summary="A needs approval",
                            kind=inbox_mod.APPROVAL_KIND, allowed_responses=inbox_mod.APPROVAL_RESPONSES,
                            from_="loop", task_id="A", approval_digest=digest)


def test_the_inbox_card_for_an_older_digest_is_refused_when_a_newer_delivery_lands(layout, client):
    item = _inbox_card(layout, DIGEST_A)
    page = client.get("/inbox").text
    assert f'name="digest" value="{DIGEST_A}"' in page       # the card's own form carries the digest it shows

    tasks.record_delivery(layout, "A", DIGEST_B, actor="test", paths=[])
    tasks.record_verification(layout, "A", DIGEST_B, ok=True)
    tasks.advance(layout, "A")

    client.post(f"/inbox/{item['id']}/answer", data={"csrf_token": "csrf", "response": "ACCEPT",
                                                      "digest": DIGEST_A})
    t = tasks.load(layout, "A")
    assert t.status.value == "WAITING_FOR_HUMAN"
    assert t.delivery.approvals == []
    assert inbox_mod.load(layout, item["id"])["status"] == "open"   # a refused answer is not spent


def test_the_inbox_card_for_the_current_digest_still_approves(layout, client):
    item = _inbox_card(layout, DIGEST_A)
    client.post(f"/inbox/{item['id']}/answer", data={"csrf_token": "csrf", "response": "ACCEPT",
                                                      "digest": DIGEST_A})
    t = tasks.load(layout, "A")
    assert t.status.value == "COMPLETE"
    assert [a.digest for a in t.delivery.approvals] == [DIGEST_A]


def _agent_written_question(layout, digest):
    """F3 (round 1's finding, deferred through 2.6.1/2.7.0): a task WAITING_FOR_HUMAN with no attempt behind
    it (an agent asked the owner the question the loop would otherwise have asked) is itself an approval
    target (inbox.approval_target's second shape) but the item this leaves open carries no `approval_digest`
    field at all - R-05's fix only ever gave a hidden digest field to the first shape."""
    _ready(layout, digest)
    return inbox_mod.create(layout, for_role="owner", subject="A needs a decision",
                            summary="the task is waiting on you; can I proceed with it as delivered?",
                            kind="question", from_="agent", task_id="A")


def test_the_card_for_an_agent_written_question_shows_the_digest_it_binds_to(layout, client):
    """F3: the card must show what it would approve, the same way the loop-opened approval card already
    does, so the owner's browser actually has a digest to carry back."""
    _agent_written_question(layout, DIGEST_A)
    page = client.get("/inbox").text
    assert f'name="digest" value="{DIGEST_A}"' in page


def test_the_agent_written_card_for_an_older_digest_is_refused_when_a_newer_delivery_lands(layout, client):
    item = _agent_written_question(layout, DIGEST_A)
    page = client.get("/inbox").text
    assert f'name="digest" value="{DIGEST_A}"' in page       # the card's own form carries the digest it shows

    tasks.record_delivery(layout, "A", DIGEST_B, actor="test", paths=[])
    tasks.record_verification(layout, "A", DIGEST_B, ok=True)
    tasks.advance(layout, "A")

    client.post(f"/inbox/{item['id']}/answer", data={"csrf_token": "csrf", "response": "ACCEPT",
                                                      "digest": DIGEST_A})
    t = tasks.load(layout, "A")
    assert t.status.value == "WAITING_FOR_HUMAN"
    assert t.delivery.approvals == []
    assert inbox_mod.load(layout, item["id"])["status"] == "open"   # a refused answer is not spent


def test_the_agent_written_card_for_the_current_digest_still_approves(layout, client):
    item = _agent_written_question(layout, DIGEST_A)
    client.post(f"/inbox/{item['id']}/answer", data={"csrf_token": "csrf", "response": "ACCEPT",
                                                      "digest": DIGEST_A})
    t = tasks.load(layout, "A")
    assert t.status.value == "COMPLETE"
    assert [a.digest for a in t.delivery.approvals] == [DIGEST_A]
