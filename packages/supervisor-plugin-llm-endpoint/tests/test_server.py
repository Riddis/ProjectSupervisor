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


# ---------------------------------------------------------------- F18: the watchdog finishes the in-flight
# request before yielding (RELEASE_REVIEW_local-lane_round2.md)


def _wait_for_ready_pid(root, timeout=10.0):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        try:
            state = json.loads(_server.state_path(str(root)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("pid"):
            return state["pid"]
        time.sleep(0.05)
    return None


def test_run_finishes_the_in_flight_request_before_yielding_to_a_release_request(tmp_path, fake_health):
    """F18 (RELEASE_REVIEW_local-lane_round2.md): R4's fairness clause is binding and specific - "this lane's
    watchdog FINISHES THE IN-FLIGHT REQUEST, stops the server and releases" (LOCAL_MODEL_LANE.md:267-270) - but
    at 50660a07becd2e35a4d20073d13b93b96eecb280 `run()` returns `release_requested` on the first poll that sees
    the request (_server.py:336-337), with no look at the activity file - the worker's own in-flight signal,
    touched on every streamed piece (_worker.py:85-90). Here a thread stands in for the worker, touching the
    activity file every 25 ms while B's project asks to take the lease; the fix must keep the child alive while
    the activity keeps coming and only yield once it goes quiet. Asserts that the child OUTLIVED the first
    poll after the request landed, not merely the eventual return value - the defect kills the child within one
    poll regardless of the activity file, so this fails at the pre-fix head even though `why` is still
    "release_requested" either way."""
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    testing.project(root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}}})
    other_lay = testing.project(tmp_path / "other", routing={"resources": {"gpu": {
        "lease": {"path": str(lease_file), "label": "other"}}}})
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 60,
          "hand_over_grace_s": 0.5, "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    out = {}

    def go():
        out["why"] = _server.run(str(root), cfg, poll_s=0.05)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    child_pid = _wait_for_ready_pid(root)
    assert child_pid, "watchdog never became ready"

    stop_streaming = threading.Event()

    def stream():                                              # the worker mid-generation: touches on every piece
        while not stop_streaming.is_set():
            _server.touch_activity(str(root))
            time.sleep(0.025)
    w = threading.Thread(target=stream, daemon=True)
    w.start()
    time.sleep(0.1)
    assert plugin.resource_lease(other_lay, "gpu").acquire(wait_s=0) is False   # B's request lands mid-stream
    time.sleep(0.2)                                             # a couple of the watchdog's own polls
    assert psutil.pid_exists(child_pid) and psutil.Process(child_pid).is_running(), (
        "the watchdog killed the in-flight request's server instead of finishing it (F18): "
        "the child did not outlive the first poll after the request landed")
    stop_streaming.set()
    w.join(timeout=2)
    t.join(timeout=10)
    assert out.get("why") == "release_requested"
    assert _wait_until_dead(child_pid), _dead_or_why_not(child_pid)   # finished, then stopped - not leaked


def test_run_does_not_hold_the_cards_past_hand_over_timeout_s_when_the_request_never_goes_quiet(tmp_path, fake_health):
    """The companion bound (F18's fix shape): a wedged generation that never goes quiet must not hold the lease
    past the request's own `hand_over_timeout_s` - R4's own "until that project has had the lease or
    hand_over_timeout_s has passed" (LOCAL_MODEL_LANE.md:270-271). `hand_over_grace_s` is set far larger than
    `hand_over_timeout_s` here so only the timeout bound, not the grace, can be what stops the server."""
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    testing.project(root, routing={"resources": {"gpu": {"lease": {
        "path": str(lease_file), "label": "demo", "hand_over_timeout_s": 0.6}}}})
    other_lay = testing.project(tmp_path / "other", routing={"resources": {"gpu": {
        "lease": {"path": str(lease_file), "label": "other"}}}})
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 60,
          "hand_over_grace_s": 10.0, "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    out = {}

    def go():
        out["why"] = _server.run(str(root), cfg, poll_s=0.05)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    assert _wait_for_ready_pid(root), "watchdog never became ready"
    stop_streaming = threading.Event()

    def stream():
        while not stop_streaming.is_set():
            _server.touch_activity(str(root))
            time.sleep(0.025)
    w = threading.Thread(target=stream, daemon=True)
    w.start()
    assert plugin.resource_lease(other_lay, "gpu").acquire(wait_s=0) is False
    asked_at = time.time()
    t.join(timeout=10)
    stop_streaming.set()
    w.join(timeout=2)
    assert out.get("why") == "release_requested"
    elapsed = time.time() - asked_at
    assert 0.6 <= elapsed < 5.0, elapsed   # held at least the window, but never past it by more than a poll or two


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


