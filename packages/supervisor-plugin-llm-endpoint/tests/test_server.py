"""The server watchdog (D-013 section 1.2): `ensure_started()` (called from the adapter's submit(), in the
loop's own process) kicks the watchdog off in the background and never blocks on a cold load; `run()` (the
watchdog's own entry point, a separate detached process) acquires the machine-wide GPU lease, starts the
configured argv, waits for health, then watches idle/release-request/child-liveness until one of them says
stop. A fake HTTP server stands in for the real endpoint's health check; a dummy sleeping child stands in for
the real server process (the watchdog only polls health over HTTP and `proc.poll()`, so the two need not be
the same process for this test)."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from supervisor import plugin, testing

from supervisor_plugin_llm_endpoint import _server


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


def test_run_times_out_and_releases_when_the_server_never_becomes_healthy(tmp_path):
    lease_file = tmp_path / "lease" / "gpu.lease"
    root = tmp_path / "proj"
    lay = testing.project(root, routing={"resources": {"gpu": {"lease": {"path": str(lease_file), "label": "demo"}}}})
    cfg = {"base_url": "http://127.0.0.1:1", "health_path": "/health", "start_timeout_s": 0.2, "idle_stop_s": 60,
          "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    why = _server.run(str(root), cfg, poll_s=0.05)
    assert why == "start_timeout"
    assert plugin.resource_lease(lay, "gpu").acquire(wait_s=0) is True   # released: a fresh acquire succeeds at once
