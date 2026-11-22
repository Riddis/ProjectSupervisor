"""Step 3 of the attempt-identity design: the Overview and the
task page must read a task document on either `Attempt` shape — an un-migrated document (one `Attempt` entry per
job, joined on `Attempt.id`) and a migrated one (`Attempt.n`/`.jobs[]`, `Attempt.id` never set) — without ever
showing a bare "attempt N" for a job that is not a delivery, and without ever reading `Attempt.id` on a migrated
document.
"""
from __future__ import annotations

import re

import pytest

from supervisor import jobs as jobs_mod
from supervisor import tasks as tasks_mod
from tests.conftest import make_task

CSRF = "test-csrf-value"


def post(client, url, **data):
    return client.post(url, data={"csrf_token": CSRF, **data}, follow_redirects=False)


@pytest.fixture
def app(layout):
    from supervisor.ui.app import create_app
    return create_app(layout)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def auth_client(layout, client):
    from supervisor.ui.auth import CSRF_COOKIE
    client.cookies.set(CSRF_COOKIE, CSRF)
    return client


# --------------------------------------------------------- the Overview's "Now running" label


def test_now_running_label_never_reads_attempt_n_alone_for_a_non_delivery_kind(layout, auth_client):
    """Dispatch one job of each kind, all belonging to the same delivery attempt (`attempt=1`). The Overview's
    per-job label must name the kind beside the attempt number — never "attempt 1" on its own — for the
    verification, the review and the diagnosis; the suffix read alone (what the defect does today) cannot tell
    these apart from a fresh try at the deliverable."""
    make_task(layout, "T0-10")
    jobs_mod.prepare(layout, "T0-10", "T0-10.a1.s1", "claude_cli", {"kind": "delivery"},
                     kind="delivery", attempt=1, seq=1)
    for job_id, kind in [("T0-10.a1.v1", "verify"), ("T0-10.a1.r1", "review"), ("T0-10.a1.d1", "diagnosis")]:
        jobs_mod.prepare(layout, "T0-10", job_id, "claude_cli", {"kind": kind}, kind=kind, attempt=1, seq=1)
        jobs_mod.set_phase(layout, job_id, "RUNNING", handle={"started": "2026-01-01T00:00:00+00:00"})
    jobs_mod.set_phase(layout, "T0-10.a1.s1", "RUNNING", handle={"started": "2026-01-01T00:00:00+00:00"})

    from supervisor.ui.routes.overview import now_running
    rows = now_running(jobs_mod.open_records(layout), tasks_mod.load_all(layout), now=1767225600 + 600)
    by_kind = {}
    for r in rows:
        job = jobs_mod.read(layout, r["attempt_id"])
        by_kind[jobs_mod.kind_of(job)] = r
    assert set(by_kind) == {"delivery", "verify", "review", "diagnosis"}
    for kind, row in by_kind.items():
        assert row["attempt"] == 1
        if kind != "delivery":
            assert row["kind_label"] and row["kind_label"] != "delivery", kind

    page = auth_client.get("/overview").text
    # never a bare "attempt 1" span with nothing else inside it
    assert not re.search(r"attempt 1\s*</span>", page)
    assert "verification" in page and "review" in page and "diagnosis" in page


def test_now_running_label_reads_the_promoted_kind_when_spec_lacks_it(layout, auth_client):
    """A record that carries the promoted `kind` field but no `spec["kind"]` (what a migrated-writer dispatch
    produces, and what `jobs.kind_of` exists to read) must still label as its own kind, not fall back to
    "delivery" by reading `spec["kind"]` raw."""
    make_task(layout, "T0-17")
    jobs_mod.prepare(layout, "T0-17", "T0-17.a1.v1", "process", {}, kind="verify", attempt=1, seq=1)
    jobs_mod.set_phase(layout, "T0-17.a1.v1", "RUNNING", handle={"started": "2026-01-01T00:00:00+00:00"})

    from supervisor.ui.routes.overview import now_running
    rows = now_running(jobs_mod.open_records(layout), tasks_mod.load_all(layout), now=1767225600 + 600)
    row = next(r for r in rows if r["attempt_id"] == "T0-17.a1.v1")
    assert row["kind_label"] == "verification"
    assert row["activity"] == "Verifying"