@pytest.mark.skipif(sys.platform == "win32", reason="zombies are a POSIX-only process state")
def test_process_alive_reads_a_zombie_as_not_alive():
    """The production half of F5's same question: `_process_alive()` (used by both the doctor and
    `dead_watchdog_with_live_server()`) must not read a zombie - exited, awaiting its parent's reap - as a live
    server, the same gap `_wait_until_dead()` above works around for this file's own stand-in fixture."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    create_time = psutil.Process(child.pid).create_time()
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:  # the child exits on its own; nothing reaps it, so it zombies meanwhile
        status = psutil.Process(child.pid).status()
        if status == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.05)
    assert status == psutil.STATUS_ZOMBIE, f"the child never reached zombie state (last status={status!r})"
    assert _server._process_alive(child.pid, create_time) is False
    child.wait(timeout=10)  # reap, so no zombie is left behind for the test runner to clean up


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


# ---------------------------------------------------------------- F22 (P3, advisory): a failed
# AssignProcessToJobObject is silent (RELEASE_REVIEW_local-lane_round2.md)


@pytest.mark.skipif(sys.platform != "win32", reason="AssignProcessToJobObject is Windows-only")
def test_a_failed_job_object_tie_is_logged_and_recorded_as_not_tied(tmp_path, fake_health, monkeypatch):
    """F22: `_start_child_tied_to_this_process` used to discard `AssignProcessToJobObject`'s own BOOL, so a
    host where the tie fails (a watchdog already inside a job that forbids nesting or breakaway) left the
    child simply untied with nothing said. The call is made to fail here instead of needing such a host."""
    def _fail_assign(*a):
        _server.ctypes.set_last_error(5)   # ERROR_ACCESS_DENIED: a plausible real failure, injected here
        return 0
    monkeypatch.setattr(_server._kernel32, "AssignProcessToJobObject", _fail_assign)
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    cfg = {"base_url": fake_health, "health_path": "/health", "start_timeout_s": 5, "idle_stop_s": 1,
          "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    layout = testing.project(root, plugins=["llm_endpoint"], routing={
        "resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}},
        "plugins": {"llm_endpoint": cfg}})
    # Deliberately not primed: run() itself must publish supervisor.plugins' registry (via _hand_over_timeout_s()
    # -> plugin.resources()) before calling log_event, the same as the real detached watchdog process, which has
    # no equivalent of a priming call (round 2 review: this test used to pass only by priming by hand).
    out = {}

    def go():
        out["why"] = _server.run(str(root), cfg, poll_s=0.05)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    assert _wait_for_ready_pid(root), "watchdog never became ready"
    state = json.loads(_server.state_path(str(root)).read_text(encoding="utf-8"))
    assert state["tied"] is False
    check = llm_endpoint.check_llm_endpoint(layout)
    assert check.ok and "NOT tied" in check.detail
    t.join(timeout=10)
    assert out.get("why") == "idle"
    lines = [json.loads(l) for l in layout.log.read_text(encoding="utf-8").splitlines() if l.strip()]
    tie_lines = [l for l in lines if l.get("event") == "llm_endpoint_server_job_object_tie_failed"]
    assert len(tie_lines) == 1
    assert tie_lines[0]["win32_error"] != 0
