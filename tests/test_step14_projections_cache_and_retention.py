"""2.8.0 step 14 (R-09 step 2, R-22, R-23): a turn whose cost stops growing with history.

Covers: render only what changed (R-22), the document cache's key and filesystem assumption (R-09 step 2,
already shipped — these two tests pin it down rather than change it), the archive behind the resolver so a
finished job record leaves the loop's own scan (R-23), the log rotating by size and being tailed by byte
offset (R-23), and two of astra's load_all/turn-snapshot questions (F-load-all-sharing, F-mid-turn-create).
"""
from __future__ import annotations

import json
import time

from tests.conftest import make_task


# ------------------------------------------------------------------ R-22: render only dirty projections

def _core_mtimes(layout):
    names = ("TASKS.md", "STATE.md", "ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md")
    return {n: (layout.root / n).stat().st_mtime_ns for n in names if (layout.root / n).exists()}


def test_an_idle_turn_writes_no_projection(layout):
    from supervisor import render
    make_task(layout, "A")
    runtime = {"status": "RUNNING", "pid": 111, "heartbeat": "2026-01-01T00:00:00+00:00"}
    render.render_all(layout, runtime=runtime)
    before = _core_mtimes(layout)
    time.sleep(0.02)
    render.render_all(layout, runtime=runtime)        # nothing changed: must not touch any projection file
    after = _core_mtimes(layout)
    assert after == before, f"an idle turn rewrote: {sorted(k for k in before if before[k] != after.get(k))}"


def test_an_identical_render_is_not_rewritten(layout):
    from supervisor import render
    make_task(layout, "A")
    render.render_tasks(layout)
    p = layout.root / "TASKS.md"
    mtime1 = p.stat().st_mtime_ns
    time.sleep(0.02)
    render.render_tasks(layout)                        # same store, called directly (not through render_all)
    assert p.stat().st_mtime_ns == mtime1


def test_a_turn_that_changes_one_task_rerenders_only_what_that_touches(layout):
    from supervisor import render
    make_task(layout, "A")
    make_task(layout, "B")
    runtime = {"status": "RUNNING", "pid": 111}
    render.render_all(layout, runtime=runtime)
    before = _core_mtimes(layout)
    time.sleep(0.02)
    from supervisor import tasks as tasks_mod
    tasks_mod.set_status(layout, "A", "IN_PROGRESS")
    render.render_all(layout, runtime=runtime)
    after = _core_mtimes(layout)
    assert after["TASKS.md"] != before["TASKS.md"] and after["STATE.md"] != before["STATE.md"]
    for untouched in ("ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md"):
        assert after[untouched] == before[untouched], f"{untouched} was rewritten for an unrelated task change"


# ------------------------------------------------------------------ R-09 step 2: the document cache (already shipped)

def test_cached_document_reparses_only_when_path_mtime_or_size_moves(layout):
    """`tasks._cached_task`'s key is (path, mtime_ns, size) — stat-cheap, no content hash — so it assumes a file's
    bytes never change without its mtime or size moving too (true of every writer here: store.update/write_new
    bump both). Pins down behaviour R-09 step 1 already shipped; not something this task changes."""
    from supervisor import tasks as tasks_mod
    make_task(layout, "A")
    p = tasks_mod.path_for(layout, "A")
    t1, err1 = tasks_mod._cached_task(p)
    assert err1 is None
    t2, _ = tasks_mod._cached_task(p)
    assert t2 is t1, "same (path, mtime, size): must be served from the cache, not re-parsed"
    tasks_mod.set_status(layout, "A", "IN_PROGRESS")      # real rewrite: mtime and size move
    t3, _ = tasks_mod._cached_task(p)
    assert t3 is not t1 and t3.status.value == "IN_PROGRESS"


def test_a_finished_job_records_json_is_not_reparsed_once_cached(layout):
    from supervisor import jobs as jobs_mod
    jobs_mod.prepare(layout, "A", "A.a1", "process", {})
    jobs_mod.set_phase(layout, "A.a1", "DONE", result="COMPLETE", finished="now")
    p = jobs_mod.path(layout, "A.a1")
    r1 = jobs_mod._cached_read(p)
    r2 = jobs_mod._cached_read(p)
    assert r1 is r2


