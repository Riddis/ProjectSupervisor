"""One log line per owner action, and progress reports from long jobs.

A command whose handler logs its own event (Extend → `job_extended`) no longer adds a generic `command` line; the
event carries the command's id instead. A running job's PROGRESS.json shows in the Control Center, and the loop logs
one `job_progress` line per stage."""
from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from supervisor import commands, jobs, progress, tasks
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from supervisor.ui.routes.overview import now_running
from tests.conftest import make_task


def log_lines(layout) -> list[dict]:
    if not layout.log.exists():
        return []
    return [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines() if line.strip()]


def running(layout, attempt="T.a1", run_dir=True) -> dict:
    rd = layout.runs / "T" / attempt
    rd.mkdir(parents=True, exist_ok=True)
    started = "2026-01-01T00:00:00+00:00"
    return jobs.write(layout, {"attempt_id": attempt, "task_id": "T", "adapter": "process", "phase": "RUNNING",
                               "spec": {"kind": "delivery", "run_dir": str(rd) if run_dir else None},
                               "handle": {"started": started, "deadline": time.time() + 3600},
                               "created": started})


# ------------------------------------------------------------------ one line per action

def test_extend_writes_one_log_line_carrying_the_command_id(layout):
    make_task(layout, "T")
    running(layout)
    before = len(log_lines(layout))
    commands.submit(layout, "agent.extend", {"attempt_id": "T.a1"}, "owner-ui")
    out = commands.apply_all(layout)
    assert out[0]["ok"]
    new = log_lines(layout)[before:]
    assert [r["event"] for r in new] == ["job_extended"]
    assert new[0]["command"] == out[0]["id"]


def test_a_failed_command_still_gets_its_generic_line(layout):
    commands.submit(layout, "agent.extend", {"attempt_id": "T.a1", "seconds": -5}, "owner-ui")
    make_task(layout, "T")
    running(layout)
    commands.apply_all(layout)
    last = log_lines(layout)[-1]
    assert last["event"] == "command" and last["ok"] is False and "extend by" in last["error"]


def test_a_handler_that_logs_nothing_keeps_the_generic_line(layout):
    commands.submit(layout, "agent.extend", {"attempt_id": "nope.a1"}, "owner-ui")     # skipped: no such job
    commands.apply_all(layout)
    last = log_lines(layout)[-1]
    assert last["event"] == "command" and last["ok"] is True


def test_lines_outside_a_command_carry_no_command_id(layout):
    tasks.log(layout, "something", "loop")
    assert "command" not in log_lines(layout)[-1]


# ------------------------------------------------------------------ progress reports

def test_report_writes_the_file_named_by_the_environment(tmp_path, monkeypatch):
    target = tmp_path / progress.PROGRESS_FILE
    monkeypatch.setenv(progress.ENV_VAR, str(target))
    assert progress.report("training", "step 300", 300, 500)
    got = progress.read(tmp_path)
    assert got["stage"] == "training" and got["detail"] == "step 300" and got["percent"] == 60.0


def test_report_outside_the_supervisor_does_nothing(monkeypatch):
    monkeypatch.delenv(progress.ENV_VAR, raising=False)
    assert progress.report("training") is False


@pytest.mark.parametrize("content", ["not json", "[]", '{"detail": "no stage"}', '{"stage": "  "}'])
def test_a_missing_or_malformed_report_shows_nothing(tmp_path, content):
    (tmp_path / progress.PROGRESS_FILE).write_text(content, encoding="utf-8")
    assert progress.read(tmp_path) is None
    assert progress.read(tmp_path / "absent") is None


def test_read_cleans_what_an_agent_wrote(tmp_path):
    (tmp_path / progress.PROGRESS_FILE).write_text(json.dumps(
        {"stage": "down\nloading " + "x" * 200, "detail": "a\tb", "done": True, "total": "500", "percent": 40}),
        encoding="utf-8")
    got = progress.read(tmp_path)
    assert "\n" not in got["stage"] and len(got["stage"]) <= progress.MAX_STAGE
    assert got["detail"] == "a b" and got["percent"] == 40.0          # bool/str numbers ignored, percent honoured


def test_the_loop_logs_each_stage_once(layout):
    make_task(layout, "T")
    rec = running(layout)
    rd = rec["spec"]["run_dir"]
    progress.report("downloading", "weights", file=progress.path(rd))
    progress.observe(layout, jobs.read(layout, "T.a1"))
    progress.report("downloading", "weights 2 of 3", 2, 3, file=progress.path(rd))
    progress.observe(layout, jobs.read(layout, "T.a1"))                 # same stage: no new line
    progress.report("training", "step 1", 1, 500, file=progress.path(rd))
    progress.observe(layout, jobs.read(layout, "T.a1"))
    lines = [r for r in log_lines(layout) if r["event"] == "job_progress"]
    assert [r["stage"] for r in lines] == ["downloading", "training"]
    assert jobs.read(layout, "T.a1")["progress_stage"] == "training"


def test_settle_observes_progress_of_a_job_still_running(layout, monkeypatch):
    make_task(layout, "T")
    rec = running(layout)
    progress.report("evaluating", file=progress.path(rec["spec"]["run_dir"]))
    monkeypatch.setattr(ProcessAdapter, "collect", lambda self, spec, handle: None)
    Supervisor(layout, sleep_s=0).settle()
    assert [r["stage"] for r in log_lines(layout) if r["event"] == "job_progress"] == ["evaluating"]


def test_launched_jobs_inherit_the_progress_file(layout):
    spec = {"attempt_id": "T.a1", "run_dir": str(layout.runs / "T" / "T.a1")}
    env = ProcessAdapter().env(spec)
    assert env[progress.ENV_VAR] == str(progress.path(spec["run_dir"]))
    assert os.environ.get(progress.ENV_VAR) != env[progress.ENV_VAR]       # the loop's own environment is untouched


def test_the_overview_and_agent_page_show_the_stage(layout):
    make_task(layout, "T")
    rec = running(layout)
    progress.report("training", "500 steps", 125, 500, file=progress.path(rec["spec"]["run_dir"]))
    rows = now_running([jobs.read(layout, "T.a1")], {"T": tasks.load(layout, "T")}, time.time())
    assert rows[0]["progress"]["stage"] == "training" and rows[0]["progress"]["percent"] == 25.0
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set(CSRF_COOKIE, "csrf")
    page = client.get("/agents/T.a1").text
    assert "Training" in page and "500 steps" in page and "125 of 500" in page


def test_the_brief_paragraph_names_the_file_and_the_variable(layout):
    text = progress.instructions(layout.runs / "T" / "T.a1")
    assert progress.PROGRESS_FILE in text and progress.ENV_VAR in text


@pytest.mark.parametrize("result_kind, asked", [("delivery", True), ("review", False), ("conversation", False)])
def test_a_delivery_prompt_asks_for_progress_and_a_review_does_not(layout, result_kind, asked):
    from supervisor.adapters import REGISTRY
    make_task(layout, "C")
    tasks._update(layout, "C", lambda doc: doc.__setitem__("job", {"adapter": "claude_cli", "result_kind": result_kind}))
    spec = REGISTRY["claude_cli"].prepare(layout, tasks.load(layout, "C"), "C.a1")
    prompt = (layout.runs / "C" / "C.a1" / "prompt.md").read_text(encoding="utf-8")
    assert ("## Progress reports" in prompt) is asked
    assert REGISTRY["claude_cli"].env(spec)[progress.ENV_VAR].endswith(progress.PROGRESS_FILE)
