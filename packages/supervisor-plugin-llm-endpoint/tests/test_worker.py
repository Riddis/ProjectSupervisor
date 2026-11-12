"""The worker subprocess (`_worker.py`): one streaming chat-completions request, `result.json` written at the
end. A fake loopback HTTP server stands in for the real endpoint and serves whatever SSE lines a test configures
(RELEASE_REVIEW_local-lane.md F6: `_stream()` must read `finish_reason` and notice a stream that never reaches
`[DONE]`, instead of reporting COMPLETE on a truncated or broken one)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from supervisor_plugin_llm_endpoint import _worker


class _Fake(BaseHTTPRequestHandler):
    """POST /v1/chat/completions -> exactly the configured SSE lines (no `[DONE]` unless a case adds one)."""
    sse_lines: list[str] = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in _Fake.sse_lines:
            self.wfile.write((line + "\n\n").encode("utf-8"))
            self.wfile.flush()

    def log_message(self, *a):
        pass


@pytest.fixture
def fake():
    _Fake.sse_lines = []
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _run(tmp_path, fake_url, sse_lines, **extra):
    _Fake.sse_lines = sse_lines
    req = {"base_url": fake_url, "model": "demo", "prompt": "p", "schema": None, "max_tokens": 8, "timeout_s": 10,
           "run_root": str(tmp_path), "output_file": "out.md", "activity_file": str(tmp_path / "act")}
    req.update(extra)
    (tmp_path / "req.json").write_text(json.dumps(req), encoding="utf-8")
    assert _worker.main(str(tmp_path / "req.json"), str(tmp_path / "res.json")) == 0
    return json.loads((tmp_path / "res.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- F6: a truncated, errored or unterminated stream


def test_a_length_truncated_stream_fails_the_attempt_naming_max_tokens(tmp_path, fake):
    """At this head the worker never reads finish_reason: an answer cut at max_tokens is written to output_file
    and reported COMPLETE with no trace (RELEASE_REVIEW_local-lane.md F6). It must FAILED instead, naming
    max_tokens, and never write the truncated text as if it were the deliverable."""
    res = _run(tmp_path, fake, [
        'data: {"choices":[{"delta":{"content":"The summary is"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" that the"},"finish_reason":"length"}]}',
        'data: [DONE]'])
    assert res["status"] == "FAILED"
    assert "max_tokens" in res["error"] and "8" in res["error"]
    assert not (tmp_path / "out.md").exists()


def test_a_mid_stream_error_event_fails_the_attempt_with_the_servers_message(tmp_path, fake):
    """A chunk with no `choices` (every runtime's in-stream error shape) is skipped silently at this head, so
    the partial text before the error reads as the completed answer. It must FAILED with the server's message."""
    res = _run(tmp_path, fake, [
        'data: {"choices":[{"delta":{"content":"partial"}}]}',
        'data: {"error":{"code":500,"message":"slot unavailable"}}'])
    assert res["status"] == "FAILED"
    assert "slot unavailable" in res["error"]
    assert not (tmp_path / "out.md").exists()


def test_a_stream_that_never_sends_done_fails_the_attempt(tmp_path, fake):
    """A stream whose body ends (the connection closes) without ever sending `[DONE]` ends like a normal one at
    this head. It must FAILED, naming that the stream never completed."""
    res = _run(tmp_path, fake, ['data: {"choices":[{"delta":{"content":"partial"}}]}'])
    assert res["status"] == "FAILED"
    assert "DONE" in res["error"]
    assert not (tmp_path / "out.md").exists()
