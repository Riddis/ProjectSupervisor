"""An approval shows what it approves: the inbox card and the task page render the files frozen in the digest,
the verification and the live reviews of that digest, and warn about declared artifacts the digest does not cover.
A decision record under state/decisions/ is a deliverable and is frozen in the digest like any other file."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from supervisor import inbox as inbox_mod
from supervisor import jobs, tasks
from supervisor import loop as loop_mod
from supervisor._vendor.contract import digest as digest_of, manifest
from supervisor.adapters import Result
from supervisor.loop import Supervisor
from supervisor.ui import delivery
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task

DIGEST = "d" * 64          # a digest no files hash to: only for records whose files are not shown


def real_digest(layout, paths):
    return digest_of(manifest([layout.root / p for p in paths], root=layout.root))


@pytest.fixture
def client(layout):
    c = TestClient(create_app(layout), base_url="http://127.0.0.1")
    c.cookies.set(CSRF_COOKIE, "csrf")
    return c


def delivered(layout, artifacts=("state/decisions/D-1.md", "config/p/*.yaml"), paths=None, review=None):
    """T delivered, verified and reviewed on the real digest of its files, waiting for the owner, with an approval
    item open. Returns (item, digest)."""
    make_task(layout, "T", human="required", reviewer=["senior_pm"])
    tasks._update(layout, "T", lambda doc: doc.__setitem__("artifacts", list(artifacts)))
    (layout.state / "decisions").mkdir(parents=True, exist_ok=True)
    (layout.state / "decisions" / "D-1.md").write_text("# Decision\n\n| a | b |\n|---|---|\n| <x> | 2 |\n", encoding="utf-8")
    (layout.root / "config" / "p").mkdir(parents=True, exist_ok=True)
    (layout.root / "config" / "p" / "one.yaml").write_text("role: primary\n", encoding="utf-8")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    paths = ["config/p/one.yaml", "state/decisions/D-1.md"] if paths is None else paths
    dg = real_digest(layout, paths)
    tasks.record_delivery(layout, "T", dg, actor="test", paths=paths)
    tasks.record_verification(layout, "T", dg, ok=True, detail="artifacts present; declared tests passed")
    report = layout.runs / "T" / "T.a2" / "result.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(review or {"verdict": "ACCEPT", "summary": "Re-derived the totals.",
                                            "findings": [{"category": "quality", "blocking": False,
                                                          "message": "a test is vacuous", "evidence": "x"}]}),
                      encoding="utf-8")
    tasks.record_review(layout, "T", dg, "senior_pm", "ACCEPT", report="runs/T/T.a2/result.json")
    item = inbox_mod.create(layout, for_role="owner", subject="Approve delivery: T", summary="T needs approval",
                            kind=inbox_mod.APPROVAL_KIND, allowed_responses=inbox_mod.APPROVAL_RESPONSES,
                            from_="loop", task_id="T", approval_digest=dg)
    return item, dg


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


def test_delivered_files_open_by_index_and_digest_only(layout, client):
    _, dg = delivered(layout)
    t = tasks.load(layout, "T")
    i = t.paths.index("state/decisions/D-1.md")
    r = client.get(f"/tasks/T/files/{i}?digest={dg}")
    assert r.status_code == 200 and r.text.startswith("# Decision")
    assert r.headers["content-type"].startswith("text/plain")
    assert client.get(f"/tasks/T/files/{len(t.paths)}?digest={dg}").status_code == 404
    assert client.get(f"/tasks/T/files/-1?digest={dg}").status_code == 404
    assert f"/tasks/T/files/{i}?digest={dg}" in client.get("/tasks/T").text   # the page links carry the digest


def test_a_link_without_or_with_an_old_digest_serves_nothing(layout, client):
    _, dg = delivered(layout)
    assert client.get("/tasks/T/files/0").status_code == 409
    (layout.root / "config" / "p" / "two.yaml").write_text("role: other\n", encoding="utf-8")
    new_paths = ["config/p/two.yaml", "state/decisions/D-1.md"]          # index 0 now names another file
    tasks.record_delivery(layout, "T", real_digest(layout, new_paths), actor="test", paths=new_paths)
    r = client.get(f"/tasks/T/files/0?digest={dg}")                       # the old page's link
    assert r.status_code == 409 and "role: other" not in r.text


def test_a_file_edited_after_the_delivery_is_never_shown_or_served(layout, client):
    _, dg = delivered(layout)
    (layout.root / "config" / "p" / "one.yaml").write_text("role: UNREVIEWED replacement\n", encoding="utf-8")
    page = client.get("/inbox").text
    assert "no longer match digest" in page
    assert "UNREVIEWED" not in page and "role: primary" not in page
    view = delivery.view(layout, tasks.load(layout, "T"), dg)
    assert view["current"] and not view["intact"] and view["files"] == []
    i = tasks.load(layout, "T").paths.index("config/p/one.yaml")
    r = client.get(f"/tasks/T/files/{i}?digest={dg}")
    assert r.status_code == 409 and "UNREVIEWED" not in r.text
    assert "UNREVIEWED" not in client.get("/tasks/T").text


def test_bytes_that_differ_from_their_manifest_entry_are_refused(layout):
    """read_frozen() hashes the bytes it returns: a file swapped after frozen() checked the digest is refused."""
    _, dg = delivered(layout)
    t = tasks.load(layout, "T")
    entries = delivery.frozen(layout, t, dg)
    on_disk = (layout.root / "config" / "p" / "one.yaml").read_bytes()
    assert delivery.read_frozen(layout, "config/p/one.yaml", entries) == on_disk
    (layout.root / "config" / "p" / "one.yaml").write_text("role: swapped\n", encoding="utf-8")
    assert delivery.read_frozen(layout, "config/p/one.yaml", entries) is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_a_delivered_file_replaced_by_a_link_is_not_shown(layout, client):
    _, dg = delivered(layout)
    target = layout.root / "config" / "p" / "one.yaml"
    (layout.root / "elsewhere.yaml").write_text("role: primary\n", encoding="utf-8")   # same bytes, via a link
    target.unlink()
    try:
        os.symlink(layout.root / "elsewhere.yaml", target)
    except OSError:
        pytest.skip("creating symlinks needs privileges here")
    assert not delivery.view(layout, tasks.load(layout, "T"), dg)["intact"]
    assert client.get(f"/tasks/T/files/0?digest={dg}").status_code == 409


def test_the_task_page_shows_the_current_delivery(layout, client):
    delivered(layout)
    page = client.get("/tasks/T").text
    assert "Current delivery" in page and "role: primary" in page and "a test is vacuous" in page


def test_an_image_thumbnail_is_made_from_the_verified_bytes(layout, client):
    from PIL import Image
    (layout.root / "out").mkdir()
    Image.new("RGB", (8, 8), "red").save(layout.root / "out" / "a.png")
    _, dg = delivered(layout, artifacts=("out/*.png",), paths=["out/a.png"])
    view = delivery.view(layout, tasks.load(layout, "T"), dg)
    [f] = view["files"]
    assert f["kind"] == "image" and f["thumb_key"].endswith("-256x256.jpg")
    r = client.get(f"/tasks/T/files/0?digest={dg}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


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
