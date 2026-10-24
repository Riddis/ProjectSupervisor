"""Activity-based time limits (1.7.0): agent sessions used to be killed at exactly one hour while still working,
and each kill spent a revision. Now a job is stopped only when it goes quiet (no output, no CPU work in its
processes) or reaches a generous hard cap the owner can extend; a model-lane timeout is a continuation."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from supervisor import commands, jobs, tasks, timeouts
from supervisor._vendor.contract import atomic_json, read_json
from supervisor.adapters import REGISTRY
from supervisor.adapters.claude_cli import model_limits
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


# ------------------------------------------------------------------ resolution

def test_defaults_are_twenty_idle_minutes_and_a_six_hour_cap():
    assert timeouts.resolve({}) == {"idle_s": 1200, "hard_cap_s": 21600, "idle_cpu_s": 5.0}


def test_later_levels_win_global_cost_role_task():
    cfg = {"timeouts": {"idle_s": 900, "hard_cap_s": 7200, "by_cost": {"local": {"hard_cap_s": 28800}}}}
    assert timeouts.resolve(cfg)["hard_cap_s"] == 7200 and timeouts.resolve(cfg)["idle_s"] == 900
    assert timeouts.resolve(cfg, "local")["hard_cap_s"] == 28800
    assert timeouts.resolve(cfg, "local", {"timeouts": {"hard_cap_s": 3000, "idle_s": 600}})["hard_cap_s"] == 3000
    lim = timeouts.resolve(cfg, "local", {"timeouts": {"idle_s": 600}}, {"timeout_s": 999, "idle_timeout_s": 0})
    assert lim["hard_cap_s"] == 999 and lim["idle_s"] is None          # 0 switches the idle limit off


def test_claude_cli_prepare_carries_the_resolved_limits(layout):
    import yaml
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({
        "timeouts": {"idle_s": 1500, "by_cost": {"claude": {"hard_cap_s": 10800}}},
        "roles": {"engineer": {"adapter": "claude_cli", "model": "sonnet", "tools": "writer"}}}), encoding="utf-8")
    make_task(layout, "T")
    spec = REGISTRY["claude_cli"].prepare(layout, tasks.load(layout, "T"), "T.a1")
    assert (spec["timeout_s"], spec["idle_s"], spec["idle_cpu_s"]) == (10800, 1500, 5.0)


# ------------------------------------------------------------------ activity measurement

def test_output_growth_and_cpu_work_count_as_activity(tmp_path, monkeypatch):
    cpu = {"v": 10.0}
    monkeypatch.setattr(timeouts, "tree_cpu", lambda pid, ct: cpu["v"])
    handle = {"pid": 1, "create_time": 1.0, "started": "2026-10-24T08:00:00+00:00"}
    start = timeouts._epoch(handle["started"])
    assert timeouts.last_active(tmp_path, handle, 5.0, now=start + 10) == start + 10  # first sample opens the window
    cpu["v"] = 13.0                                                                    # 3 s CPU: below threshold
    assert timeouts.last_active(tmp_path, handle, 5.0, now=start + 100) == start + 10
    cpu["v"] = 15.5                                                                    # 5.5 s since the baseline
    assert timeouts.last_active(tmp_path, handle, 5.0, now=start + 200) == start + 200
    (tmp_path / "stdout.jsonl").write_text('{"type":"assistant"}\n')                 # new output
    assert timeouts.last_active(tmp_path, handle, 5.0, now=start + 300) == start + 300
    cpu["v"] = 2.0                                                                     # a child exited: rebaseline, no credit
    assert timeouts.last_active(tmp_path, handle, 5.0, now=start + 400) == start + 300
    assert timeouts.read_activity(tmp_path) == start + 300


def _quiet_spec(tmp_path, idle_s, timeout_s=3600, code="import time; time.sleep(30)"):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return {"argv": [sys.executable, "-c", code], "timeout_s": timeout_s, "idle_s": idle_s, "idle_cpu_s": 5.0,
            "run_dir": str(run_dir), "root": str(tmp_path), "attempt_id": "X.a1"}


def _wait_result(ad, spec, handle, seconds):
    end = time.time() + seconds
    while time.time() < end:
        res = ad.collect(spec, handle)
        if res is not None:
            return res
        time.sleep(0.2)
    return None


def test_a_quiet_process_is_stopped_as_idle(tmp_path):
    ad = ProcessAdapter()
    spec = _quiet_spec(tmp_path, idle_s=1)
    handle = ad.submit(spec)
    try:
        res = _wait_result(ad, spec, handle, 10)
        assert res is not None and res.status == "FAILED" and res.raw == {"timeout": "idle"}
        assert res.error.startswith("timeout: idle")
    finally:
        ad.terminate(handle)


def test_a_process_that_keeps_writing_outlives_its_idle_limit(tmp_path):
    ad = ProcessAdapter()
    code = "import time, sys\nfor i in range(12):\n    print(i, flush=True); time.sleep(0.25)"
    spec = _quiet_spec(tmp_path, idle_s=1, code=code)
    handle = ad.submit(spec)
    try:
        res = _wait_result(ad, spec, handle, 10)                       # 3 s of work against a 1 s idle limit
        assert res is not None and res.raw.get("timeout") is None      # finished on its own (no result.json → FAILED)
        assert "without result.json" in (res.error or "")
    finally:
        ad.terminate(handle)


def test_the_hard_cap_stops_even_a_busy_process(tmp_path):
    ad = ProcessAdapter()
    spec = _quiet_spec(tmp_path, idle_s=None)
    handle = ad.submit(spec)
    handle["deadline"] = time.time() - 1
    try:
        res = ad.collect(spec, handle)
        assert res.raw == {"timeout": "cap"} and "hard cap" in res.error and "Extend" in res.error
    finally:
        ad.terminate(handle)


def test_plain_process_jobs_keep_their_cap_and_get_no_idle_limit(layout):
    from tests.test_loop import proc_task
    proc_task(layout, "P")
    spec = REGISTRY["process"].prepare(layout, tasks.load(layout, "P"), "P.a1")
    assert spec["timeout_s"] == 3600 and spec["idle_s"] is None


# ------------------------------------------------------------------ model lanes

def test_a_session_prepared_before_the_change_moves_onto_the_new_policy(tmp_path):
    started = time.time() - 3500
    spec = {"root": str(tmp_path), "role": "engineer", "timeout_s": 3600, "run_dir": str(tmp_path)}   # no idle_s
    handle = {"started": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(started)), "deadline": started + 3600}
    deadline, idle_s, _ = model_limits(REGISTRY["claude_cli"], spec, handle)
    assert deadline == pytest.approx(started + 21600, abs=2) and idle_s == 1200
    chat = dict(spec, timeout_s=600)                                   # a deliberate cap is kept
    assert model_limits(REGISTRY["claude_cli"], chat, {**handle, "deadline": started + 600})[0] == started + 600


class ModelLane(ProcessAdapter):
    handoff_before_cap = True                                         # what claude_cli / codex_cli set


def test_model_lanes_ask_for_a_handoff_before_the_cap():
    assert REGISTRY["claude_cli"].handoff_before_cap and REGISTRY["codex_cli"].handoff_before_cap
    assert not REGISTRY["process"].handoff_before_cap


def test_a_model_session_is_asked_for_its_handoff_before_the_cap(tmp_path):
    ad = ModelLane()
    spec = _quiet_spec(tmp_path, idle_s=None, timeout_s=21600)
    handle = ad.submit(spec)
    try:
        handle["deadline"] = time.time() + 300                       # inside HANDOFF_WARN_S
        assert ad.collect(spec, handle) is None
        req = read_json(Path(spec["run_dir"]) / "HANDOFF_REQUEST.json")
        assert req["reason"] == "time limit"
    finally:
        ad.terminate(handle)


class QuietClaude(ProcessAdapter):
    """Runs on the claude_cli lane but launches a python child that never writes: an idle session."""
    name = "claude_cli"

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "HANDOFF.md").write_text("# handoff\nstep 1 of 3 done\n", encoding="utf-8")
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                "timeout_s": 21600, "idle_s": 1, "idle_cpu_s": 5.0, "kind": "delivery"}


def test_a_timed_out_session_is_continued_not_counted_as_a_failed_attempt(layout, monkeypatch):
    monkeypatch.setitem(REGISTRY, "claude_cli", QuietClaude())
    make_task(layout, "T", status="READY")
    sup = Supervisor(layout, sleep_s=0)
    end = time.time() + 20
    while time.time() < end and not any("continuation" in str(r.get("deferred_reason", "")) for r in jobs.for_task(layout, "T")):
        sup.once()
        time.sleep(0.2)
    recs = {r["attempt_id"]: r for r in jobs.for_task(layout, "T")}
    first = recs["T.a1"]
    assert first["phase"] == "DONE" and first.get("continued") and first["error"].startswith("timeout: idle")
    cont = [r for r in recs.values() if str(r.get("deferred_reason", "")).startswith("continuation after time limit (idle)")]
    assert cont, recs
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS            # not FAILED, no revision spent
    assert not (layout.runtime / "escalation" / "T.json").exists()
    log = [json.loads(line) for line in layout.log.read_text().splitlines()]
    assert any(e.get("event") == "job_requeued" and e.get("reason") == "time limit (idle)" for e in log)
    for r in jobs.open_records(layout):                                        # stop the continuation's child
        if r.get("handle"):
            REGISTRY["claude_cli"].terminate(r["handle"])


# ------------------------------------------------------------------ Extend

def _running_job(layout, deadline):
    run_dir = layout.runs / "T" / "T.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    make_task(layout, "T")
    handle = {"kind": "process", "pid": 999999, "create_time": 0, "started": "2026-10-24T08:00:00+00:00", "deadline": deadline}
    jobs.prepare(layout, "T", "T.a1", "claude_cli", {"run_dir": str(run_dir), "idle_s": 1200, "timeout_s": 21600})
    jobs.set_phase(layout, "T.a1", "RUNNING", handle=handle)
    atomic_json(run_dir / "status.json", {"status": "RUNNING", **handle})
    return run_dir


def test_extend_pushes_the_cap_once_per_command_and_survives_readoption(layout):
    deadline = time.time() + 100
    run_dir = _running_job(layout, deadline)
    atomic_json(run_dir / "HANDOFF_REQUEST.json", {"requested": True, "reason": "time limit"})
    out = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1"}, "owner-ui")
    assert out["extended_s"] == 3600
    handle = jobs.read(layout, "T.a1")["handle"]
    assert handle["deadline"] == pytest.approx(deadline + 3600) and handle["extensions"][0]["by"] == "owner-ui"
    assert not (run_dir / "HANDOFF_REQUEST.json").exists()                     # the session keeps working
    again = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1"}, "owner-ui")
    assert again["skipped"] == "already applied"
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(deadline + 3600)
    st = read_json(run_dir / "status.json")
    assert st["deadline"] == pytest.approx(deadline + 3600)
    adopted = ProcessAdapter().recover({"run_dir": str(run_dir)})
    assert adopted["deadline"] == pytest.approx(deadline + 3600) and adopted["extensions"]


def test_extend_refuses_a_job_that_is_not_running(layout):
    _running_job(layout, time.time() + 100)
    jobs.set_phase(layout, "T.a1", "DONE")
    assert commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1"}, "owner")["skipped"] == "not running"


def test_control_center_shows_the_cap_and_extends_it(layout):
    from fastapi.testclient import TestClient

    from supervisor.ui.app import create_app
    from supervisor.ui.auth import CSRF_COOKIE
    from supervisor.ui.routes.overview import now_running
    started = timeouts._epoch("2026-10-24T08:00:00+00:00")
    _running_job(layout, started + 21600)
    rows = now_running(jobs.open_records(layout), tasks.load_all(layout), now=started + 600)
    assert rows[0]["timeout_s"] == 21600 and rows[0]["can_extend"] and rows[0]["idle_s"] == 1200
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set(CSRF_COOKIE, "t")
    assert 'action="/agents/T.a1/extend"' in client.get("/overview").text
    r = client.post("/agents/T.a1/extend", data={"csrf_token": "t"}, follow_redirects=False)
    assert r.status_code == 303
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(started + 21600 + 3600)
    rows = now_running(jobs.open_records(layout), tasks.load_all(layout), now=started + 600)
    assert rows[0]["timeout_s"] == 21600 + 3600 and rows[0]["extended_s"] == 3600
    assert "Extend +1h" in client.get("/agents/T.a1").text


# ------------------------------------------------------------------ upgrade edges (review of 1.7.0)

class LegacyClaude(ProcessAdapter):
    """The real subprocess path with the real claude_cli limits (model_limits): no model client is involved."""
    name = "claude_cli"
    handoff_before_cap = True
    limits = model_limits


def _legacy_spec(tmp_path, code):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return {"argv": [sys.executable, "-c", code], "timeout_s": 3600, "run_dir": str(run_dir),   # no idle_s: pre-1.7.0
            "root": str(tmp_path), "role": "engineer", "attempt_id": "L.a1"}


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _wait_growth(run_dir: Path, seconds: float = 10) -> None:
    out, end = run_dir / "stdout.jsonl", time.time() + seconds
    while time.time() < end and not (out.exists() and out.stat().st_size):
        time.sleep(0.05)


def test_a_busy_legacy_session_survives_its_first_collect(tmp_path):
    """Adopted after the upgrade 30 minutes into its run (idle limit 20 min), with no activity.json yet and its old
    deadline still ahead: the first look must not call it idle while its output is growing."""
    ad = LegacyClaude()
    spec = _legacy_spec(tmp_path, "import time\nfor i in range(400):\n    print(i, flush=True); time.sleep(0.05)")
    handle = ad.submit(spec)
    try:
        handle["started"] = _iso(time.time() - 1800)
        handle["deadline"] = time.time() + 1800
        _wait_growth(Path(spec["run_dir"]))
        assert not (Path(spec["run_dir"]) / timeouts.ACTIVITY_FILE).exists()
        assert ad.collect(spec, handle) is None                        # busy: kept running
        time.sleep(0.3)
        assert ad.collect(spec, handle) is None
        assert time.time() - timeouts.read_activity(spec["run_dir"]) < 5
    finally:
        ad.terminate(handle)


def test_a_quiet_legacy_session_is_still_stopped_after_a_full_idle_window(tmp_path):
    import yaml
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "routing.yaml").write_text(yaml.safe_dump({"timeouts": {"idle_s": 1}}), encoding="utf-8")
    ad = LegacyClaude()
    spec = _legacy_spec(tmp_path, "import time; time.sleep(30)")
    handle = ad.submit(spec)
    try:
        handle["started"] = _iso(time.time() - 1800)                   # long past the 1 s idle limit
        assert ad.collect(spec, handle) is None                        # first sample: the window opens now
        res = _wait_result(ad, spec, handle, 10)
        assert res is not None and res.raw == {"timeout": "idle"}
    finally:
        ad.terminate(handle)


def _legacy_job(layout, started):
    run_dir = layout.runs / "T" / "T.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    make_task(layout, "T")
    handle = {"kind": "process", "pid": 999999, "create_time": 0, "started": _iso(started), "deadline": started + 3600}
    jobs.prepare(layout, "T", "T.a1", "claude_cli", {"run_dir": str(run_dir), "root": str(layout.root),
                                                     "role": "engineer", "timeout_s": 3600})   # pre-1.7.0 spec
    jobs.set_phase(layout, "T.a1", "RUNNING", handle=handle)
    atomic_json(run_dir / "status.json", {"status": "RUNNING", **handle})
    return run_dir


def test_extend_on_a_legacy_session_extends_its_effective_cap(layout):
    """5h55m into a pre-1.7.0 session: the effective (six-hour) cap is 5 min away and the session was asked for its
    handoff. Extend must add the hour to that cap, record it, show it, and withdraw the request."""
    from supervisor.ui.routes.agents import _enrich
    from supervisor.ui.routes.overview import now_running
    now = time.time()
    started = now - (5 * 3600 + 55 * 60)
    run_dir = _legacy_job(layout, started)
    effective = timeouts.effective_limits("claude_cli", jobs.read(layout, "T.a1")["spec"], jobs.read(layout, "T.a1")["handle"])[0]
    assert effective == pytest.approx(started + 21600, abs=2)
    atomic_json(run_dir / "HANDOFF_REQUEST.json", {"requested": True, "reason": "time limit"})
    out = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1"}, "owner-ui")
    assert out["deadline"] == pytest.approx(started + 21600 + 3600, abs=2)
    rec = jobs.read(layout, "T.a1")
    assert rec["handle"]["deadline"] == pytest.approx(out["deadline"])                   # recorded = effective
    assert timeouts.effective_limits("claude_cli", rec["spec"], rec["handle"])[0] == pytest.approx(out["deadline"])
    assert not (run_dir / "HANDOFF_REQUEST.json").exists()                               # keeps working
    assert read_json(run_dir / "status.json")["deadline"] == pytest.approx(out["deadline"])
    assert ProcessAdapter().recover({"run_dir": str(run_dir)})["deadline"] == pytest.approx(out["deadline"])
    row = now_running(jobs.open_records(layout), tasks.load_all(layout), now=time.time())[0]
    assert row["timeout_s"] == pytest.approx(21600 + 3600, abs=2) and row["idle_s"] == 1200
    assert _enrich(layout, rec)["cap_left_s"] == pytest.approx(65 * 60, abs=5)
    commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c2"}, "owner-ui")   # a second hour
    rec = jobs.read(layout, "T.a1")
    assert timeouts.effective_limits("claude_cli", rec["spec"], rec["handle"])[0] == pytest.approx(started + 21600 + 7200, abs=2)


def test_replaying_extend_repairs_an_interrupted_update(layout, monkeypatch):
    """Crash after the durable job update, before status.json and the handoff request: the replay completes both
    without adding the hour a second time."""
    deadline = time.time() + 100
    run_dir = _running_job(layout, deadline)
    atomic_json(run_dir / "HANDOFF_REQUEST.json", {"requested": True, "reason": "time limit"})
    real = commands.atomic_json

    def crash_on_status(path, data, *a, **kw):
        if Path(path).name == "status.json":
            raise SystemExit("killed between the job write and the status write")
        return real(path, data, *a, **kw)

    monkeypatch.setattr(commands, "atomic_json", crash_on_status)
    with pytest.raises(SystemExit):
        commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1"}, "owner-ui")
    monkeypatch.setattr(commands, "atomic_json", real)
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(deadline + 3600)   # durable
    assert read_json(run_dir / "status.json")["deadline"] == pytest.approx(deadline)           # stale
    assert (run_dir / "HANDOFF_REQUEST.json").exists()
    again = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1"}, "owner-ui")
    assert again["skipped"] == "already applied" and set(again["repaired"]) == {"status", "handoff_request"}
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(deadline + 3600)   # not twice
    assert len(jobs.read(layout, "T.a1")["handle"]["extensions"]) == 1
    st = read_json(run_dir / "status.json")
    assert st["deadline"] == pytest.approx(deadline + 3600) and len(st["extensions"]) == 1
    assert not (run_dir / "HANDOFF_REQUEST.json").exists()
    adopted = ProcessAdapter().recover({"run_dir": str(run_dir)})
    assert adopted["deadline"] == pytest.approx(deadline + 3600) and adopted["extensions"]
    log = [json.loads(line) for line in layout.log.read_text().splitlines()]
    assert any(e.get("event") == "job_extend_repaired" for e in log)
    third = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1"}, "owner-ui")
    assert third["repaired"] == []                                                            # nothing left to do
