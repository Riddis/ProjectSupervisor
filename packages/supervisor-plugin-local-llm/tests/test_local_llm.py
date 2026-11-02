"""Behaviour of the Ollama lane, against a temporary project (supervisor.testing is unstable: no compatibility
promise across minor framework versions). Ported from a project-local extension's tests, onto supervisor.plugin
and supervisor.testing only - no framework internal."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from supervisor import plugin, testing

import supervisor_plugin_local_llm as local_llm

SCHEMA = {"type": "object", "properties": {"caption": {"type": "string"}}, "required": ["caption"]}


class FakeOllama(BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length))
        if FakeOllama.mode == "ok":
            body = {"response": json.dumps({"caption": "a red fox"}), "done": True}
        else:
            body = {"response": json.dumps({"wrong_key": "nope"}), "done": True}
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def lltask(layout, base_url, tid="LL", mode="ok", vram_mb=0):
    FakeOllama.mode = mode
    testing.make_task(layout, tid, job={"adapter": "local_llm", "base_url": base_url, "model": "pixnote:4b",
                                        "prompt": "describe", "schema": SCHEMA, "vram_mb": vram_mb, "timeout_s": 30},
                      cost_category="local", artifacts=[])


def run_until_done(layout, tid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup = testing.drive(layout, iterations=1)
        recs = testing.jobs(layout, tid)
        if recs and recs[-1]["phase"] in ("DONE", "FAILED"):
            return sup
        time.sleep(0.1)
    raise AssertionError("job never finished")


def test_schema_valid_response_completes(layout, fake_server):
    lltask(layout, fake_server, mode="ok")
    run_until_done(layout, "LL")
    t = testing.task(layout, "LL")
    assert t.status == "COMPLETE"
    run_dir = layout.runs / "LL" / "LL.a1"
    result = json.loads((run_dir / "result.json").read_text())
    assert result["status"] == "COMPLETE" and result["answer"]["caption"] == "a red fox"


def test_schema_invalid_response_fails(layout, fake_server):
    lltask(layout, fake_server, mode="bad")
    run_until_done(layout, "LL")
    run_dir = layout.runs / "LL" / "LL.a1"
    result = json.loads((run_dir / "result.json").read_text())
    assert result["status"] == "FAILED" and "schema validation failed" in result["error"]


def test_vram_preflight_defers(layout, fake_server, monkeypatch):
    lltask(layout, fake_server, vram_mb=4000)
    spec = local_llm.ADAPTER.prepare(layout, testing.task(layout, "LL"), "LL.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda: 500)
    with pytest.raises(plugin.CapacityDeferred):
        local_llm.ADAPTER.submit(spec)
