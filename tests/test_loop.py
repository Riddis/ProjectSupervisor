import json, os, subprocess, sys, time
import pytest

from supervisor import commands, diagnosis, inbox, jobs, runtime, store, tasks, testing
from supervisor.adapters import python as pyad
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


@pyad.job("write_artifact")
def _write_artifact(root, run_dir, name="out.txt", content="x", **_):
    (root / name).write_text(content)
    return {"status": "COMPLETE", "artifacts": [name], "notes": "wrote " + name}


@pyad.job("corrupt_own_task")
def _corrupt_own_task(root, run_dir, name="out.txt", **_):
    (root / name).write_text("x")
    (root / "state" / "tasks" / "A.yaml").write_text("id: A\nphase: [unclosed\n", encoding="utf-8")
    return {"status": "COMPLETE", "artifacts": [name], "notes": "corrupted its own task file"}


@pyad.job("diagnose_and_corrupt_own_task")
def _diagnose_and_corrupt_own_task(root, run_dir, **_):
    (root / "state" / "tasks" / "A.yaml").write_text("id: A\nphase: [unclosed\n", encoding="utf-8")
    return {"status": "COMPLETE", "artifacts": [], "notes": "diagnosis result", "action": "owner", "summary": "needs the owner"}


@pyad.job("diagnose_owner")
def _diagnose_owner(root, run_dir, **_):
    return {"status": "COMPLETE", "artifacts": [], "notes": "diagnosis result", "action": "owner", "summary": "needs the owner"}


DIAG_EXTRA = {"diag_signature": "sig-1", "diag_level": "manager", "diag_issue": None, "diag_source": "test",
              "diag_detail": "test detail", "diag_role": "senior_pm", "diag_grant": 0}


def ptask(layout, tid, deps=(), name=None, owned=()):
    make_task(layout, tid, deps=deps)
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "write_artifact", "params": {"name": name or f"{tid}.txt"}}
        doc["artifacts"] = [name or f"{tid}.txt"]
        doc["owned_paths"] = list(owned)
    tasks._update(layout, tid, fn)


def proc_task(layout, tid, seconds=1.0, deps=(), owned=(), artifact=None):
    make_task(layout, tid, deps=deps)
    art = artifact or f"{tid}.txt"
    code = (f"import time, json, pathlib; time.sleep({seconds}); pathlib.Path({art!r}).write_text('ok'); "
            f"pathlib.Path('runs/{tid}').mkdir(parents=True, exist_ok=True)")
    def fn(doc):
        doc["job"] = {"adapter": "process", "argv": [sys.executable, "-c", code + f"; import glob; [pathlib.Path(d, 'result.json').write_text(json.dumps({{'status':'COMPLETE','artifacts':[{art!r}]}})) for d in glob.glob('runs/{tid}/*')]"]}
        doc["artifacts"] = [art]
        doc["owned_paths"] = list(owned)
    tasks._update(layout, tid, fn)


def test_python_graph_completes_in_order(layout):
    ptask(layout, "A"); ptask(layout, "B", deps=["A"]); ptask(layout, "C", deps=["B"])
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(4):
        sup.once()
    for tid in "ABC":
        assert tasks.load(layout, tid).status == TaskStatus.COMPLETE, tid
    hist = [e for e in layout.log.read_text().splitlines() if '"job_done"' in e]
    assert [json.loads(e)["task"] for e in hist] == ["A", "B", "C"]
    assert (layout.root / "A.txt").exists()


def test_crash_before_submit_redispatches_same_attempt(layout):
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    task = tasks.load(layout, "A")
    from supervisor.adapters import REGISTRY
    spec = REGISTRY["python"].prepare(layout, task, "A.a1")
    jobs.prepare(layout, "A", "A.a1", "python", spec)          # crash here: record exists, nothing launched
    Supervisor(layout, sleep_s=0).once()                       # restart
    recs = jobs.for_task(layout, "A")       # R-23: COMPLETE now, so the record may have moved to the archive
    assert [r["attempt_id"] for r in recs] == ["A.a1"]
    assert recs[0]["phase"] == "DONE"
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE


def test_crash_after_submit_adopts_live_child(layout):
    proc_task(layout, "P", seconds=1.5)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")      # submitted; now "crash"
    assert rec["phase"] == "SUBMITTED"
    job_id = rec["attempt_id"]
    sup2 = Supervisor(layout, sleep_s=0)
    sup2.once()
    assert jobs.resolve(layout, job_id)["phase"] == "RUNNING"       # adopted, not re-run
    deadline = time.time() + 10
    while time.time() < deadline and jobs.resolve(layout, job_id)["phase"] != "DONE":
        sup2.once(); time.sleep(0.2)
    assert jobs.resolve(layout, job_id)["phase"] == "DONE"          # R-23: COMPLETE now, maybe archived
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE
    assert len(jobs.for_task(layout, "P")) == 1


