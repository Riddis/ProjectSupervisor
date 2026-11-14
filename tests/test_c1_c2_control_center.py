"""C-1/C-2: the Control Center re-parses the whole task store and the whole job directory on every expensive page,
continuously on the Overview's own 5s poll; the page-wide refresh never applies while that poll is in flight; the
Agents "recent" list sorts by file name, not by when a job actually finished; a running job's transcript starts
empty. Reuses the scale fixture from tests/test_r09_turn_snapshot.py (350 tasks, 1,400 job records, built by the
test itself — no project data)."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from supervisor import jobs as jobs_mod
from supervisor import store
from supervisor import tasks as tasks_mod
from supervisor.ui.app import create_app
from supervisor.ui.sse import tail_file
from tests.test_r09_turn_snapshot import _build_scale_fixture

WARM_BUDGET_S = 2.0          # four pages, warm cache; generous, matching R-09's own bound style


@pytest.fixture
def scale_layout(layout):
    _build_scale_fixture(layout)
    return layout


def _read_counts(monkeypatch):
    """Every real parse `_scan_disk`/`_cached_task` makes (`store.read`) and every real job-record read
    `_cached_read` makes (`jobs.read_json`) — zero of either on a warm cache is the whole point of C-1's fix."""
    counts = {"store_read": 0, "job_read": 0}
    real_store_read = store.read
    real_job_read = jobs_mod.read_json

    def wrapped_store_read(p, *a, **k):
        counts["store_read"] += 1
        return real_store_read(p, *a, **k)

    def wrapped_job_read(p, *a, **k):
        counts["job_read"] += 1
        return real_job_read(p, *a, **k)

    monkeypatch.setattr(store, "read", wrapped_store_read)
    monkeypatch.setattr(jobs_mod, "read_json", wrapped_job_read)
    return counts


def test_control_center_pages_are_bounded_on_a_warm_cache(scale_layout, monkeypatch):
    layout = scale_layout
    client = TestClient(create_app(layout, update_check=False), base_url="http://127.0.0.1")
    urls = ("/overview", "/overview/live", "/tasks", "/roadmap")

    counts = _read_counts(monkeypatch)
    for url in urls:                                             # cold: warms every cache
        assert client.get(url).status_code == 200
    cold_store, cold_job = counts["store_read"], counts["job_read"]
    assert cold_store > 0 and cold_job > 0, "the cold round never touched the store at all"

    counts["store_read"] = counts["job_read"] = 0
    started = time.perf_counter()
    for url in urls:
        assert client.get(url).status_code == 200
    elapsed = time.perf_counter() - started

    assert counts["store_read"] == 0, (
        f"a warm {','.join(urls)} round re-parsed {counts['store_read']} task file(s); expected the cache to serve all of them")
    assert counts["job_read"] == 0, (
        f"a warm {','.join(urls)} round re-read {counts['job_read']} job file(s); expected the cache to serve all of them")
    assert elapsed < WARM_BUDGET_S, f"{urls} took {elapsed:.2f}s warm, expected under {WARM_BUDGET_S}s"


def test_a_changed_task_file_is_seen_on_the_next_request(scale_layout):
    layout = scale_layout
    client = TestClient(create_app(layout, update_check=False), base_url="http://127.0.0.1")
    r = client.get("/tasks")                                      # warms the cache
    assert "scale task 1" in r.text

    doc = store.read(layout.tasks / "S-0001.yaml")
    doc["title"] = "renamed after the cache warmed"
    store.write_text(layout.tasks / "S-0001.yaml", store.dump(doc))   # a new mtime/size: the cache must not serve the old title

    r = client.get("/tasks")
    assert "renamed after the cache warmed" in r.text


# ---------------------------------------------------------------------- Agents: "Recent" ordered by finish time