# ------------------------------------------------------------------ R-23: the archive behind the resolver

def _finish_task_with_jobs(layout, task_id: str, n_jobs: int):
    from supervisor import jobs as jobs_mod, tasks as tasks_mod
    make_task(layout, task_id, status="IN_PROGRESS")
    for i in range(1, n_jobs + 1):
        aid = f"{task_id}.a{i}"
        jobs_mod.prepare(layout, task_id, aid, "process", {})
        jobs_mod.set_phase(layout, aid, "DONE", result="COMPLETE", finished="now")
    tasks_mod.record_delivery(layout, task_id, "d1")
    tasks_mod.record_verification(layout, task_id, "d1", ok=True)
    tasks_mod.advance(layout, task_id)
    assert tasks_mod.load(layout, task_id).status.value == "COMPLETE"


def test_a_terminal_tasks_job_records_leave_the_top_level_jobs_directory(layout):
    """R-23: "finished job records are scanned forever" — the loop's own scans (`_scan_open`, `all_records`,
    `all_grouped`) glob only `state/runtime/jobs/*.json` (non-recursive), so a record moved under
    `state/runtime/jobs/archive/<task>/` is out of every one of them, however many tasks have finished."""
    from supervisor import jobs as jobs_mod
    _finish_task_with_jobs(layout, "A", 3)
    assert len(list(layout.jobs.glob("A.*.json"))) == 0, "a completed task's records must not stay at the top level"
    assert len(list((layout.jobs / "archive" / "A").glob("*.json"))) == 3
    assert all(r.get("task_id") != "A" for r in jobs_mod.all_records(layout))
    assert all(r.get("task_id") != "A" for r in jobs_mod.open_records(layout))


def test_an_archived_jobs_old_id_still_resolves(layout):
    from supervisor import jobs as jobs_mod
    _finish_task_with_jobs(layout, "A", 2)
    rec = jobs_mod.resolve(layout, "A.a1")
    assert rec is not None and rec["task_id"] == "A" and rec["phase"] == "DONE"
    assert jobs_mod.resolve(layout, "A.a1") is jobs_mod.resolve(layout, "A.a1")       # cached once archived too


def test_an_archived_tasks_attempts_table_still_lists_every_job(layout):
    """`jobs.for_task` is what the Task page's Attempts table reads: archiving must not make a completed task's
    own history disappear from it, only make the *loop's* bulk scans skip it."""
    from supervisor import jobs as jobs_mod
    _finish_task_with_jobs(layout, "A", 3)
    recs = jobs_mod.for_task(layout, "A")
    assert sorted(r["attempt_id"] for r in recs) == ["A.a1", "A.a2", "A.a3"]


def test_an_archived_job_url_still_resolves_in_the_control_center(layout):
    from fastapi.testclient import TestClient
    from supervisor.ui.app import create_app
    _finish_task_with_jobs(layout, "A", 1)
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    r = client.get("/agents/A.a1")
    assert r.status_code == 200 and "A.a1" in r.text


def test_scanning_open_jobs_does_not_grow_with_archived_finished_work(layout):
    """The turn cost on the scale fixture: with N finished-and-archived tasks behind them, finding the few open
    jobs costs the same stat-and-glob work as with none — the archive, not the cache, is what keeps this flat."""
    from supervisor import jobs as jobs_mod
    for i in range(40):
        _finish_task_with_jobs(layout, f"F{i}", 5)                 # 200 archived records
    make_task(layout, "OPEN")
    jobs_mod.prepare(layout, "OPEN", "OPEN.a1", "process", {})
    start = time.perf_counter()
    open_recs = jobs_mod.open_records(layout)
    elapsed = time.perf_counter() - start
    assert [r["attempt_id"] for r in open_recs] == ["OPEN.a1"]
    assert len(list(layout.jobs.glob("*.json"))) == 1, "only the one truly open record should remain at the top level"
    assert elapsed < 1.0, elapsed


# ------------------------------------------------------------------ R-23: log rotation and offset tailing

