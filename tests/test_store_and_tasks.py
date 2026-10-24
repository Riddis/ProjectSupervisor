import threading
import pytest

from supervisor import gates, render, store, tasks
from supervisor.locks import FileLock, LockHeld
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def test_cas_concurrent_updates_lose_nothing(layout):
    p = layout.state / "counter.yaml"
    store.write_new(p, {"n": 0})

    def worker():
        for _ in range(50):
            while True:
                doc = store.read(p)
                try:
                    store.update(p, lambda d: d.__setitem__("n", d["n"] + 1), expected_rev=doc["rev"])
                    break
                except store.StaleRevision:
                    continue
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert store.read(p)["n"] == 200
    assert store.read(p)["rev"] == 201


def test_stale_rev_rejected(layout):
    p = layout.state / "doc.yaml"
    store.write_new(p, {"a": 1})
    store.update(p, lambda d: d.__setitem__("a", 2), expected_rev=1)
    with pytest.raises(store.StaleRevision):
        store.update(p, lambda d: d.__setitem__("a", 3), expected_rev=1)


def test_file_lock_exclusive(layout):
    with FileLock(layout.lock, owner="a"):
        with pytest.raises(LockHeld):
            FileLock(layout.lock, owner="b").acquire()


def test_eligibility_respects_deps_and_gates(layout):
    make_task(layout, "A"); make_task(layout, "B", deps=["A"]); make_task(layout, "P1", phase=1)
    assert [t.id for t in tasks.eligible(layout)] == ["A"]
    tasks.set_status(layout, "A", "COMPLETE", actor="owner", note="bootstrap", force=True)
    assert [t.id for t in tasks.eligible(layout)] == ["B"]
    gates.set_status(layout, "G0", "PASSED", ["evidence"])
    assert {t.id for t in tasks.eligible(layout)} == {"B", "P1"}


def test_illegal_transition_and_complete_guard(layout):
    make_task(layout, "A")
    with pytest.raises(tasks.TaskError):
        tasks.set_status(layout, "A", "WAITING_FOR_REVIEW")
    tasks.set_status(layout, "A", "IN_PROGRESS")
    with pytest.raises(tasks.TaskError):          # COMPLETE never via set_status
        tasks.set_status(layout, "A", "COMPLETE")
    with pytest.raises(tasks.TaskError):          # force needs owner + note
        tasks.set_status(layout, "A", "COMPLETE", actor="cli", force=True)


