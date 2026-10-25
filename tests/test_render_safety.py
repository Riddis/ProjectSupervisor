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


# ------------------------------------------------------------------ review 1.10.0 round 5

EXTRA = """
def setup(registry):
    def extra(layout):
        p = layout.root / "EXTRA.md"
        p.write_text("# extra " + (layout.root / "source.txt").read_text(encoding="utf-8"), encoding="utf-8")
        return p
    registry.projection(extra, path="EXTRA.md")
"""


def _with_extension(layout, code, name="extra"):
    from supervisor import extensions, project
    (layout.root / "extensions").mkdir(parents=True, exist_ok=True)
    (layout.root / "extensions" / f"{name}.py").write_text(code, encoding="utf-8")
    doc = dict(project.load(layout))
    doc["name"] = doc.get("name") or "proj"
    doc["extensions"] = [name]
    project.write(layout, doc)
    extensions.reset()


def _tree(root):
    """Every file under the project: its bytes, or its size for a file locked against reading (the held state/.lock)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out[p.relative_to(root).as_posix()] = p.read_bytes()
            except PermissionError:
                out[p.relative_to(root).as_posix()] = ("locked", p.stat().st_size)
    return out


def test_a_cold_status_process_leaves_the_whole_project_unchanged(store):
    """Review 1.10.0 round 5: `supervisor status` in a fresh process, with an extension that declares a projection
    not yet recorded and with state/.lock held by another process, changes no file anywhere in the project."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    (store.root / "source.txt").write_text("v1", encoding="utf-8")
    _with_extension(store, EXTRA)
    assert not (store.runtime / "projection_paths.json").exists()
    store.runtime.mkdir(parents=True, exist_ok=True)
    (store.runtime / "supervisor.json").write_text(json.dumps({"status": "RUNNING", "pid": 4242}), encoding="utf-8")
    lock = FileLock(store.lock, owner="loop").acquire()
    try:
        before = _tree(store.root)
        env = {**os.environ, "SUPERVISOR_ROOT": str(store.root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        out = subprocess.run([sys.executable, "-m", "supervisor", "status"], cwd=str(store.root), env=env,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
        after = _tree(store.root)
    finally:
        lock.release()
    assert out.returncode == 0, out.stderr
    assert "**Supervisor:** RUNNING (pid 4242" in out.stdout
    assert sorted(after) == sorted(before), set(after) ^ set(before)
    assert [k for k in before if before[k] != after[k]] == []


def test_a_registered_projection_is_serialized_like_the_built_in_ones(store, monkeypatch):
    """Review 1.10.0 round 5: two render_all calls overlap on an extension projection; the one that started first
    and read the old source cannot land after the one that read the new source."""
    from supervisor import extensions
    (store.root / "source.txt").write_text("OLD", encoding="utf-8")
    _with_extension(store, EXTRA)
    reg = extensions.load(store)
    [real] = reg.projections
    stalled, release, second_done = threading.Event(), threading.Event(), threading.Event()

    def stalling(layout):
        text = (layout.root / "source.txt").read_text(encoding="utf-8")
        if not stalled.is_set():
            stalled.set()
            assert release.wait(timeout=20)
        p = layout.root / "EXTRA.md"
        p.write_text("# extra " + text, encoding="utf-8")
        return p
    monkeypatch.setattr(reg, "projections", [stalling])
    first = threading.Thread(target=lambda: render.render_all(store), daemon=True)
    first.start()
    assert stalled.wait(timeout=20)
    (store.root / "source.txt").write_text("NEW", encoding="utf-8")

    def second():
        render.render_all(store)
        second_done.set()
    t2 = threading.Thread(target=second, daemon=True)
    t2.start()
    assert not second_done.wait(1.5), "a second render finished while the first projection held an older read"
    release.set()
    assert second_done.wait(timeout=60)
    first.join(timeout=60)
    assert (store.root / "EXTRA.md").read_text(encoding="utf-8") == "# extra NEW"
    assert callable(real)


def test_a_projection_may_call_a_built_in_renderer_without_waiting_on_itself(store):
    """The render lock is reentrant within a thread: a projection that renders STATE.md itself does not block on
    the lock its own call already holds."""
    import time
    _with_extension(store, """
def setup(registry):
    def combined(layout):
        from supervisor import render
        return render.render_state(layout)
    registry.projection(combined, path="STATE.md")
""", name="combined")
    started = time.monotonic()
    out = render.render_all(store)
    assert time.monotonic() - started < 10                                   # well under the 30 s lock wait
    assert sum(1 for p in out if p.name == "STATE.md") == 2
    log = store.log.read_text(encoding="utf-8") if store.log.exists() else ""
    assert '"projection_error"' not in log


# ------------------------------------------------------------------ review 1.10.0 round 6: however the CLI is invoked

CLI_EXT = """
import typer
cli = typer.Typer()

@cli.command("hello")
def hello():
    typer.echo("hello from the extension")

def setup(registry):
    def extra(layout):
        p = layout.root / "EXTRA.md"
        p.write_text("# extra", encoding="utf-8")
        return p
    registry.projection(extra, path="EXTRA.md")
    registry.cli(cli, "echo")
"""


@pytest.fixture
def cli_store(store, monkeypatch):
    from supervisor import cli
    _with_extension(store, CLI_EXT)
    monkeypatch.setattr(cli, "_extensions_cli", {}, raising=False)     # a fresh process: nothing built yet
    store.runtime.mkdir(parents=True, exist_ok=True)
    (store.runtime / "supervisor.json").write_text(json.dumps({"status": "RUNNING", "pid": 4242}), encoding="utf-8")
    return store


@pytest.mark.parametrize("how", ["app_call", "cli_runner"])
def test_status_sets_up_no_extension_however_the_cli_is_invoked(cli_store, capsys, how):
    """Review 1.10.0 round 6: importing the CLI and calling `app(args=["status"])` (or a test runner) used to load
    the extensions at import and write their projection record while state/.lock was held."""
    from supervisor import cli, extensions
    lock = FileLock(cli_store.lock, owner="loop").acquire()
    try:
        before = _tree(cli_store.root)
        if how == "app_call":
            with pytest.raises(SystemExit) as exit_:
                cli.app(args=["status"])
            assert exit_.value.code in (0, None)
            output = capsys.readouterr().out
        else:
            result = CliRunner().invoke(cli.app, ["status"])
            assert result.exit_code == 0, result.output
            output = result.output
        after = _tree(cli_store.root)
    finally:
        lock.release()
    assert "**Supervisor:** RUNNING (pid 4242" in output
    assert after == before
    assert extensions.loaded(cli_store.root) is None and not cli._extensions_cli


def test_an_extension_command_group_is_found_when_asked_for(cli_store):
    from supervisor import cli
    result = CliRunner().invoke(cli.app, ["echo", "hello"])
    assert result.exit_code == 0, result.output
    assert "hello from the extension" in result.output
    assert "echo" in CliRunner().invoke(cli.app, ["--help"]).output


def test_importing_the_cli_and_calling_app_status_in_a_fresh_process_changes_nothing(cli_store):
    """The reviewer's probe: a new process imports the CLI (which used to discover extensions at import, keyed on
    sys.argv) and calls `app(args=["status"])` with state/.lock held."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    lock = FileLock(cli_store.lock, owner="loop").acquire()
    try:
        before = _tree(cli_store.root)
        env = {**os.environ, "SUPERVISOR_ROOT": str(cli_store.root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        out = subprocess.run([sys.executable, "-c", "from supervisor.cli import app; app(args=['status'])"],
                             cwd=str(cli_store.root), env=env, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
        after = _tree(cli_store.root)
    finally:
        lock.release()
    assert out.returncode == 0, out.stderr
    assert "**Supervisor:** RUNNING (pid 4242" in out.stdout
    assert sorted(after) == sorted(before), set(after) ^ set(before)
    assert [k for k in before if before[k] != after[k]] == []


# ------------------------------------------------------------------ review 1.10.0 round 7

def _cli_project(root, ext_name, command):
    """A minimal project at `root` whose one extension registers the command group `command`."""
    from supervisor import project
    from supervisor.paths import Layout
    layout = Layout(root).ensure()
    (root / "extensions").mkdir(parents=True, exist_ok=True)
    (root / "extensions" / f"{ext_name}.py").write_text(f"""
import typer
cli = typer.Typer()

@cli.command("hello")
def hello():
    typer.echo("hello from {command}")

def setup(registry):
    registry.cli(cli, "{command}")
""", encoding="utf-8")
    project.write(layout, {"name": ext_name, "framework_version": "1.10.0", "extensions": [ext_name]})
    return layout


def test_switching_projects_switches_their_extension_commands(tmp_path, monkeypatch):
    """Review 1.10.0 round 7: one process, two projects; the second must not keep the first one's command groups."""
    from supervisor import cli, extensions
    monkeypatch.setattr(cli, "_extensions_cli", {})
    a = _cli_project(tmp_path / "a", "alpha_ext", "alpha")
    b = _cli_project(tmp_path / "b", "beta_ext", "beta")
    extensions.reset()
    runner = CliRunner()
    monkeypatch.setenv("SUPERVISOR_ROOT", str(a.root))
    assert "hello from alpha" in runner.invoke(cli.app, ["alpha", "hello"]).output
    monkeypatch.setenv("SUPERVISOR_ROOT", str(b.root))
    assert runner.invoke(cli.app, ["alpha", "hello"]).exit_code != 0             # A's group is not B's
    assert "hello from beta" in runner.invoke(cli.app, ["beta", "hello"]).output
    help_b = runner.invoke(cli.app, ["--help"]).output
    assert "beta" in help_b and "alpha" not in help_b


def test_a_reloaded_registry_rebuilds_the_extension_commands(tmp_path, monkeypatch):
    from supervisor import cli, extensions
    monkeypatch.setattr(cli, "_extensions_cli", {})
    a = _cli_project(tmp_path / "a", "alpha_ext", "alpha")
    extensions.reset()
    monkeypatch.setenv("SUPERVISOR_ROOT", str(a.root))
    runner = CliRunner()
    assert "hello from alpha" in runner.invoke(cli.app, ["alpha", "hello"]).output
    (a.root / "extensions" / "alpha_ext.py").write_text(
        (a.root / "extensions" / "alpha_ext.py").read_text(encoding="utf-8").replace('"alpha")', '"gamma")'),
        encoding="utf-8")
    extensions.reset()                                                          # the registry is reloaded
    assert "hello from alpha" in runner.invoke(cli.app, ["gamma", "hello"]).output
    assert runner.invoke(cli.app, ["alpha", "hello"]).exit_code != 0


def test_running_the_cli_module_directly_shows_help(store):
    """Review 1.10.0 round 7: `python -m supervisor.cli --help` ran main() before the extension lookup was defined."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {**os.environ, "SUPERVISOR_ROOT": str(store.root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    out = subprocess.run([sys.executable, "-m", "supervisor.cli", "--help"], cwd=str(store.root), env=env,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr
    assert "status" in out.stdout and "Traceback" not in out.stderr
