"""The llm_endpoint doctor check and its owner commands (D-013 section 6: "a doctor check (endpoint reachable,
the configured model present at /v1/models, the server's state file consistent with the process table)";
owner commands llm_endpoint.start and llm_endpoint.stop)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from supervisor import testing

import supervisor_plugin_llm_endpoint as llm_endpoint
from supervisor_plugin_llm_endpoint import _server


class _Models(BaseHTTPRequestHandler):
    models = ["demo-model"]
    health = True

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"data": [{"id": m} for m in _Models.models]}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200 if _Models.health else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_endpoint():
    _Models.models, _Models.health = ["demo-model"], True
    srv = HTTPServer(("127.0.0.1", 0), _Models)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _layout(tmp_path, **cfg):
    return testing.project(tmp_path, plugins=["llm_endpoint"], routing={"plugins": {"llm_endpoint": cfg}})


def test_unreachable_with_no_argv_configured_fails(tmp_path):
    layout = _layout(tmp_path, base_url="http://127.0.0.1:1")
    check = llm_endpoint.check_llm_endpoint(layout)
    assert not check.ok and "unreachable" in check.detail


def test_unreachable_with_argv_configured_warns_rather_than_fails(tmp_path):
    layout = _layout(tmp_path, base_url="http://127.0.0.1:1", argv=["demo-server"])
    check = llm_endpoint.check_llm_endpoint(layout)
    assert check.ok and check.warn


def test_reachable_with_the_configured_model_present_passes(tmp_path, fake_endpoint):
    layout = _layout(tmp_path, base_url=fake_endpoint, model="demo-model")
    check = llm_endpoint.check_llm_endpoint(layout)
    assert check.ok and not check.warn


def test_reachable_but_the_configured_model_is_missing_fails(tmp_path, fake_endpoint):
    layout = _layout(tmp_path, base_url=fake_endpoint, model="other-model")
    check = llm_endpoint.check_llm_endpoint(layout)
    assert not check.ok and "other-model" in check.detail


def test_a_dangling_state_file_with_no_live_process_is_flagged(tmp_path, fake_endpoint):
    layout = _layout(tmp_path, base_url=fake_endpoint)
    _server._write_state(str(layout.root), {"pid": 999999, "base_url": fake_endpoint})
    check = llm_endpoint.check_llm_endpoint(layout)
    assert not check.ok and "state file" in check.detail


def test_start_command_calls_ensure_started(tmp_path, fake_endpoint, monkeypatch):
    layout = _layout(tmp_path, base_url=fake_endpoint)
    calls = []
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: calls.append((cfg, root)) or True)
    result = llm_endpoint._start_command(layout, {}, "owner")
    assert result["status"] == "ok" and result["ready"] is True
    assert len(calls) == 1


def test_stop_command_writes_a_stop_request(tmp_path):
    layout = _layout(tmp_path)
    result = llm_endpoint._stop_command(layout, {}, "owner")
    assert result["status"] == "ok"
    assert _server.stop_path(str(layout.root)).exists()


def test_a_lease_refusal_state_does_not_read_as_healthy_and_the_owner_command_does_not_appear_to_succeed(
        tmp_path, monkeypatch):
    """F4's fix: the state _server.run() writes on a refused lease acquire carries pid: None and why: "lease".
    No server was ever started, so the endpoint stays unreachable - the doctor must not read the state file as
    a healthy server, and llm_endpoint.start (ensure_started(), which only spawns a watchdog in the background
    and never knows the lease's outcome itself) must still report ready: False, never an apparent success."""
    layout = _layout(tmp_path, base_url="http://127.0.0.1:1", argv=["demo-server"])
    _server._write_state(str(tmp_path), {"pid": None, "stopped": 1.0, "why": "lease"})
    check = llm_endpoint.check_llm_endpoint(layout)
    assert check.ok and check.warn and "healthy" not in check.detail
    monkeypatch.setattr(_server.subprocess, "Popen", lambda *a, **k: None)
    result = llm_endpoint._start_command(layout, {}, "owner")
    assert result == {"status": "ok", "ready": False}
