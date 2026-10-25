"""Two read/render defects fixed in 1.10.0. `supervisor status` is a read: it reports the live runtime record and writes
nothing (it used to re-render STATE.md, and always said the loop was not running). Rendering a projection reads the
store and writes the file under one lock, so a slower render holding an older snapshot cannot land after a fresher
one."""
from __future__ import annotations

import json
import threading
import time

import pytest
import yaml
from typer.testing import CliRunner

from supervisor import render
from supervisor.cli import app
from supervisor.locks import FileLock, LockHeld
from tests.conftest import make_task


@pytest.fixture
def store(layout):
    make_task(layout, "A")
    make_task(layout, "B", deps=("A",))
    render.render_all(layout)
    return layout


def test_status_reports_the_runtime_record_and_writes_nothing(store):
    store.runtime.mkdir(parents=True, exist_ok=True)
    (store.runtime / "supervisor.json").write_text(json.dumps(
        {"status": "RUNNING", "pid": 99999, "heartbeat": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in store.root.glob("*.md")}
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "**Supervisor:** RUNNING (pid 99999" in result.output
    assert {p.name: p.read_bytes() for p in store.root.glob("*.md")} == before


def test_status_without_a_runtime_record_says_stopped(store):
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0 and "**Supervisor:** STOPPED" in result.output


def test_a_render_that_starts_later_waits_and_carries_the_fresher_snapshot(store, monkeypatch):
    """The first render is held between its read and its write; a change lands; a second render must not finish
    while the first still holds its older snapshot, and the file left behind reflects the change."""
    started = time.monotonic()
    render.render_state(store)
    window = max(1.5, (time.monotonic() - started) * 5)     # longer than a render takes, so "not done" means blocked

    stalled, release, second_done = threading.Event(), threading.Event(), threading.Event()
    real = render.state_lines

    def stalling(*args, **kwargs):
        lines = real(*args, **kwargs)
        if not stalled.is_set():                            # only the first call stalls
            stalled.set()
            assert release.wait(timeout=20)
        return lines
    monkeypatch.setattr(render, "state_lines", stalling)

    first = threading.Thread(target=lambda: render.render_state(store), daemon=True)
    first.start()
    assert stalled.wait(timeout=20)
    (store.inbox / "H-901.yaml").write_text(yaml.safe_dump(
        {"id": "H-901", "status": "open", "subject": "probe", "for": "owner", "kind": "question",
         "allowed_responses": ["OK"]}, sort_keys=False), encoding="utf-8")

    def second_render():
        render.render_state(store)
        second_done.set()
    second = threading.Thread(target=second_render, daemon=True)
    second.start()
    assert not second_done.wait(window), "a second render finished while the first was between read and write"
    release.set()
    assert second_done.wait(timeout=20)
    first.join(timeout=20)
    line = next(ln for ln in (store.root / "STATE.md").read_text(encoding="utf-8").splitlines() if "Owner inbox:" in ln)
    assert "H-901" in line


@pytest.mark.parametrize("fn,name", [(render.render_tasks, "TASKS.md"), (render.render_issues, "ISSUES.md"),
                                     (render.render_decisions, "DECISIONS.md"), (render.render_inbox, "HUMAN_INBOX.md"),
                                     (render.render_owner_tasks, "OWNER_TASKS.md"), (render.render_state, "STATE.md")])
def test_every_projection_reads_and_writes_under_the_render_lock(store, monkeypatch, fn, name):
    seen = []
    real_write = render._write

    def checking_write(path, lines):
        try:
            FileLock(store.render_lock, owner="probe").acquire().release()
            seen.append("unlocked")
        except LockHeld:
            seen.append("locked")
        return real_write(path, lines)
    monkeypatch.setattr(render, "_write", checking_write)
    assert fn(store).name == name
    assert seen == ["locked"]


def test_rendering_works_while_the_loop_holds_the_state_lock(store):
    """The loop holds state/.lock for its whole run and renders from inside it: the render lock is a different one."""
    lock = FileLock(store.lock, owner="loop").acquire()
    try:
        assert render.render_state(store).exists()
    finally:
        lock.release()


def test_phase_and_gates_passed_matches_the_state_md_phase_line(store):
    from supervisor import tasks as tasks_mod
    phase, passed = render.phase_and_gates_passed(store, tasks_mod.load_all(store))
    line = next(ln for ln in (store.root / "STATE.md").read_text(encoding="utf-8").splitlines() if "**Phase:**" in ln)
    assert line.startswith(f"- **Phase:** {phase} ") and line.endswith(f"gates passed: {', '.join(passed) or 'none'}")