def test_crash_after_result_before_done_ingests_without_rerun(layout):
    proc_task(layout, "P", seconds=0.1)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")
    job_id = rec["attempt_id"]
    time.sleep(1.5)                                              # child finished, result.json written, record still SUBMITTED
    assert (layout.root / "P.txt").exists()
    Supervisor(layout, sleep_s=0).once()
    assert jobs.resolve(layout, job_id)["phase"] == "DONE"          # R-23: COMPLETE now, maybe archived
    assert len(jobs.for_task(layout, "P")) == 1
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE


def test_lost_process_marks_failed(layout):
    proc_task(layout, "P", seconds=30)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")
    job_id = rec["attempt_id"]
    from supervisor.adapters import REGISTRY
    REGISTRY["process"].terminate(rec["handle"])
    time.sleep(0.5)
    Supervisor(layout, sleep_s=0).once()
    assert jobs.read(layout, job_id)["phase"] == "DONE"
    assert tasks.load(layout, "P").status == TaskStatus.FAILED
    assert "lost" in tasks.load(layout, "P").delivery.verification.detail


def test_overlapping_owned_paths_are_serialised(layout):
    proc_task(layout, "X", seconds=1.0, owned=["shared/dir"])
    proc_task(layout, "Y", seconds=0.1, owned=["shared/dir/sub"])
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    assert len(jobs.open_records(layout)) == 1                  # only one of the two dispatched
    assert {r["task_id"] for r in jobs.open_records(layout)} == {"X"}


def test_command_applied_exactly_once_and_acknowledged(layout):
    ptask(layout, "A")         # a fake (python) worker: the suite never launches a real model client
    p = commands.submit(layout, "task.note", {"task_id": "A", "note": "hello"}, actor="owner-ui")
    sup = Supervisor(layout, sleep_s=0)
    sup.once(); sup.once()
    assert not p.exists()
    applied = commands.status_of(layout, p.stem)
    assert applied["ok"] is True and applied["actor"] == "owner-ui"
    # only the owner's note counts: the task's own (unrunnable here) job may add escalation notes since every
    # worker failure is routed (1.6.0)
    assert len([n for n in tasks.load(layout, "A").model_extra["notes"] if n.get("note") == "hello"]) == 1


def test_pause_stops_dispatch_and_resume_restarts(layout):
    ptask(layout, "A")
    runtime.set_decision(layout, "PAUSE")
    sup = Supervisor(layout, sleep_s=0)
    assert sup.once() == "PAUSE"
    assert tasks.load(layout, "A").status == TaskStatus.NOT_STARTED
    assert runtime.read(layout)["status"] == "PAUSED"
    commands.submit(layout, "loop.resume", {}, actor="owner")
    sup.once()
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE


def test_dispatch_under_pause_leaves_a_failed_tasks_status_alone_across_several_turns(layout):
    """2.7.0 review, P3-2 (observed, pre-existing): `launch()` called `_ensure_in_progress()` before the one
    gate (`gate.check`), so a FAILED task whose only queued job the gate then refuses (PAUSE: "decision is
    PAUSE") was still flipped to IN_PROGRESS at prepare time even though nothing launched. The status must
    stay FAILED while every launch is refused, across several paused turns, and only move once the decision
    returns to RUN and the gate actually lets the job through."""
    ptask(layout, "A")
    tasks.set_status(layout, "A", "FAILED", actor="owner", note="forced for the test", force=True)
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python")
    assert rec["phase"] == "PREPARED" and rec.get("held") == "decision is PAUSE"
    assert tasks.load(layout, "A").status == TaskStatus.FAILED
    for _ in range(3):
        sup.once()
        assert tasks.load(layout, "A").status == TaskStatus.FAILED
    runtime.set_decision(layout, "RUN", "test")
    for _ in range(5):
        sup.once()
        if tasks.load(layout, "A").status == TaskStatus.COMPLETE:
            break
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE


def test_unreadable_task_file_during_launch_logs_held_once_not_per_turn(layout):
    """Chief Architect review of the fix above: moving `_ensure_in_progress()` below the held-reset
    (`if rec.get("held"): rec = jobs_mod.set_phase(..., held=None)`) rebinds `rec` to a record whose `held` is
    always `None` by the time the `TaskError` branch compares it to the new reason, so that comparison is always
    true and `launch_held` is logged once per turn for as long as the file stays unreadable - the same "logged
    once per reason, not per iteration" invariant `test_pause_holds_prepared_records_and_a_resume_launches_them`
    pins for the gate branch above it."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python")
    assert rec["phase"] == "PREPARED" and rec.get("held") == "decision is PAUSE"
    (layout.tasks / "A.yaml").write_text("id: A\nphase: [unclosed\n", encoding="utf-8")
    runtime.set_decision(layout, "RUN", "test")           # the gate now lets the record through the held-reset
    for _ in range(3):
        sup.once()
    held = [json.loads(e) for e in layout.log.read_text().splitlines()
            if '"launch_held"' in e and "task file unreadable" in e]
    assert len(held) == 1                                      # logged once per reason, not per iteration
    rec = jobs.read(layout, rec["attempt_id"])
    assert rec["phase"] == "PREPARED" and (rec.get("held") or "").startswith("task file unreadable")


def test_stop_decision_exits_run(layout):
    runtime.set_decision(layout, "STOP")
    assert Supervisor(layout, sleep_s=0).run() == 0
    assert runtime.read(layout)["status"] == "STOPPED"


def test_run_refuses_when_lock_held(layout):
    from supervisor.locks import FileLock
    with FileLock(layout.lock, owner="other"):
        assert Supervisor(layout, sleep_s=0).run(max_iterations=1) == 2


def test_direct_command_when_no_loop(layout):
    make_task(layout, "A")
    r = commands.submit_or_apply(layout, "task.set_priority", {"task_id": "A", "priority": 5}, actor="owner")
    assert r["ok"] is True and tasks.load(layout, "A").priority == 5


def test_crash_between_submit_and_submitted_record_adopts_not_duplicates(layout, monkeypatch):
    proc_task(layout, "P", seconds=1.5)
    sup = Supervisor(layout, sleep_s=0)
    task = tasks.load(layout, "P")
    from supervisor.adapters import REGISTRY
    spec = REGISTRY["process"].prepare(layout, task, "P.a1")
    jobs.prepare(layout, "P", "P.a1", "process", spec)
    handle = REGISTRY["process"].submit(spec)                    # launched; "crash" before set_phase(SUBMITTED)
    assert jobs.read(layout, "P.a1")["phase"] == "PREPARED"
    sup2 = Supervisor(layout, sleep_s=0)
    sup2.once()
    rec = jobs.resolve(layout, "P.a1")
    assert rec.get("recovered") is True and rec["handle"]["pid"] == handle["pid"]
    import psutil
    assert len([p for p in psutil.Process().children(recursive=True) if p.pid == handle["pid"]]) <= 1
    deadline = time.time() + 10
    while time.time() < deadline and jobs.resolve(layout, "P.a1")["phase"] != "DONE":
        sup2.once(); time.sleep(0.2)
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE
    assert len(jobs.for_task(layout, "P")) == 1    # R-23: COMPLETE now, maybe archived; no duplicate either way


def test_command_not_reapplied_after_crash_between_handler_and_ack(layout, monkeypatch):
    ptask(layout, "A")         # a fake (python) worker: the suite never launches a real model client
    p = commands.submit(layout, "task.note", {"task_id": "A", "note": "once"}, actor="owner")
    cid = p.stem
    tasks.add_note(layout, "A", "once", "owner", command_id=cid)      # handler ran, then crash before ack
    Supervisor(layout, sleep_s=0).once()
    notes = tasks.load(layout, "A").model_extra["notes"]
    assert len(notes) == 1 and not p.exists()


def test_artifact_globs_are_expanded(layout):
    make_task(layout, "G")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "write_artifact", "params": {"name": "out/g1.txt"}}
        doc["artifacts"] = ["out/*.txt"]
    (layout.root / "out").mkdir()
    tasks._update(layout, "G", fn)
    Supervisor(layout, sleep_s=0).once()
    t = tasks.load(layout, "G")
    assert t.status == TaskStatus.COMPLETE and t.paths == ["out/g1.txt"]


def test_cli_mutation_queues_while_loop_holds_lock(layout):
    """Single-writer rule: with a supervisor holding the lock the CLI never writes state/ itself — the change is
    queued as a command file and applied by the loop (exactly once)."""
    from typer.testing import CliRunner
    from supervisor.cli import app
    from supervisor.locks import FileLock
    from supervisor import commands
    make_task(layout, "A")
    with FileLock(layout.lock, owner="supervisor"):
        r = CliRunner().invoke(app, ["task", "note", "A", "hi"])
        assert r.exit_code == 0 and "queued" in r.output
        assert not (tasks.load(layout, "A").model_extra or {}).get("notes")
        r2 = CliRunner().invoke(app, ["task", "migrate-ownership"])         # direct-mutation commands still refuse
        assert r2.exit_code != 0 and "supervisor holds" in (r2.output + str(r2.exception))
        r3 = CliRunner().invoke(app, ["task", "import"])                    # import goes through the queue instead
        assert r3.exit_code == 0 and "queued" in r3.output
    with FileLock(layout.lock, owner="supervisor"):
        commands.apply_all(layout)
    assert (tasks.load(layout, "A").model_extra or {})["notes"][0]["note"] == "hi"


def test_recover_adopts_child_killed_before_running_write(layout):
    """A live child whose RUNNING write was lost is adopted by pid. The child holds until the test releases it: a
    fixed sleep raced the process scan on a loaded Windows host, and a child that finishes during the scan is
    (correctly) adopted as finished instead — that case has its own deterministic test below."""
    release = layout.root / "release.flag"
    proc_task(layout, "P", seconds=0)
    hold = (f"import time, pathlib; d = time.time() + 120\n"
            f"while not pathlib.Path({str(release)!r}).exists() and time.time() < d: time.sleep(0.05)\n")
    def fn(doc):
        doc["job"]["argv"][2] = hold + doc["job"]["argv"][2]
    tasks._update(layout, "P", fn)
    from supervisor.adapters import REGISTRY
    from supervisor._vendor.contract import atomic_json
    task = tasks.load(layout, "P")
    spec = REGISTRY["process"].prepare(layout, task, "P.a1")
    jobs.prepare(layout, "P", "P.a1", "process", spec)
    handle = REGISTRY["process"].submit(spec)
    try:
        atomic_json(layout.runs / "P" / "P.a1" / "status.json", {"status": "LAUNCHING", "attempt_id": "P.a1"})   # simulate kill before RUNNING write
        rec = REGISTRY["process"].recover(spec)
        assert rec is not None and rec["pid"] == handle["pid"]
    finally:
        release.write_text("go")
        REGISTRY["process"].terminate(handle)


def test_recover_does_not_relaunch_a_child_that_finished_during_the_scan(layout, monkeypatch):
    """LAUNCHING status, no result yet when recover() starts, and the child finishes while the process scan runs:
    the scan finds nothing, but the result is there — recover must adopt it, not return None (which relaunches
    the attempt and executes it twice)."""
    import json
    from supervisor.adapters import REGISTRY, process as process_mod
    from supervisor._vendor.contract import atomic_json
    proc_task(layout, "P2", seconds=0.1)
    task = tasks.load(layout, "P2")
    spec = REGISTRY["process"].prepare(layout, task, "P2.a1")
    jobs.prepare(layout, "P2", "P2.a1", "process", spec)
    run_dir = layout.runs / "P2" / "P2.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(run_dir / "status.json", {"status": "LAUNCHING", "attempt_id": "P2.a1"})
    launches = []

    def scan_that_takes_a_while(attempt):
        launches.append(attempt)
        (run_dir / "result.json").write_text(json.dumps({"status": "COMPLETE", "artifacts": ["P2.txt"]}))   # child finished meanwhile
        return None
    monkeypatch.setattr(process_mod, "_topmost_tagged", scan_that_takes_a_while)
    handle = REGISTRY["process"].recover(spec)
    assert launches == ["P2.a1"]
    assert handle is not None and handle["pid"] is None and handle["deadline"] == float("inf")   # adopted as finished


def test_one_unreadable_task_file_does_not_stop_the_loop(layout):
    """R-02: a task file that does not parse must not raise on every turn. A healthy task next to it keeps
    running, the task pages render instead of 500ing, the broken file shows as such, and exactly one owner item
    names it — it is not re-opened on every later load."""
    ptask(layout, "A"); ptask(layout, "B", deps=["A"])
    (layout.tasks / "BROKEN.yaml").write_text("id: BROKEN\nphase: [unclosed\n", encoding="utf-8")
    sup = Supervisor(layout, sleep_s=0)
    code = sup.run(max_iterations=10)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE
    assert tasks.load(layout, "B").status == TaskStatus.COMPLETE
    items = [i for i in inbox.open_items(layout, "owner") if i.get("dedup_key") == "unreadable_task:BROKEN.yaml"]
    assert len(items) == 1
    tasks.load_all(layout)                                        # a second load must not open a second item
    assert len(inbox.open_items(layout, "owner")) == 1
    c = testing.ui_client(layout)
    for url in ("/overview", "/tasks", "/roadmap"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert "BROKEN" in r.text and "unreadable" in r.text, url


def test_a_task_depending_on_an_unreadable_one_is_not_dispatched(layout):
    ptask(layout, "C", deps=["BROKEN"])
    (layout.tasks / "BROKEN.yaml").write_text("id: BROKEN\nphase: [unclosed\n", encoding="utf-8")
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        sup.once()
    c = tasks.load(layout, "C")
    assert c.status in (TaskStatus.NOT_STARTED, TaskStatus.READY)
    assert tasks.unmet_dependency(layout, c) == "BROKEN (unreadable)"


def test_unreadable_task_file_with_a_prepared_job_record_holds_rather_than_blocks(layout):
    """F1: `_ensure_in_progress`'s `TaskError` must not reach `launch()`'s caller. A task already holding a
    PREPARED record whose file then turns unreadable must have that record held, not raise on every later turn -
    one deduped owner item names the file, and a healthy task beside it still dispatches and completes."""
    ptask(layout, "A"); ptask(layout, "B")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python")
    (layout.tasks / "A.yaml").write_text("id: A\nphase: [unclosed\n", encoding="utf-8")
    runtime.set_decision(layout, "RUN", "test")
    code = sup.run(max_iterations=8)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert sup.errors == []
    held = jobs.read(layout, rec["attempt_id"])
    assert held["phase"] == "PREPARED"
    assert held.get("held")
    items = [i for i in inbox.open_items(layout, "owner") if i.get("dedup_key") == "unreadable_task:A.yaml"]
    assert len(items) == 1
    assert tasks.load(layout, "B").status == TaskStatus.COMPLETE
    assert (layout.root / "B.txt").exists()


def test_result_for_a_task_whose_file_turned_unreadable_while_its_job_ran_closes_the_record(layout):
    """F1: the same `TaskError` reaches `ingest()` through its own call to `_ensure_in_progress`. A result
    arriving for a task whose file became unreadable while the job ran must close that record with the result,
    not raise - the turn goes on and a healthy task beside it still completes."""
    make_task(layout, "A")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "corrupt_own_task", "params": {"name": "A.txt"}}
        doc["artifacts"] = ["A.txt"]
    tasks._update(layout, "A", fn)
    ptask(layout, "B")
    sup = Supervisor(layout, sleep_s=0)
    code = sup.run(max_iterations=8)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert sup.errors == []
    recs = jobs.for_task(layout, "A")
    assert recs and recs[-1]["phase"] == "DONE"
    assert recs[-1].get("result") == "COMPLETE"
    assert tasks.load(layout, "B").status == TaskStatus.COMPLETE


def test_diagnosis_result_for_a_task_whose_file_turned_unreadable_closes_the_record(layout):
    """F7 (round 2 of 2.6.1's review): `launch()` and `ingest()` both skip `_ensure_in_progress()` for a
    diagnosis-kind record, so F1's two catches never run for it; `diagnosis.ingest()` then loaded the task
    itself with no guard. A diagnosis job that corrupts its own task file before returning its result must
    still close the record with that result, not raise past once() - the turn goes on and a healthy task
    beside it still completes."""
    make_task(layout, "A")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "diagnose_and_corrupt_own_task", "params": {}}
        doc["artifacts"] = []
    tasks._update(layout, "A", fn)
    tasks.set_status(layout, "A", "BLOCKED", actor="test", note="blocked, awaiting diagnosis")
    ptask(layout, "B")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python", note="diagnosis:senior_pm", kind=diagnosis.KIND,
                       extra=dict(DIAG_EXTRA))
    runtime.set_decision(layout, "RUN", "test")
    code = sup.run(max_iterations=8)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert sup.errors == []
    j = jobs.read(layout, rec["attempt_id"])
    assert j["phase"] == "DONE"
    assert j.get("result") == "COMPLETE"
    done = [json.loads(e) for e in layout.log.read_text().splitlines()
           if '"job_done"' in e and rec["attempt_id"] in e]
    assert done and done[-1].get("task_unreadable") is True
    assert tasks.load(layout, "B").status == TaskStatus.COMPLETE
    assert (layout.root / "B.txt").exists()


def test_prepared_diagnosis_record_then_the_task_file_breaks_before_launch_does_not_block(layout):
    """F7: a diagnosis record dispatched under PAUSE (PREPARED), the task file then made unreadable, the loop
    resumed. `launch()`'s own gate already tolerates an unreadable task (the record still launches), so the
    result reaches `ingest()` the same way as the sibling case above; the turn must not raise, the record
    must not be left SUBMITTED to be re-ingested forever, and a healthy task beside it still completes."""
    make_task(layout, "A")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "diagnose_owner", "params": {}}
        doc["artifacts"] = []
    tasks._update(layout, "A", fn)
    tasks.set_status(layout, "A", "BLOCKED", actor="test", note="blocked, awaiting diagnosis")
    ptask(layout, "B")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python", note="diagnosis:senior_pm", kind=diagnosis.KIND,
                       extra=dict(DIAG_EXTRA))
    (layout.tasks / "A.yaml").write_text("id: A\nphase: [unclosed\n", encoding="utf-8")
    runtime.set_decision(layout, "RUN", "test")
    code = sup.run(max_iterations=8)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert sup.errors == []
    j = jobs.read(layout, rec["attempt_id"])
    assert j["phase"] != "SUBMITTED"
    assert tasks.load(layout, "B").status == TaskStatus.COMPLETE
    assert (layout.root / "B.txt").exists()


def test_malformed_command_file_is_quarantined_and_later_commands_still_apply(layout):
    """R-02: a command file whose JSON parses but is not an object must not stop every command queued behind it
    in the same turn, `loop.pause` included."""
    ptask(layout, "A")
    layout.commands.mkdir(parents=True, exist_ok=True)
    (layout.commands / "0000000000001-bad.json").write_text("[]", encoding="utf-8")
    commands.submit(layout, "loop.pause", {}, actor="owner")
    sup = Supervisor(layout, sleep_s=0)
    code = sup.run(max_iterations=10)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert runtime.read(layout).get("decision") == "PAUSE"
    assert list(commands.pending(layout)) == []
    quarantined = list((layout.commands / "quarantine").glob("*.json"))
    assert [p.name for p in quarantined] == ["0000000000001-bad.json"]
    assert quarantined[0].read_text(encoding="utf-8") == "[]"
    outcome = json.loads((layout.commands_applied / "0000000000001-bad.json").read_text(encoding="utf-8"))
    assert outcome["ok"] is False


@pytest.mark.parametrize("name, raw", [
    ("string_payload", '{"kind":"loop.pause","payload":"x"}'),
    ("list_payload", '{"payload":["a","b"]}'),
    ("list_kind", '{"kind":["loop.pause"]}'),
])
def test_command_envelope_with_wrong_typed_kind_or_payload_is_quarantined_and_later_commands_still_apply(layout, name, raw):
    """F2 (round-1 review of 2.6.1, finding F2): a command file that *is* a JSON object but whose `kind` or
    `payload` has the wrong type raises past the per-file guard R-02 added (`isinstance(cmd, dict)` only) -
    `payload="x"` and `payload=["a","b"]` raise `ValueError` in `dict(payload, ...)`, `kind=["loop.pause"]` raises
    `TypeError` in `HANDLERS.get([...])` - so the file is never quarantined and a `loop.pause` queued behind it
    never applies."""
    ptask(layout, "A")
    layout.commands.mkdir(parents=True, exist_ok=True)
    (layout.commands / "0000000000001-bad.json").write_text(raw, encoding="utf-8")
    commands.submit(layout, "loop.pause", {}, actor="owner")
    sup = Supervisor(layout, sleep_s=0)
    code = sup.run(max_iterations=10)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert runtime.read(layout).get("decision") == "PAUSE"
    assert sup.errors == []
    assert list(commands.pending(layout)) == []
    quarantined = list((layout.commands / "quarantine").glob("*.json"))
    assert [p.name for p in quarantined] == ["0000000000001-bad.json"]
    assert quarantined[0].read_text(encoding="utf-8") == raw
    applied_outcomes = list(layout.commands_applied.glob("0000000000001-bad*.json"))
    assert len(applied_outcomes) == 1
    outcome = json.loads(applied_outcomes[0].read_text(encoding="utf-8"))
    assert outcome["ok"] is False


def test_directory_named_like_a_command_file_is_reported_once_and_the_loop_runs_on(layout):
    """F8 (round-3 review of 2.6.1, probe i5): a directory named like a command file must not stop every
    command queued behind it. `apply_all()` called `Path.unlink()` on the lookalike after the per-file guard
    already caught the read failure, which raises again (PermissionError on Windows, IsADirectoryError on
    POSIX) uncaught, so the turn fails every time and the loop reaches BLOCKED after five errors with the
    `loop.pause` queued behind it never applied."""
    ptask(layout, "A")
    layout.commands.mkdir(parents=True, exist_ok=True)
    (layout.commands / "0000000000001-bad.json").mkdir()
    commands.submit(layout, "loop.pause", {}, actor="owner")
    sup = Supervisor(layout, sleep_s=0)
    code = sup.run(max_iterations=10)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert runtime.read(layout).get("decision") == "PAUSE"
    assert sup.errors == []
    outcome = json.loads((layout.commands_applied / "0000000000001-bad.json").read_text(encoding="utf-8"))
    assert outcome["ok"] is False


def test_command_file_with_non_utf8_bytes_is_quarantined_with_its_bytes_intact(layout):
    """F9 (round-3 review of 2.6.1, probe i4): `apply_all()` only captured `raw` after a successful
    `read_text(encoding="utf-8")`, so a file whose bytes are not valid UTF-8 never reached the quarantine copy
    even though the loop stayed healthy and the `loop.pause` queued behind it still applied."""
    ptask(layout, "A")
    layout.commands.mkdir(parents=True, exist_ok=True)
    bad_bytes = b'{"kind": "loop.pause", "payload": {"x": "\xff\xfe"}}'
    (layout.commands / "0000000000001-bad.json").write_bytes(bad_bytes)
    commands.submit(layout, "loop.pause", {}, actor="owner")
    sup = Supervisor(layout, sleep_s=0)
    code = sup.run(max_iterations=10)
    assert code == 0
    assert runtime.read(layout).get("status") != "BLOCKED"
    assert runtime.read(layout).get("decision") == "PAUSE"
    assert list(commands.pending(layout)) == []
    quarantined = list((layout.commands / "quarantine").glob("*.json"))
    assert [p.name for p in quarantined] == ["0000000000001-bad.json"]
    assert quarantined[0].read_bytes() == bad_bytes
    outcome = json.loads((layout.commands_applied / "0000000000001-bad.json").read_text(encoding="utf-8"))
    assert outcome["ok"] is False


def test_lost_job_record_does_not_reuse_its_id_or_run_dir(layout):
    """Carried by the allocator's monotonic `counters` block now that `dispatch`
    no longer counts files at all: `jobs.open_records` — called by every Overview and Agents GET — never
    rewrites or quarantines a record it cannot read, and the next dispatch still never reuses an id or run
    directory, because nothing about it was ever derived from a file count to begin with."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")              # records stay PREPARED: nothing runs
    ids = [sup.dispatch(tasks.load(layout, "A"), "python")["attempt_id"] for _ in range(3)]
    assert ids == ["A.a1.s1", "A.a2.s1", "A.a3.s1"]
    (layout.runs / "A" / "A.a3.s1" / "marker.txt").write_text("belongs to the first A.a3.s1", encoding="utf-8")
    broken = "{broken"
    jobs.path(layout, "A.a2.s1").write_text(broken, encoding="utf-8")
    jobs.open_records(layout)                                  # what any Overview/Agents GET calls
    assert jobs.path(layout, "A.a2.s1").read_text(encoding="utf-8") == broken, "the unreadable record was rewritten"
    assert not jobs.path(layout, "A.a2.s1").with_suffix(".corrupt.json").exists(), "the record was quarantined"
    again = sup.dispatch(tasks.load(layout, "A"), "python")["attempt_id"]
    assert again not in ids, "the new job reused an id already on disk"
    assert not (layout.runs / "A" / again / "marker.txt").exists(), "the new job ran in an old run directory"
    all_job_ids = [j["id"] for a in tasks.load(layout, "A").attempts for j in a.jobs]
    assert sorted(all_job_ids) == sorted(ids + [again])