def test_advance_requires_owner_approval_even_without_reviewer(layout):
    make_task(layout, "A", human="required", status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    tasks.record_verification(layout, "A", "d1", ok=True)
    t = tasks.advance(layout, "A")
    assert t.status == TaskStatus.WAITING_FOR_HUMAN
    tasks.record_approval(layout, "A", "d1", "ACCEPT")
    assert tasks.advance(layout, "A").status == TaskStatus.COMPLETE


def test_approval_for_old_digest_does_not_count(layout):
    make_task(layout, "A", human="required", status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    tasks.record_verification(layout, "A", "d1", ok=True)
    tasks.record_approval(layout, "A", "d1", "ACCEPT")
    tasks.record_delivery(layout, "A", "d2")                       # re-delivery supersedes
    tasks.record_verification(layout, "A", "d2", ok=True)
    assert tasks.advance(layout, "A").status == TaskStatus.WAITING_FOR_HUMAN
    with pytest.raises(tasks.TaskError):                          # cannot approve a stale digest
        tasks.record_approval(layout, "A", "d1", "ACCEPT")


def test_review_flow(layout):
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    tasks.record_verification(layout, "A", "d1", ok=True)
    assert tasks.advance(layout, "A").status == TaskStatus.WAITING_FOR_REVIEW
    tasks.record_review(layout, "A", "d1", "code_review", "REVISE")
    assert tasks.advance(layout, "A").status == TaskStatus.IN_PROGRESS
    tasks.record_delivery(layout, "A", "d2")
    tasks.record_verification(layout, "A", "d2", ok=True)
    tasks.record_review(layout, "A", "d2", "code_review", "ACCEPT")
    assert tasks.advance(layout, "A").status == TaskStatus.COMPLETE


def test_failed_verification(layout):
    make_task(layout, "A", status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    tasks.record_verification(layout, "A", "d1", ok=False, detail="tests failed")
    assert tasks.advance(layout, "A").status == TaskStatus.FAILED


def test_graph_validation_detects_cycle_and_bad_phase(layout):
    make_task(layout, "A", deps=["B"]); make_task(layout, "B", deps=["A"]); make_task(layout, "C", phase=0, deps=["P1"]); make_task(layout, "P1", phase=1)
    errs = tasks.validate_graph(tasks.load_all(layout))
    assert any("cycle" in e for e in errs) and any("later-phase" in e for e in errs)


def test_render_is_deterministic_and_reads_live_store(layout):
    make_task(layout, "A"); make_task(layout, "B", deps=["A"])
    tasks.set_status(layout, "A", "COMPLETE", actor="owner", note="bootstrap", force=True)
    render.render_all(layout)
    text = (layout.root / "TASKS.md").read_text(encoding="utf-8")
    assert "| A | A | engineer | — | — | none | claude | COMPLETE (forced) |" in text
    assert "⚑ NOT_STARTED |" in text.split("| B |")[1]
    first = (layout.root / "STATE.md").read_text(encoding="utf-8").splitlines()[2:]
    render.render_all(layout)
    assert (layout.root / "STATE.md").read_text(encoding="utf-8").splitlines()[2:] == first


def test_blocked_or_invalid_verdicts_never_complete(layout):
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    tasks.record_verification(layout, "A", "d1", ok=True)
    tasks.record_review(layout, "A", "d1", "code_review", "BLOCKED")
    assert tasks.advance(layout, "A").status == TaskStatus.BLOCKED
    with pytest.raises(ValueError):
        tasks.record_review(layout, "A", "d1", "code_review", "accept")
    assert tasks.completion_check(tasks.load(layout, "A")) is not None


def test_multiple_reviewers_all_required(layout):
    make_task(layout, "A", reviewer=["code_review", "chief_architect"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1"); tasks.record_verification(layout, "A", "d1", ok=True)
    tasks.record_review(layout, "A", "d1", "code_review", "ACCEPT")
    assert tasks.advance(layout, "A").status == TaskStatus.WAITING_FOR_REVIEW
    tasks.record_review(layout, "A", "d1", "chief_architect", "ACCEPT")
    assert tasks.advance(layout, "A").status == TaskStatus.COMPLETE


def test_wrong_digest_rejected_for_verification_and_review(layout):
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    with pytest.raises(tasks.TaskError):
        tasks.record_verification(layout, "A", "dX", ok=True)
    with pytest.raises(tasks.TaskError):
        tasks.record_review(layout, "A", "dX", "code_review", "ACCEPT")


def test_redelivery_returns_waiting_task_to_in_progress(layout):
    make_task(layout, "A", human="required", status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1"); tasks.record_verification(layout, "A", "d1", ok=True)
    assert tasks.advance(layout, "A").status == TaskStatus.WAITING_FOR_HUMAN
    t = tasks.record_delivery(layout, "A", "d2")
    assert t.status == TaskStatus.IN_PROGRESS and t.delivery.verification is None


def test_forced_status_is_visible_everywhere(layout):
    make_task(layout, "A")
    tasks.set_status(layout, "A", "COMPLETE", actor="owner", note="hand-verified bootstrap", force=True)
    render.render_all(layout)
    assert "COMPLETE (forced)" in (layout.root / "TASKS.md").read_text(encoding="utf-8")
    assert "FORCED" in (layout.root / "STATE.md").read_text(encoding="utf-8")
    assert tasks.load(layout, "A").forced["by"] == "owner"


def test_unlisted_phase_is_closed(layout):
    assert gates.phase_open(layout, 42) is False
    assert gates.phase_open(layout, 0) is True


def test_nested_update_raises_immediately(layout):
    make_task(layout, "A"); make_task(layout, "B")
    import time
    start = time.monotonic()
    with pytest.raises(LockHeld):
        tasks._update(layout, "A", lambda d: tasks.add_note(layout, "B", "x"))
    assert time.monotonic() - start < 2


def test_import_is_idempotent(layout, tmp_path):
    import yaml
    seed = tmp_path / "seed.yaml"
    seed.write_text(yaml.safe_dump({"tasks": [
        {"id": "S1", "phase": 0, "title": "s", "objective": "o", "owner_role": "engineer", "depends_on": [], "actions": [],
         "artifacts": [], "tests": [], "reviewer": [], "acceptance": "", "escalation": "none", "human_approval": "none",
         "cost_category": "claude", "status": "NOT_STARTED", "owned_paths": ["."]}]}))
    r1 = tasks.import_seed(layout, seed)
    tasks.set_status(layout, "S1", "IN_PROGRESS")
    rev = tasks.load(layout, "S1").rev
    r2 = tasks.import_seed(layout, seed)
    assert r1["created"] == ["S1"] and r2["created"] == [] and r2["untouched"] == ["S1"]
    t = tasks.load(layout, "S1")
    assert t.rev == rev and t.status == TaskStatus.IN_PROGRESS


def test_cross_process_lock(layout):
    import subprocess, sys, time
    code = ("import sys,time; sys.path.insert(0, %r); from supervisor.locks import FileLock; "
            "l=FileLock(%r, owner='other').acquire(); print('held', flush=True); time.sleep(3)") % (str(layout.root.parent / "x"), str(layout.lock))
    import pathlib
    repo = str(pathlib.Path(__file__).resolve().parents[1])
    code = code.replace(str(layout.root.parent / "x"), repo)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    try:
        with pytest.raises(LockHeld):
            FileLock(layout.lock, owner="me").acquire()
    finally:
        proc.kill(); proc.wait()
    with FileLock(layout.lock, owner="me"):        # released after the other process died
        pass


def test_render_state_with_two_gates_in_one_phase(layout):
    import yaml
    data = yaml.safe_load(layout.gates.read_text())
    data["gates"]["G0b"] = {"status": "OPEN", "phase": 0, "note": "second"}
    data["gates"]["G0"]["status"] = "PASSED"
    layout.gates.write_text(yaml.safe_dump(data))
    make_task(layout, "A")
    render.render_state(layout)


def test_digest_is_location_independent(tmp_path):
    from supervisor._vendor.contract import digest, manifest
    for d in ("one", "two"):
        (tmp_path / d / "sub").mkdir(parents=True)
        (tmp_path / d / "sub" / "f.txt").write_text("same")
    a = digest(manifest([tmp_path / "one" / "sub"], root=tmp_path / "one"))
    b = digest(manifest([tmp_path / "two" / "sub"], root=tmp_path / "two"))
    assert a == b
    with pytest.raises(ValueError):
        manifest([tmp_path / "two"], root=tmp_path / "one")


def test_blocked_by_review_recovers_on_redelivery(layout):
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1"); tasks.record_verification(layout, "A", "d1", ok=True)
    tasks.record_review(layout, "A", "d1", "code_review", "BLOCKED")
    assert tasks.advance(layout, "A").status == TaskStatus.BLOCKED
    tasks.record_delivery(layout, "A", "d2"); tasks.record_verification(layout, "A", "d2", ok=True)
    tasks.record_review(layout, "A", "d2", "code_review", "ACCEPT")
    assert tasks.advance(layout, "A").status == TaskStatus.COMPLETE


def test_invalid_seed_writes_nothing(layout, tmp_path):
    import yaml
    seed = tmp_path / "seed.yaml"
    base = {"phase": 0, "title": "s", "objective": "o", "owner_role": "engineer", "actions": [], "artifacts": [], "tests": [],
            "reviewer": [], "acceptance": "", "escalation": "none", "human_approval": "none", "cost_category": "claude", "status": "NOT_STARTED"}
    seed.write_text(yaml.safe_dump({"tasks": [dict(base, id="X", depends_on=["Y"]), dict(base, id="Y", depends_on=["X"])]}))
    with pytest.raises(tasks.TaskError):
        tasks.import_seed(layout, seed)
    assert not list(layout.tasks.glob("*.yaml"))


def test_lock_respects_fresh_foreign_sidecar(tmp_path, monkeypatch):
    """A fresh sidecar written by another host counts as held even though the OS lock is free (VM vs Windows)."""
    import json, time
    from supervisor import locks
    lock = tmp_path / ".lock"
    sc = tmp_path / ".lock.owner"
    sc.write_text(json.dumps({"owner": "supervisor pid 1", "pid": 1, "host": "OTHER-HOST", "since": time.time()}))
    with pytest.raises(locks.LockHeld):
        locks.FileLock(lock).acquire()
    assert locks.holder(lock)["host"] == "OTHER-HOST"
    old = time.time() - locks.FOREIGN_FRESH_S - 5
    import os
    os.utime(sc, (old, old))                                  # stale: the other host is gone
    with locks.FileLock(lock):
        assert json.loads(sc.read_text())["host"] == locks.HOST
    assert not sc.exists()


def test_a_view_that_cannot_render_is_skipped_not_fatal(layout):
    """A YAML record caught half-written (an agent editing it) must not fail the loop iteration: the view is
    skipped, its previous file stays, the error is logged once and the recovery once."""
    import json as _json

    from supervisor import render
    good = render.render_all(layout)
    issues_md = layout.root / "ISSUES.md"
    before = issues_md.read_text(encoding="utf-8")
    layout.issues.mkdir(parents=True, exist_ok=True)
    broken = layout.issues / "ISS-900.yaml"
    broken.write_text("id: ISS-900\ntitle: half: written: value\n", encoding="utf-8")
    for _ in range(3):
        paths = render.render_all(layout)                    # no exception
    skipped = {p.name for p in good} - {p.name for p in paths}
    assert skipped == {"ISSUES.md", "STATE.md"}                  # STATE.md counts open issues too
    assert issues_md.read_text(encoding="utf-8") == before   # previous projection kept
    broken.write_text("id: ISS-900\ntitle: fixed\nstatus: open\n", encoding="utf-8")
    assert issues_md in render.render_all(layout)
    events = [_json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines()]
    assert [e["view"] for e in events if e.get("event") == "render_error"] == ["render_issues", "render_state"]
    assert [e["view"] for e in events if e.get("event") == "render_recovered"] == ["render_issues", "render_state"]

