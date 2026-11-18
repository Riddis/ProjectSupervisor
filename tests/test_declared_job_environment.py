"""R-24, ATTEMPT_MODEL.md section 4.4: a process job's subprocess starts from a declared, allow-listed
environment — never the loop's whole `os.environ` — and the loop refuses to start under another supervisor's
own job-identity markers."""
from __future__ import annotations

import json
import sys

from supervisor import envconfig, jobs as jobs_mod, tasks
from supervisor.loop import ForeignEnvironmentError, Supervisor, _check_launch_environment_guard
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

SCRIPT = ("import json, os, pathlib\n"
          "run = pathlib.Path(os.environ['SUPERVISOR_RUN_DIR'])\n"
          "(run / 'env.json').write_text(json.dumps(dict(os.environ)))\n"
          "(run / 'result.json').write_text(json.dumps({'status': 'COMPLETE', 'artifacts': [], 'notes': 'ok'}))\n")


def _dispatch_process_job(layout, task_id: str) -> str:
    make_task(layout, task_id)
    tasks._update(layout, task_id, lambda d: d.update(job={"adapter": "process", "argv": [sys.executable, "-c", SCRIPT]},
                                                       artifacts=[]))
    sup = Supervisor(layout, sleep_s=0)
    attempt_id = None
    for _ in range(20):
        sup.once()
        t = tasks.load(layout, task_id)
        if t.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
            attempts = jobs_mod.for_task(layout, task_id)
            attempt_id = attempts[-1]["attempt_id"]
            break
    assert attempt_id, f"{task_id} never produced a job"
    return attempt_id


def test_a_jobs_environment_holds_only_declared_names_plus_lane_additions_and_supervisor_markers(layout, monkeypatch):
    monkeypatch.setenv("MY_UNDECLARED_VENDOR_VAR", "leak-me-not")
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.root / "config" / "routing.yaml").write_text(
        "env:\n  base: [PATH]\n  lanes:\n    process: {add: {MY_DECLARED_VAR: hello}}\n", encoding="utf-8")
    attempt_id = _dispatch_process_job(layout, "E1")
    env = json.loads((layout.runs / "E1" / attempt_id / "env.json").read_text(encoding="utf-8"))
    assert env.get("MY_DECLARED_VAR") == "hello"
    assert "MY_UNDECLARED_VENDOR_VAR" not in env
    assert env.get("SUPERVISOR_ATTEMPT") == attempt_id
    assert env.get("SUPERVISOR_RUN_DIR")


def test_an_undeclared_loop_variable_never_reaches_the_job(layout, monkeypatch):
    monkeypatch.setenv("SOME_OTHER_VENDOR_TOKEN", "should-not-appear")
    attempt_id = _dispatch_process_job(layout, "E2")
    env = json.loads((layout.runs / "E2" / attempt_id / "env.json").read_text(encoding="utf-8"))
    assert "SOME_OTHER_VENDOR_TOKEN" not in env


def test_env_digest_is_stable_for_an_unchanged_declaration_and_moves_when_it_changes(layout):
    env_a, digest_a = envconfig.build(layout, "process")
    env_a2, digest_a2 = envconfig.build(layout, "process")
    assert digest_a == digest_a2
    assert env_a == env_a2
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.root / "config" / "routing.yaml").write_text(
        "env:\n  base: [PATH]\n  lanes:\n    process: {add: {NEW_VAR: changed}}\n", encoding="utf-8")
    _, digest_b = envconfig.build(layout, "process")
    assert digest_b != digest_a


def test_two_dispatched_jobs_with_the_same_declaration_carry_the_same_env_digest_on_their_record(layout):
    a1 = _dispatch_process_job(layout, "E3")
    a2 = _dispatch_process_job(layout, "E4")
    rec1, rec2 = jobs_mod.read(layout, a1), jobs_mod.read(layout, a2)
    assert rec1.get("env_digest") and rec1["env_digest"] == rec2["env_digest"]


def test_no_env_value_is_ever_written_to_the_job_record_or_the_log(layout):
    (layout.secrets).mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text("A_LANE_SECRET=super-secret-value\n", encoding="utf-8")
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.root / "config" / "routing.yaml").write_text(
        "env:\n  base: [PATH]\n  lanes:\n    process: {secrets: [A_LANE_SECRET]}\n", encoding="utf-8")
    attempt_id = _dispatch_process_job(layout, "E5")
    rec = jobs_mod.read(layout, attempt_id)
    assert "super-secret-value" not in json.dumps(rec)
    assert isinstance(rec.get("env_digest"), str) and rec["env_digest"]
    if layout.log.exists():
        assert "super-secret-value" not in layout.log.read_text(encoding="utf-8")


def test_the_loop_refuses_to_start_under_a_foreign_supervisors_markers(monkeypatch):
    for name in envconfig.FOREIGN_MARKERS:
        monkeypatch.delenv(name, raising=False)
    _check_launch_environment_guard()           # a clean launching environment: must not raise
    monkeypatch.setenv("SUPERVISOR_ATTEMPT", "OTHER-TASK.a1")
    monkeypatch.setenv("SUPERVISOR_JOB", "OTHER-TASK.a1")
    try:
        _check_launch_environment_guard()
        assert False, "expected ForeignEnvironmentError"
    except ForeignEnvironmentError as exc:
        assert "SUPERVISOR_ATTEMPT" in str(exc) and "SUPERVISOR_JOB" in str(exc)


def test_supervisor_root_alone_is_not_a_foreign_marker(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ATTEMPT", raising=False)
    monkeypatch.delenv("SUPERVISOR_JOB", raising=False)
    monkeypatch.delenv("SUPERVISOR_ATTEMPT_NO", raising=False)
    monkeypatch.delenv("SUPERVISOR_RUN_DIR", raising=False)
    monkeypatch.delenv("SUPERVISOR_PROGRESS_FILE", raising=False)
    monkeypatch.setenv("SUPERVISOR_ROOT", "C:/somewhere")
    _check_launch_environment_guard()            # must not raise: ROOT is this loop's own self-reference