def test_overview_and_agents_pages_never_touch_an_unreadable_job_record(layout):
    """R-03: `open_records` (every Overview and Agents GET) must only read the jobs folder, never write or
    delete under it, however broken a record is."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    job_id = sup.dispatch(tasks.load(layout, "A"), "python")["attempt_id"]
    broken = "{broken"
    jobs.path(layout, job_id).write_text(broken, encoding="utf-8")
    c = testing.ui_client(layout)
    for url in ("/overview", "/agents"):
        r = c.get(url)
        assert r.status_code == 200, url
    assert jobs.path(layout, job_id).read_text(encoding="utf-8") == broken
    assert not jobs.path(layout, job_id).with_suffix(".corrupt.json").exists()
    assert list(layout.jobs.glob("A.*.json")) == [jobs.path(layout, job_id)]


def test_a_task_written_before_the_counter_existed_still_gets_the_right_next_number(layout):
    """A task document from before `counters` existed has no such field. The next
    dispatch must still open attempt 1 — the allocator treats a missing block exactly as an empty one, no
    migration step, and (unlike the old `job_seq`/file-count scheme this superseded) never reads a job file to
    find out."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")

    def fn(doc):
        doc.pop("counters", None)              # a document written before this block was added
        doc.pop("job_seq", None)
    tasks._update(layout, "A", fn)
    again = sup.dispatch(tasks.load(layout, "A"), "python")["attempt_id"]
    assert again == "A.a1.s1"


