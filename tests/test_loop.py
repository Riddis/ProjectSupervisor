import json, os, subprocess, sys, time
import pytest

from supervisor import commands, jobs, runtime, store, tasks
from supervisor.adapters import python as pyad
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


@pyad.job("write_artifact")
def _write_artifact(root, run_dir, name="out.txt", content="x", **_):
    (root / name).write_text(content)
    return {"status": "COMPLETE", "artifacts": [name], "notes": "wrote " + name}


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
    recs = list(layout.jobs.glob("A.*.json"))
    assert [r.name for r in recs] == ["A.a1.json"]
    assert json.loads(recs[0].read_text())["phase"] == "DONE"
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE


def test_crash_after_submit_adopts_live_child(layout):
    proc_task(layout, "P", seconds=1.5)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")      # submitted; now "crash"
    assert rec["phase"] == "SUBMITTED"
    sup2 = Supervisor(layout, sleep_s=0)
    sup2.once()
    assert jobs.read(layout, "P.a1")["phase"] == "RUNNING"       # adopted, not re-run
    deadline = time.time() + 10
    while time.time() < deadline and jobs.read(layout, "P.a1")["phase"] != "DONE":
        sup2.once(); time.sleep(0.2)
    assert jobs.read(layout, "P.a1")["phase"] == "DONE"
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE
    assert len(list(layout.jobs.glob("P.*.json"))) == 1


def test_crash_after_result_before_done_ingests_without_rerun(layout):
    proc_task(layout, "P", seconds=0.1)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")
    time.sleep(1.5)                                              # child finished, result.json written, record still SUBMITTED
    assert (layout.root / "P.txt").exists()
    Supervisor(layout, sleep_s=0).once()
    assert jobs.read(layout, "P.a1")["phase"] == "DONE"
    assert len(list(layout.jobs.glob("P.*.json"))) == 1
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE


def test_lost_process_marks_failed(layout):
    proc_task(layout, "P", seconds=30)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "P"), "process")
    from supervisor.adapters import REGISTRY
    REGISTRY["process"].terminate(rec["handle"])
    time.sleep(0.5)
    Supervisor(layout, sleep_s=0).once()
    assert jobs.read(layout, "P.a1")["phase"] == "DONE"
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
    rec = jobs.read(layout, "P.a1")
    assert rec.get("recovered") is True and rec["handle"]["pid"] == handle["pid"]
    import psutil
    assert len([p for p in psutil.Process().children(recursive=True) if p.pid == handle["pid"]]) <= 1
    deadline = time.time() + 10
    while time.time() < deadline and jobs.read(layout, "P.a1")["phase"] != "DONE":
        sup2.once(); time.sleep(0.2)
    assert tasks.load(layout, "P").status == TaskStatus.COMPLETE
    assert len(list(layout.jobs.glob("P.*.json"))) == 1


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