def test_agents_recent_is_ordered_by_finish_time_not_file_name(layout):
    """A record whose file name sorts late can still have finished first, and vice versa — an import or a
    repair can leave any attempt number on any task. `/agents` must show the ones that actually finished last."""
    # T9-aa finishes LAST (latest `finished`) but its file name sorts before T9-zz, which finished first.
    for tid, finished in (("T9-aa", "2026-01-01T00:00:30+00:00"), ("T9-zz", "2026-01-01T00:00:00+00:00")):
        jobs_mod.prepare(layout, tid, f"{tid}.a1", "fake_lane", {"adapter": "fake_lane"})
        jobs_mod.set_phase(layout, f"{tid}.a1", "DONE", result="COMPLETE", finished=finished)

    client = TestClient(create_app(layout, update_check=False), base_url="http://127.0.0.1")
    r = client.get("/agents")
    assert r.status_code == 200
    pos_aa, pos_zz = r.text.find("T9-aa"), r.text.find("T9-zz")
    assert pos_aa != -1 and pos_zz != -1
    assert pos_aa < pos_zz, "T9-aa finished last and must be listed above T9-zz, which finished first"


# ---------------------------------------------------------------------- Agent transcript: replay + split lines


class _FakeRequest:
    def __init__(self, disconnect_after: int = 20):
        self.polls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self):
        self.polls += 1
        return self.polls > self._disconnect_after


def test_a_running_jobs_stream_replays_the_lines_already_written(layout):
    """Before the fix, a running job's stream starts at the end of the file (`from_end=True`): nothing already on
    disk is ever shown. The route must replay it, same as a finished job's first load."""
    run_dir = layout.runs / "T9-90" / "T9-90.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout = run_dir / "stdout.jsonl"
    stdout.write_text('{"type": "assistant", "text": "written before the stream opened"}\n', encoding="utf-8")
    jobs_mod.prepare(layout, "T9-90", "T9-90.a1", "process", {"run_dir": str(run_dir)})
    jobs_mod.set_phase(layout, "T9-90.a1", "RUNNING")

    from supervisor.ui.routes.agents import agent_stream

    async def drive():
        resp = await agent_stream("T9-90.a1", _FakeRequest(disconnect_after=1), layout)
        return [f async for f in resp.body_iterator]

    frames = asyncio.run(drive())
    assert any("written before the stream opened" in f for f in frames), (
        "a running job's transcript stream started empty instead of replaying what was already on disk")


def test_a_line_split_across_two_writes_arrives_whole():
    """tail_file() is only ever advanced by its caller's `__anext__()` (an async generator does not run in the
    background), so the write in between must happen while a poll is deliberately left pending — otherwise both
    writes land before tail_file ever looks, and the cross-read buffering this proves is never exercised."""
    import tempfile

    async def drive(path: Path):
        frames = []
        req = _FakeRequest(disconnect_after=1000)
        agen = tail_file(req, path, poll_s=0.01, from_end=True)
        frames.append(await agen.__anext__())                                     # "event: ready"

        path.write_text('{"type": "assistant", "text": "hel', encoding="utf-8")   # a partial write ...
        pending = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)                                                 # several polls over just that
        if pending.done():                                                        # unfixed: already (wrongly) yielded
            frames.append(pending.result())
            pending = asyncio.ensure_future(agen.__anext__())

        with path.open("a", encoding="utf-8") as fh:                              # ... finished later
            fh.write('lo world"}\n')
        frames.append(await asyncio.wait_for(pending, timeout=2.0))
        return frames

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stdout.jsonl"
        path.write_text("", encoding="utf-8")
        frames = asyncio.run(drive(path))

    appended = [f for f in frames if not f.startswith("event: ready")]
    assert len(appended) == 1, f"a line split across two writes must arrive as exactly one frame, got {len(appended)}: {appended}"
    assert "hello world" in appended[0]


# ---------------------------------------------------------------------- live.js: the refresh decision (needs node)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH (no JS runtime to execute live.js)")
def test_page_refresh_applies_while_only_the_pages_own_poll_is_in_flight():
    """Runs the real live.js against a hand-written DOM/fetch stub (tests/js/live_dom_stub.js - no npm, no jsdom):
    the page's own 5s background poll of #now-running is the only thing in flight, nothing is dirty or selected,
    and a new fingerprint is waiting. The refresh must still apply (C-2); before the fix any ".htmx-request"
    anywhere, including the page's own poll, counted as "the owner is busy" and the page stayed on "waiting"."""
    script = Path(__file__).parent / "js" / "live_poll_refresh_test.js"
    static_live_js = Path(__file__).resolve().parents[1] / "supervisor" / "ui" / "static" / "live.js"
    result = subprocess.run(["node", str(script), str(static_live_js)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