def test_dispatch_refuses_a_run_directory_that_already_exists(layout):
    """R-03: a job id must never be used a second time while its old run directory is still there."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    sup.dispatch(tasks.load(layout, "A"), "python")       # A.a1.s1
    stray = layout.runs / "A" / "A.a2.s1"
    stray.mkdir(parents=True)
    (stray / "marker.txt").write_text("left behind", encoding="utf-8")
    rec = sup.dispatch(tasks.load(layout, "A"), "python")  # would naturally be A.a2.s1
    assert rec["attempt_id"] == "A.a2.s1"
    assert rec["result"] == "FAILED"
    assert "already exists" in rec.get("error", "")
    assert (stray / "marker.txt").exists()     # the stray directory was not touched


# ------------------------------------------------------------------ R-04: cancel retires jobs

def test_cancelled_tasks_queued_job_never_launches_on_resume(layout):
    """R-04: `tasks.set_status` changes only the status; `reconcile` launches every PREPARED record regardless;
    `gate.check` has no terminal-status test. A job queued while paused must not run once the task is
    cancelled and the loop resumes."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python")
    assert rec["phase"] == "PREPARED"
    commands.submit(layout, "task.set_status", {"task_id": "A", "status": "CANCELLED", "note": "owner cancel"},
                    actor="owner")
    commands.apply_all(layout)
    assert tasks.load(layout, "A").status == TaskStatus.CANCELLED
    runtime.set_decision(layout, "RUN", "test")
    sup.once()
    assert jobs.resolve(layout, rec["attempt_id"])["phase"] != "PREPARED", "the cancelled task's job still launched"
    assert not (layout.root / "A.txt").exists(), "a job of a cancelled task wrote its artifact"
    assert tasks.load(layout, "A").status == TaskStatus.CANCELLED