# --------------------------------------------------------- the Attempts table, both shapes


def _old_shape_task(layout, tid):
    """Today's writer: one `Attempt` entry per dispatched job, joined by `Attempt.id` (a job id)."""
    make_task(layout, tid)
    jobs_mod.prepare(layout, tid, f"{tid}.a1", "claude_cli", {"kind": "delivery", "run_dir": str(layout.runs / tid / f"{tid}.a1")})
    jobs_mod.set_phase(layout, f"{tid}.a1", "DONE", result="COMPLETE", finished="now")
    jobs_mod.prepare(layout, tid, f"{tid}.a2", "process", {"kind": "verify", "run_dir": str(layout.runs / tid / f"{tid}.a2")})
    jobs_mod.set_phase(layout, f"{tid}.a2", "DONE", result="COMPLETE", finished="now")

    def fn(doc):
        doc.setdefault("attempts", []).extend([
            {"id": f"{tid}.a1", "resource": "claude_cli", "at": "2026-01-01T00:00:00+00:00",
             "note": "dispatch", "outcome": "ok"},
            {"id": f"{tid}.a2", "resource": "process", "at": "2026-01-01T00:05:00+00:00",
             "note": "verify delivery", "outcome": "ok"},
        ])
    tasks_mod._update(layout, tid, fn)
    return tasks_mod.load(layout, tid)


def _migrated_shape_task(layout, tid):
    """A document already run through the migration: `Attempt.id` is never set; the join key is `jobs[].id`."""
    make_task(layout, tid)
    run_dir_s = str(layout.runs / tid / f"{tid}.a1.s1")
    run_dir_v = str(layout.runs / tid / f"{tid}.a1.v1")
    jobs_mod.prepare(layout, tid, f"{tid}.a1.s1", "claude_cli", {"run_dir": run_dir_s},
                     kind="delivery", attempt=1, seq=1)
    jobs_mod.set_phase(layout, f"{tid}.a1.s1", "DONE", result="COMPLETE", finished="now", model="sonnet")
    jobs_mod.prepare(layout, tid, f"{tid}.a1.v1", "process", {"run_dir": run_dir_v},
                     kind="verify", attempt=1, seq=1, parent=f"{tid}.a1.s1")
    jobs_mod.set_phase(layout, f"{tid}.a1.v1", "DONE", result="COMPLETE", finished="now")

    def fn(doc):
        doc.setdefault("attempts", []).append({
            "n": 1, "opened": "2026-01-01T00:00:00+00:00", "closed": "2026-01-01T00:10:00+00:00",
            "cause": "scheduled", "outcome": "accepted", "digest": "abc123",
            "jobs": [
                {"id": f"{tid}.a1.s1", "kind": "delivery", "seq": 1, "resource": "claude_cli",
                 "run_dir": run_dir_s, "result": "COMPLETE", "note": "dispatch"},
                {"id": f"{tid}.a1.v1", "kind": "verify", "seq": 1, "resource": "process",
                 "run_dir": run_dir_v, "result": "COMPLETE", "note": "verify delivery", "parent": f"{tid}.a1.s1"},
            ],
        })
    tasks_mod._update(layout, tid, fn)
    return tasks_mod.load(layout, tid)