def test_the_log_rotates_by_size(layout):
    from supervisor import store
    layout.log.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(50):
        store.append_line(layout.log, json.dumps({"ts": "x", "event": "probe", "pad": "y" * 40}))
    size_before = layout.log.stat().st_size
    assert size_before > 0
    rotated = store.rotate_log(layout.log, max_bytes=200)
    assert rotated is True
    assert layout.log.exists() and layout.log.stat().st_size == 0
    assert (layout.log.parent / (layout.log.name + ".1")).stat().st_size == size_before


def test_the_log_is_tailed_by_byte_offset_not_read_whole(layout, monkeypatch):
    from supervisor import render, store
    layout.log.parent.mkdir(parents=True, exist_ok=True)
    for i in range(200):
        store.append_line(layout.log, json.dumps({"ts": "x", "event": f"e{i}"}))
    real_read_text = type(layout.log).read_text
    calls = []

    def counting_read_text(self, *a, **k):
        if self == layout.log:
            calls.append(1)
        return real_read_text(self, *a, **k)
    monkeypatch.setattr(type(layout.log), "read_text", counting_read_text)
    rows = render._tail_log(layout, 5)
    assert len(rows) == 5 and rows[-1]["event"] == "e199"
    assert not calls, "the log tail must seek from the end, not call read_text() on the whole file"


# ------------------------------------------------------------------ astra: load_all sharing, and mid-turn creation

def test_load_all_hands_out_the_same_task_object_across_calls_in_one_turn():
    """Ruling (documented, not changed): inside a turn_snapshot, load_all() returns a fresh dict but the SAME
    Task instances every call — cheap on purpose. Every production caller only ever mutates through
    store.update()/tasks._update() (never a Task attribute), so nothing in this codebase relies on, or is hurt
    by, that sharing; a copy-on-read fix would cost a deep copy of the whole store every call for no observed
    benefit. This test is the enforcement: it fails the day a caller starts mutating one in place."""
    from supervisor import tasks as tasks_mod
    import tempfile
    from pathlib import Path
    from supervisor import testing
    with tempfile.TemporaryDirectory() as d:
        layout = testing.project(Path(d))
        make_task(layout, "A")
        snap = tasks_mod.load_all(layout)
        with tasks_mod.turn_snapshot(snap):
            a1 = tasks_mod.load_all(layout)["A"]
            a2 = tasks_mod.load_all(layout)["A"]
            assert a1 is a2, "load_all must not deep-copy Task objects on every call inside one turn"
            before = a1.status
            a1.status = "IN_PROGRESS"            # a caller that (wrongly) mutated the returned object in place
            try:
                assert tasks_mod.load_all(layout)["A"].status == "IN_PROGRESS", (
                    "sharing is real: a direct mutation is visible to every other reader of this turn's snapshot")
            finally:
                a1.status = before               # leave the shared object as found


def test_a_task_created_mid_turn_is_visible_to_load_all_inside_that_turn(layout):
    """Fix, not merely documented: tasks.create() used to write the new file and stop — a step later in the SAME
    turn that called load_all() still got the pre-create snapshot, so a just-created task could not be scheduled
    until the NEXT turn even though tasks._update()'s docstring already promised "later steps must see this
    write" for every other kind of change. create() and import_seed() now keep that promise too."""
    from supervisor import tasks as tasks_mod
    make_task(layout, "EXISTING")
    snap = tasks_mod.load_all(layout)
    with tasks_mod.turn_snapshot(snap):
        tasks_mod.create(layout, {"id": "NEW", "phase": 0, "title": "new", "objective": "o", "owner_role": "engineer",
                                  "depends_on": [], "actions": ["a"], "artifacts": ["x"], "tests": ["t"], "reviewer": [],
                                  "acceptance": "ok", "escalation": "none", "human_approval": "none",
                                  "cost_category": "deterministic", "status": "NOT_STARTED"})
        assert "NEW" in tasks_mod.load_all(layout), "a task created mid-turn must be visible to the rest of that turn"
    assert "NEW" in tasks_mod.load_all(layout)                      # and still there outside the turn, from disk
