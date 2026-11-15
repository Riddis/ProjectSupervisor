"""Regression: a log line or an agent transcript line that carries markup must reach the browser as literal text,
not be parsed as HTML. The SSE routes build each frame from unescaped file content, and the bundled htmx evaluates
`<script>` tags in swapped content by default, so a note or a tool's stdout line can carry working script."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from supervisor import jobs as jobs_mod
from supervisor import store
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from supervisor.ui.sse import tail_file

CSRF = "test-csrf-value"

RAW_MARKUP = '<img src=x onerror=window.__probe_ran=1><b>bold</b>'
ESCAPED_MARKUP = '&lt;img src=x onerror=window.__probe_ran=1&gt;&lt;b&gt;bold&lt;/b&gt;'


@pytest.fixture
def app(layout):
    return create_app(layout)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def auth_client(layout, client):
    client.cookies.set(CSRF_COOKIE, CSRF)
    return client


class FakeRequest:
    """Mirrors the pattern in test_ui.py's agent-stream test: the TestClient buffers an open-ended SSE response to
    completion, so the generator is driven directly and told to disconnect after a bounded number of polls."""

    def __init__(self):
        self.polls = 0

    async def is_disconnected(self):
        self.polls += 1
        return self.polls > 20


def _frames_after_appending(path, write_payload) -> list[str]:
    async def drive():
        frames = []
        req = FakeRequest()
        async for frame in tail_file(req, path, poll_s=0.01, from_end=True):
            frames.append(frame)
            if frame.startswith("event: ready"):
                write_payload()
            if len(frames) > 1 and not frames[-1].startswith("event: ready"):
                break
        return frames
    return asyncio.run(drive())


def test_a_streamed_log_line_with_markup_reaches_the_browser_as_text(layout):
    store.append_line(layout.log, json.dumps({"ts": store.now_iso(), "actor": "owner", "event": "note"}))

    def append():
        store.append_line(layout.log, json.dumps({"ts": store.now_iso(), "actor": "owner", "event": "note",
                                                   "note": RAW_MARKUP}))

    frames = _frames_after_appending(layout.log, append)
    appended = [f for f in frames if not f.startswith("event: ready")]
    assert appended, "no frame arrived for the appended line"
    assert all(RAW_MARKUP not in f for f in appended)
    assert any(ESCAPED_MARKUP in f for f in appended)


def test_a_streamed_transcript_line_with_markup_reaches_the_browser_as_text(layout):
    run_dir = layout.runs / "T0-90" / "T0-90.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout = run_dir / "stdout.jsonl"
    stdout.write_text("")
    jobs_mod.prepare(layout, "T0-90", "T0-90.a1", "process", {"run_dir": str(run_dir)})
    jobs_mod.set_phase(layout, "T0-90.a1", "DONE", result="COMPLETE", finished="now")

    def append():
        stdout.write_text(json.dumps({"type": "assistant", "text": RAW_MARKUP}) + "\n")

    frames = _frames_after_appending(stdout, append)
    appended = [f for f in frames if not f.startswith("event: ready")]
    assert appended, "no frame arrived for the appended line"
    assert all(RAW_MARKUP not in f for f in appended)
    assert any(ESCAPED_MARKUP in f for f in appended)


def test_log_page_and_agent_page_carry_a_strict_script_policy(layout, auth_client):
    run_dir = layout.runs / "T0-91" / "T0-91.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stdout.jsonl").write_text("")
    jobs_mod.prepare(layout, "T0-91", "T0-91.a1", "process", {"run_dir": str(run_dir)})
    jobs_mod.set_phase(layout, "T0-91.a1", "DONE", result="COMPLETE", finished="now")

    for url in ("/log", "/agents/T0-91.a1"):
        r = auth_client.get(url)
        assert r.status_code == 200
        csp = r.headers.get("content-security-policy", "")
        assert csp, f"{url} carries no Content-Security-Policy header"
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "unsafe-eval" not in csp.split("script-src", 1)[1].split(";", 1)[0]