def test_attempts_table_groups_by_attempt_and_shows_a_kind_column_old_shape(layout, auth_client):
    """An un-migrated `Attempt.id` is a job id, not a delivery-attempt number ("an
    existing job id whose kind is whatever happened to be dispatched nth") — `T0-11.a2` here is a verification,
    not a second delivery attempt, so the row must name the job, never assert "Attempt 2" for it."""
    _old_shape_task(layout, "T0-11")
    page = auth_client.get("/tasks/T0-11").text
    assert "<th>Kind</th>" in page
    assert "delivery" in page and "verify" in page
    assert f"T0-11.a1" in page and f"T0-11.a2" in page         # job ids still drive the open-run-dir forms
    assert "Attempt 2" not in page                             # a2 is a verify job, not a second delivery attempt
    assert "Job T0-11.a2" in page


def test_attempts_table_groups_by_attempt_and_shows_a_kind_column_migrated_shape(layout, auth_client):
    t = _migrated_shape_task(layout, "T0-12")
    assert "id" not in t.attempts[0].model_dump()              # the migrated shape never carries an Attempt.id
    page = auth_client.get("/tasks/T0-12").text
    assert "<th>Kind</th>" in page
    assert "delivery" in page and "verify" in page
    assert "T0-12.a1.s1" in page and "T0-12.a1.v1" in page
    assert "sonnet" in page                                    # the Model column reads the job record
    # grouped: the attempt number/label appears once, not once per job row
    assert page.count("Attempt 1<") == 1


def test_no_route_reads_attempt_id_on_a_migrated_document(layout, auth_client):
    """A migrated `Attempt` has no `id` key at all (it is never written, §2.7, and the field itself is gone from
    the model as of step 15); the page must still resolve every job, every kind and the model from `jobs[]`
    alone, and no `id` key must appear on the stored attempt afterwards — nothing on the read path may have
    treated it as a join key and filled it in."""
    t = _migrated_shape_task(layout, "T0-13")
    assert "id" not in t.attempts[0].model_dump()
    r = auth_client.get("/tasks/T0-13")
    assert r.status_code == 200
    assert "sonnet" in r.text
    assert "T0-13.a1.s1" in r.text and "T0-13.a1.v1" in r.text
    assert "id" not in tasks_mod.load(layout, "T0-13").attempts[0].model_dump()


# --------------------------------------------------------- old-shape ids still resolve


def test_old_shape_id_still_opens_its_agent_page(layout, auth_client):
    _old_shape_task(layout, "T0-14")
    r = auth_client.get("/agents/T0-14.a1")
    assert r.status_code == 200
    assert "T0-14.a1" in r.text


def test_open_run_dir_resolves_from_the_job_entry_on_both_shapes(layout, auth_client, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("os.startfile", lambda p: calls.append(p), raising=False)

    old_run_dir = tmp_path / "old_run_dir"
    old_run_dir.mkdir()
    make_task(layout, "T0-15")
    jobs_mod.prepare(layout, "T0-15", "T0-15.a1", "claude_cli", {"kind": "delivery", "run_dir": str(old_run_dir)})

    def fn(doc):
        doc.setdefault("attempts", []).append({"id": "T0-15.a1", "resource": "claude_cli",
                                               "at": "2026-01-01T00:00:00+00:00", "note": "dispatch", "outcome": "ok"})
    tasks_mod._update(layout, "T0-15", fn)
    r = post(auth_client, "/tasks/T0-15/attempts/T0-15.a1/open-run-dir")
    assert r.status_code == 303 and "flash=applied" in r.headers["location"]
    assert calls == [str(old_run_dir)]

    new_run_dir = tmp_path / "new_run_dir"
    new_run_dir.mkdir()
    _migrated_shape_task(layout, "T0-16")

    def fn2(doc):
        doc["attempts"][0]["jobs"][1]["run_dir"] = str(new_run_dir)
    tasks_mod._update(layout, "T0-16", fn2)
    r2 = post(auth_client, "/tasks/T0-16/attempts/T0-16.a1.v1/open-run-dir")
    assert r2.status_code == 303 and "flash=applied" in r2.headers["location"]
    assert calls[-1] == str(new_run_dir)