def test_cancel_terminates_a_running_job_and_its_late_result_changes_nothing(layout):
    """R-04: a running job is not stopped by cancelling its task, and a late result for a terminal task must not
    record a delivery or a verification."""
    proc_task(layout, "P", seconds=20)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")
    assert rec["phase"] == "SUBMITTED"
    from supervisor.adapters import REGISTRY
    assert REGISTRY["process"].alive(rec["handle"]), "the process never started"
    commands.submit(layout, "task.set_status", {"task_id": "P", "status": "CANCELLED", "note": "owner cancel"},
                    actor="owner")
    commands.apply_all(layout)
    closed = jobs.resolve(layout, rec["attempt_id"])   # R-23: CANCELLED is terminal, so this may be archived
    assert closed["phase"] == "DONE"
    assert closed["result"] == "CANCELLED"
    time.sleep(0.5)
    assert not REGISTRY["process"].alive(rec["handle"]), "the running job kept running after its task was cancelled"
    sup.once()                       # the dead process, if ever collected again, must change nothing
    assert not (layout.root / "P.txt").exists()
    t = tasks.load(layout, "P")
    assert t.status == TaskStatus.CANCELLED
    assert t.delivery.digest is None


def test_cancel_through_an_inbox_decision_also_retires_jobs(layout):
    """R-04: `inbox.apply_decision` cancels a task the same way `task.set_status` does; its CANCEL answer must
    retire jobs too."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    rec = sup.dispatch(tasks.load(layout, "A"), "python")
    item = testing.make_inbox_item(layout, kind="decision", task_id="A", subject="stopped",
                                   summary="A is stopped", allowed_responses=list(inbox.DECISION_RESPONSES))
    tasks.set_status(layout, "A", "BLOCKED", actor="loop", note="stopped for the owner")
    testing.answer(layout, item["id"], "CANCEL", by="owner")
    assert tasks.load(layout, "A").status == TaskStatus.CANCELLED
    runtime.set_decision(layout, "RUN", "test")
    sup.once()
    assert jobs.resolve(layout, rec["attempt_id"])["phase"] != "PREPARED", "the cancelled task's job still launched"
    assert not (layout.root / "A.txt").exists()


def test_cancel_then_reopen_then_dispatch_gives_the_new_job_its_own_id(layout):
    """R-04: cancelling and reopening a task must leave its old job closed, and the next dispatch must get a
    distinct id rather than reuse or revive the cancelled one."""
    ptask(layout, "A")
    sup = Supervisor(layout, sleep_s=0)
    runtime.set_decision(layout, "PAUSE", "test")
    old = sup.dispatch(tasks.load(layout, "A"), "python")["attempt_id"]
    commands.submit(layout, "task.set_status", {"task_id": "A", "status": "CANCELLED", "note": "owner cancel"},
                    actor="owner")
    commands.apply_all(layout)
    commands.submit(layout, "task.set_status", {"task_id": "A", "status": "NOT_STARTED", "note": "owner reopen"},
                    actor="owner")
    commands.apply_all(layout)
    assert tasks.load(layout, "A").status == TaskStatus.NOT_STARTED
    new = sup.dispatch(tasks.load(layout, "A"), "python")["attempt_id"]
    assert new != old
    old_rec = jobs.resolve(layout, old)   # R-23: belonged to the now-closed CANCELLED episode, may be archived
    assert old_rec["phase"] == "DONE"
    assert old_rec["result"] == "CANCELLED"


def test_gate_refuses_every_job_kind_for_a_terminal_task(layout):
    """R-04: `gate.check` has no terminal-status test today; a late-arriving record of any kind must never
    launch against a task that is already CANCELLED or COMPLETE."""
    from supervisor import gate
    ptask(layout, "A")
    tasks.set_status(layout, "A", "CANCELLED", actor="owner", note="owner cancel")
    task = tasks.load(layout, "A")
    for kind in ("delivery", "verify", "review", "diagnosis"):
        reason = gate.check(layout, task, "python", kind, running=[])
        assert reason is not None and "CANCELLED" in reason, kind
