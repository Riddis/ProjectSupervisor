"""The server watchdog (D-013 section 1.2): `ensure_started()` (called from the adapter's submit(), in the
loop's own process) kicks the watchdog off in the background and never blocks on a cold load; `run()` (the
watchdog's own entry point, a separate detached process) acquires the machine-wide GPU lease, starts the
configured argv, waits for health, then watches idle/release-request/child-liveness until one of them says
stop. A fake HTTP server stands in for the real endpoint's health check; a dummy sleeping child stands in for
the real server process (the watchdog only polls health over HTTP and `proc.poll()`, so the two need not be
the same process for this test)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil
import pytest

from supervisor import plugin, testing

import supervisor_plugin_llm_endpoint as llm_endpoint
from supervisor_plugin_llm_endpoint import _server

_PKG_ROOT = Path(__file__).resolve().parents[1]
_WORKTREE = Path(__file__).resolve().parents[3]


class _Health(BaseHTTPRequestHandler):
    ok = True

    def do_GET(self):
        self.send_response(200 if _Health.ok else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_health():
    _Health.ok = True
    srv = HTTPServer(("127.0.0.1", 0), _Health)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class _FakeProc:
    pid = 999999

    def poll(self):
        return None


# ---------------------------------------------------------------- F15 (P3): _health_ok accepts only 2xx


class _NotFound(BaseHTTPRequestHandler):
    """Serves a bare 404 everywhere - the shape of a misconfigured `health_path` (Ollama has no `/health`),
    not a server that is reachable but still loading (5xx) or genuinely healthy (2xx)."""

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_health_ok_refuses_a_404_instead_of_reading_any_status_below_500_as_healthy(tmp_path):
    """RELEASE_REVIEW_local-lane.md F15: at this head `_health_ok` reads `status_code < 500`, so a 404 from a
    `health_path` the server does not serve counts as ready. A misconfiguration must read as unhealthy, not
    as a server that may still be loading."""
    srv = HTTPServer(("127.0.0.1", 0), _NotFound)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert _server._health_ok(f"http://127.0.0.1:{srv.server_port}", "/health") is False
    finally:
        srv.shutdown()


def test_ensure_started_returns_true_and_spawns_nothing_when_already_healthy(tmp_path, fake_health, monkeypatch):
    calls = []
    monkeypatch.setattr(_server.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    cfg = {"base_url": fake_health, "health_path": "/health", "argv": ["unused"]}
    assert _server.ensure_started(cfg, str(tmp_path)) is True
    assert calls == []


def test_ensure_started_raises_without_argv_when_the_endpoint_is_unreachable(tmp_path):
    cfg = {"base_url": "http://127.0.0.1:1", "health_path": "/health", "argv": None}
    with pytest.raises(RuntimeError, match="argv"):
        _server.ensure_started(cfg, str(tmp_path))


def test_ensure_started_spawns_the_watchdog_once_when_unreachable_with_argv_configured(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(_server.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or _FakeProc())
    cfg = {"base_url": "http://127.0.0.1:1", "health_path": "/health", "argv": ["fake-server"]}
    assert _server.ensure_started(cfg, str(tmp_path)) is False
    assert len(calls) == 1
    assert "supervisor_plugin_llm_endpoint._server" in calls[0][0][0]
    # a second call before the spawn guard has expired: still only one spawn, not a flood of watchdogs
    assert _server.ensure_started(cfg, str(tmp_path)) is False
    assert len(calls) == 1


def test_is_idle_reads_the_activity_files_age(tmp_path):
    path = _server.activity_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert _server.is_idle(str(tmp_path), idle_stop_s=10, now=time.time()) is False
    assert _server.is_idle(str(tmp_path), idle_stop_s=10, now=time.time() + 20) is True


def test_is_idle_with_no_activity_file_reports_idle(tmp_path):
    assert _server.is_idle(str(tmp_path), idle_stop_s=10, now=time.time()) is True


def test_run_acquires_the_lease_starts_the_child_and_stops_on_idle(tmp_path, fake_health):
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    lay = testing.project(root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}}})
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 0.2,
          "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    why = _server.run(str(root), cfg, poll_s=0.05)
    assert why == "idle"
    assert plugin.resource_lease(lay, "gpu").release_requested() is False   # released cleanly, nothing left waiting
    state = json.loads(_server.state_path(str(root)).read_text(encoding="utf-8"))
    assert state["pid"] is None                                            # stopped: the state says so


def test_run_stops_on_a_release_request_from_another_project(tmp_path, fake_health):
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    lay = testing.project(root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}}})
    other_root = tmp_path / "other"
    other_lay = testing.project(other_root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "other"}}}})
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 60,
          "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    done = {}

    def go():
        done["why"] = _server.run(str(root), cfg, poll_s=0.05)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(0.3)                                                        # let the watchdog actually start the child
    assert plugin.resource_lease(other_lay, "gpu").acquire(wait_s=0) is False   # proof: demo really holds it
    t.join(timeout=10)
    assert done.get("why") == "release_requested"


def test_run_does_not_stop_on_an_expired_release_request_left_by_a_waiter_that_never_came_back(tmp_path, fake_health):
    """F8, the watchdog side: _server.run() checks lease.release_requested() every poll (acquire()'s own first
    call also walks past the stale request and clears it, F8's fix in leases._try_take()). A request left on
    disk by a waiter whose hand_over_timeout_s has already passed before run() even starts must not read as
    live and must not stop the server it is about to start."""
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    lay = testing.project(root, routing={"resources": {"gpu": {
        "lease": {"path": str(lease_file), "label": "demo", "hand_over_timeout_s": 0.2}}}})
    lease_file.parent.mkdir(parents=True, exist_ok=True)
    lease_file.write_text(json.dumps({
        "entries": [], "release_requested_by": "some-other-project-hash",
        "release_requested_label": "other", "release_requested_at": time.time() - 10,   # long past the window
    }), encoding="utf-8")
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 0.3,
          "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    why = _server.run(str(root), cfg, poll_s=0.05)
    assert why == "idle"                                                   # not stopped by the stale request
    assert plugin.resource_lease(lay, "gpu").release_requested() is False


def test_run_returns_lease_unavailable_and_starts_no_child_when_the_lease_is_held_by_another_project(
        tmp_path, fake_health, monkeypatch):
    """F4 (RELEASE_REVIEW_local-lane.md): run() discarded lease.acquire()'s refusal and started the configured
    argv regardless, loading VRAM while holding no lease and while the other project's release request against
    it sat unanswered. The fix must check the return value before spawning anything."""
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    testing.project(root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}}})
    other = plugin.resource_lease(
        testing.project(tmp_path / "other", routing={"resources": {"gpu": {
            "lease": {"path": str(lease_file), "label": "other"}}}}), "gpu")
    assert other.acquire(wait_s=0) is True                                 # the other project holds the cards
    calls = []
    monkeypatch.setattr(_server.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or _FakeProc())
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 0.3,
          "wait_s": 0.2, "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    why = _server.run(str(root), cfg, poll_s=0.05)
    assert why == "lease_unavailable"
    assert calls == []                                                    # no child spawned at all
    assert other.release_requested() is True                              # yielding, not going silent
    state = json.loads(_server.state_path(str(root)).read_text(encoding="utf-8"))
    assert state["pid"] is None
    assert state["why"] == "lease"
    other.release()


def test_run_times_out_and_releases_when_the_server_never_becomes_healthy(tmp_path):
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    lay = testing.project(root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}}})
    cfg = {"base_url": "http://127.0.0.1:1", "health_path": "/health", "start_timeout_s": 0.2, "idle_stop_s": 60,
          "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    why = _server.run(str(root), cfg, poll_s=0.05)
    assert why == "start_timeout"
    assert plugin.resource_lease(lay, "gpu").acquire(wait_s=0) is True   # released: a fresh acquire succeeds at once


# ---------------------------------------------------------------- F5/F14: a hard-killed watchdog leaks no server


def test_a_hard_killed_watchdog_does_not_leave_its_server_running(tmp_path, fake_health):
    """F5 (design risk list: "tested, not assumed"), in the review probe's own shape
    (runs/T5-04/T5-04.a3/probes/test_llm_endpoint_probes.py::test_defect_hard_killed_watchdog_leaves_an_orphan_...),
    now proving the fix instead of the defect: `_start_child_tied_to_this_process` ties the server child to the
    watchdog at the OS level (a Job Object with KILL_ON_JOB_CLOSE here on Windows; a process group plus
    PR_SET_PDEATHSIG on POSIX, proven only by CI's ubuntu legs). The neighbouring tests below prove the
    state-based backstop (F14's shape, the doctor, ensure_started(), llm_endpoint.stop) for whatever race or
    platform that tie misses."""
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 20, "idle_stop_s": 600,
          "argv": [sys.executable, "-c", "import time; time.sleep(120)"]}
    testing.project(root, plugins=["llm_endpoint"], routing={
        "resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}},
        "plugins": {"llm_endpoint": cfg}})
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_WORKTREE), str(_PKG_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    wd = subprocess.Popen([sys.executable, "-m", "supervisor_plugin_llm_endpoint._server", str(root), str(cfg_path)],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        deadline = time.time() + 25
        state = {}
        while time.time() < deadline:
            try:
                state = json.loads(_server.state_path(str(root)).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if state.get("pid"):
                child_pid = state["pid"]
                break
            assert wd.poll() is None, "watchdog exited before becoming ready"
            time.sleep(0.2)
        assert child_pid, "watchdog never wrote the ready state"
        assert state.get("create_time") is not None                    # F14's shape: the child's create_time
        assert state.get("watchdog_pid") is not None                   # ... and the watchdog's own identity
        wd.kill()                                                       # the dirty path: no finally, no cleanup
        wd.wait(timeout=10)
        deadline = time.time() + 10
        alive = True
        while time.time() < deadline:
            alive = psutil.pid_exists(child_pid) and psutil.Process(child_pid).is_running()
            if not alive:
                break
            time.sleep(0.2)
        assert not alive, "the server survived its watchdog's hard kill: the OS-level tie did not hold"
    finally:
        if child_pid and psutil.pid_exists(child_pid):
            try:
                psutil.Process(child_pid).kill()
            except psutil.Error:
                pass
        if wd.poll() is None:
            wd.kill()


def _wait_until_dead(pid: int, timeout: float = 10.0) -> bool:
    """A real orphan's parent (the watchdog) is already dead by the time anything calls `_kill_process()` on
    it, so the kernel has already reparented it to init/the system's subreaper, which reaps on SIGCHLD as a
    matter of course. This fixture's stand-in has no such reaper - `orphaned_server` below starts its "server"
    as pytest's own direct child, and `psutil.Process(pid).kill()` (SIGKILL, called from this same pytest
    process, never the server's parent) leaves a kill with nothing to reap it - so on POSIX it sits as a zombie,
    which `psutil.pid_exists()`/`Process.is_running()` both still report present. A zombie means the kill already
    landed (the process has exited; only its exit status is unclaimed), so it counts as dead here."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running():
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def _dead_or_why_not(pid: int) -> str:
    """A diagnostic for `_wait_until_dead()`'s own failure message, not production code: if the zombie read above
    does not explain a future failure on some other POSIX runner, this names the pid's actual psutil status
    instead of a bare `assert False`."""
    try:
        return f"pid {pid} still present, psutil status={psutil.Process(pid).status()!r}"
    except psutil.NoSuchProcess:
        return f"pid {pid} not present (unexpected: _wait_until_dead should have returned True)"


@pytest.fixture
def orphaned_server():
    """A real sleeping python stands in for a server the risk list worries about; a real, already-exited
    python's pid and create_time stand in for the watchdog that used to own it - the exact state F5 names,
    built without going through `run()`'s own OS-level tie (proven above) so the state-based backstop below
    can be tested deterministically, not raced against a Job Object that usually wins in nanoseconds."""
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    server_create_time = psutil.Process(server.pid).create_time()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_create_time = psutil.Process(dead.pid).create_time()
    dead.wait(timeout=10)
    yield {"pid": server.pid, "create_time": server_create_time,
           "watchdog_pid": dead.pid, "watchdog_create_time": dead_create_time}
    if server.poll() is None:
        server.kill()


def test_doctor_fails_when_the_state_names_a_dead_watchdog_with_a_live_server(tmp_path, fake_health, orphaned_server):
    layout = testing.project(tmp_path, plugins=["llm_endpoint"],
                             routing={"plugins": {"llm_endpoint": {"base_url": fake_health}}})
    _server._write_state(str(tmp_path), orphaned_server)
    check = llm_endpoint.check_llm_endpoint(layout)
    assert not check.ok and "dead watchdog" in check.detail


def test_ensure_started_kills_the_orphan_instead_of_reporting_it_healthy(tmp_path, fake_health, orphaned_server,
                                                                         monkeypatch):
    _server._write_state(str(tmp_path), orphaned_server)
    spawned = []
    monkeypatch.setattr(_server.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)) or _FakeProc())
    cfg = {"base_url": fake_health, "health_path": "/health", "argv": ["demo-server"]}
    assert _server.ensure_started(cfg, str(tmp_path)) is False          # DEFECT at the old head: True
    assert _wait_until_dead(orphaned_server["pid"]), _dead_or_why_not(orphaned_server["pid"])
    assert len(spawned) == 1                                            # recovers: spawns a fresh watchdog


def test_stop_command_kills_the_orphan_directly_when_no_watchdog_is_left_to_read_the_request(tmp_path, orphaned_server):
    layout = testing.project(tmp_path, plugins=["llm_endpoint"], routing={"plugins": {"llm_endpoint": {}}})
    _server._write_state(str(tmp_path), orphaned_server)
    result = llm_endpoint._stop_command(layout, {}, "owner")
    assert result.get("killed_orphan_pid") == orphaned_server["pid"]    # DEFECT at the old head: a no-op file
    assert _wait_until_dead(orphaned_server["pid"]), _dead_or_why_not(orphaned_server["pid"])
    assert not _server.stop_path(str(tmp_path)).exists()
