"""Framework update check/apply against a local bare `origin`, with `framework_dir()` pointed at a scratch clone."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from supervisor import template, updates
from supervisor.paths import Layout


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                         env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                              "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"]})
    return out.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """origin (bare) ← clone (the 'installed framework') ; a second clone pushes new commits to origin."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    author = tmp_path / "author"
    git(tmp_path, "clone", "-q", str(origin), str(author))
    (author / "supervisor").mkdir()
    (author / "supervisor" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (author / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n- first\n", encoding="utf-8")
    git(author, "add", "-A"); git(author, "commit", "-q", "-m", "first"); git(author, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    monkeypatch.setattr(updates, "framework_dir", lambda: clone)
    project = tmp_path / "proj"
    template.stamp(project, name="P")
    monkeypatch.setenv("SUPERVISOR_ROOT", str(project))
    return {"origin": origin, "author": author, "clone": clone, "layout": Layout(project).ensure()}


def push_update(author: Path, version: str = "1.1.0", migration: bool = False, n: int = 1):
    for i in range(n):
        (author / "supervisor" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        text = (author / "CHANGELOG.md").read_text(encoding="utf-8")
        text += f"\n## {version}\n- change {i}\n" + ("\n**Migration**: run supervisor migrate\n" if migration else "")
        (author / "CHANGELOG.md").write_text(text, encoding="utf-8")
        git(author, "commit", "-q", "-am", f"feature {i}")
    git(author, "push", "-q", "origin", "main")


def test_up_to_date(world):
    st = updates.check(world["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.behind == 0 and not st.available
    assert "up to date" in updates.describe(st)[0]


def test_lists_commits_version_and_migration_hint(world):
    push_update(world["author"], version="99.0.0", migration=True, n=2)
    st = updates.check(world["layout"], fetch=True, max_age_s=0)
    assert st.available and st.behind == 2 and [c.subject for c in st.commits] == ["feature 1", "feature 0"]
    assert st.remote_version == "99.0.0" and st.migration_hint
    lines = updates.describe(st)
    installed = updates.__version__                          # whatever this checkout is: the arrow shows the real pair
    assert "2 commit(s)" in lines[0] and f"{installed} → 99.0.0" in lines[0] and any("migration" in ln for ln in lines)


def test_check_is_cached_per_project(world):
    updates.check(world["layout"], fetch=True, max_age_s=0)
    push_update(world["author"])
    assert updates.check(world["layout"], fetch=True).behind == 0            # within TTL: cached
    assert updates.check(world["layout"], fetch=True, max_age_s=0).behind == 1


def test_dirty_clone_blocks_apply(world):
    push_update(world["author"])
    (world["clone"] / "CHANGELOG.md").write_text("local edit\n", encoding="utf-8")
    st = updates.check(world["layout"], fetch=True, max_age_s=0)
    assert st.dirty and any("blocked" in ln for ln in updates.describe(st))
    res = updates.apply(world["layout"], st, reinstall=False)
    assert not res.ok and "uncommitted" in res.error


@pytest.fixture
def live_loop(world, monkeypatch):
    """Pretend a loop is live: a RUNNING heartbeat, commands recorded instead of applied, the wrapper spawn stubbed."""
    from supervisor import commands as commands_mod, launcher, runtime
    runtime.heartbeat(world["layout"], "RUNNING")
    submitted, spawned = [], []
    monkeypatch.setattr(commands_mod, "submit_or_apply", lambda lay, kind, payload, actor: submitted.append(kind) or {"ok": True})
    monkeypatch.setattr(launcher, "spawn", lambda layout, args=None: spawned.append(layout.root))
    return {"submitted": submitted, "spawned": spawned}


def test_apply_stops_the_loop_pulls_migrates_doctors_and_restarts_it(world, live_loop):
    push_update(world["author"])
    layout = world["layout"]
    st = updates.check(layout, fetch=True, max_age_s=0)
    res = updates.apply(layout, st, reinstall=False)
    assert res.ok and res.restart_ui and res.loop_restarted, res
    assert git(world["clone"], "rev-parse", "HEAD") == git(world["author"], "rev-parse", "HEAD")
    assert any(s.startswith("pulled 1") for s in res.steps)
    assert any(s.startswith("migrate:") for s in res.steps) and any(s.startswith("doctor:") for s in res.steps)
    assert live_loop["submitted"] == ["loop.stop"] and live_loop["spawned"] == [layout.root]
    assert updates.check(layout, fetch=True, max_age_s=0).behind == 0


def test_migration_failure_fails_the_update_and_starts_nothing(world, live_loop, monkeypatch):
    push_update(world["author"])
    layout = world["layout"]
    monkeypatch.setattr(updates, "_child", lambda args, lay: (2, "", "a supervisor holds state/.lock (supervisor pid 1)"))
    st = updates.check(layout, fetch=True, max_age_s=0)
    res = updates.apply(layout, st, reinstall=False)
    assert not res.ok and not res.restart_ui and not res.loop_restarted
    assert "migrate failed (exit 2)" in res.error and "supervisor migrate" in res.error
    assert live_loop["spawned"] == [] and "loop.reload" not in live_loop["submitted"]
    assert layout.runtime.joinpath("update_check.json").exists()       # not cleared: the banner keeps showing the update


def test_a_new_doctor_failure_fails_the_update(world, live_loop, monkeypatch):
    push_update(world["author"])
    layout = world["layout"]
    monkeypatch.setattr(updates, "_child", lambda args, lay: (0, "nothing to migrate", "") if args == ["migrate"]
                        else (1, "OK   project: P\nFAIL state: task store unreadable: boom\nFAIL git: not a repo", ""))
    st = updates.check(layout, fetch=True, max_age_s=0)
    res = updates.apply(layout, st, reinstall=False)
    assert not res.ok and "state: task store unreadable" in res.error and "git" not in res.error   # git failed before too
    assert live_loop["spawned"] == []


def test_apply_waits_for_the_loop_to_release_the_lock(world, live_loop):
    """A held state/.lock: the updater requests a stop and waits for the lock before pulling; it fails, changing
    nothing, when the lock is not released within the timeout."""
    import threading
    from supervisor.locks import FileLock
    push_update(world["author"])
    layout = world["layout"]
    before = git(world["clone"], "rev-parse", "HEAD")
    release = threading.Event()
    held = threading.Event()

    def hold():
        with FileLock(layout.lock, owner="supervisor pid 999"):
            held.set()
            release.wait(10)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    held.wait(5)
    st = updates.check(layout, fetch=True, max_age_s=0)
    res = updates.apply(layout, st, reinstall=False, stop_timeout_s=1.0)
    assert not res.ok and "state/.lock" in res.error and "supervisor stop" in res.error
    assert git(world["clone"], "rev-parse", "HEAD") == before and live_loop["spawned"] == []
    threading.Timer(0.7, release.set).start()                       # the loop reaches its safe point and exits
    res = updates.apply(layout, st, reinstall=False, stop_timeout_s=10.0)
    t.join(5)
    assert res.ok, res
    assert any(s == "loop: stopped" for s in res.steps) and live_loop["submitted"].count("loop.stop") == 2


def test_no_origin_is_reported_not_raised(world):
    git(world["clone"], "remote", "remove", "origin")
    st = updates.check(world["layout"], fetch=True, max_age_s=0)
    assert not st.ok and "origin" in st.reason and not st.available
    assert updates.apply(world["layout"], st, reinstall=False).ok is False


def test_prompt_flow_declines_and_accepts(world, monkeypatch):
    push_update(world["author"])
    st = updates.check(world["layout"], fetch=True, max_age_s=0)
    echoed = []
    assert updates.prompt_and_apply(world["layout"], st, ask=lambda _: "n", echo=echoed.append) is None
    assert any("not updating" in e for e in echoed)
    monkeypatch.setattr(updates, "apply", lambda layout, status, reinstall=True, **kw: updates.Applied(ok=True, steps=["pulled"], restart_ui=True))
    res = updates.prompt_and_apply(world["layout"], st, ask=lambda _: "y", echo=echoed.append)
    assert res and res.ok


def test_overview_banner_and_update_route(world, monkeypatch):
    from fastapi.testclient import TestClient
    from supervisor.ui.app import create_app
    push_update(world["author"], n=1)
    layout = world["layout"]
    monkeypatch.setattr("supervisor.ui.app._start_update_checker", lambda layout: None)
    updates.check(layout, fetch=True, max_age_s=0)
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set("supervisor_csrf", "t0ken")
    page = client.get("/overview")
    assert page.status_code == 200 and "Framework update available" in page.text and "feature 0" in page.text
    restarted = []
    monkeypatch.setattr(updates, "restart_process_later", lambda *a, **k: restarted.append(True))
    monkeypatch.setattr(updates, "apply", lambda layout, status, reinstall=True, **kw: updates.Applied(ok=True, steps=["pulled 1 commit(s)"], restart_ui=True))
    r = client.post("/overview/update", data={"csrf_token": "t0ken"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "restarting" in r.text and restarted == [True]


def test_doctor_dying_without_a_verdict_fails_the_update(world, live_loop, monkeypatch):
    """A doctor child that exits non-zero without printing FAIL lines (an exception on the new code) is a failed
    update, never 'all checks OK': the exit code is authoritative."""
    push_update(world["author"])
    layout = world["layout"]
    monkeypatch.setattr(updates, "_child", lambda args, lay: (0, "nothing to migrate", "") if args == ["migrate"]
                        else (1, "", "Traceback (most recent call last):\n  ...\nImportError: boom"))
    res = updates.apply(layout, updates.check(layout, fetch=True, max_age_s=0), reinstall=False)
    assert not res.ok and "doctor" in res.error and "ImportError: boom" in res.error
    assert not any("all checks OK" in s for s in res.steps) and live_loop["spawned"] == []


def test_pip_failure_with_empty_output_fails_the_update(world, live_loop, monkeypatch):
    push_update(world["author"])
    layout = world["layout"]

    class Proc:
        returncode, stdout, stderr = 1, "", ""
    real_run = updates.subprocess.run
    monkeypatch.setattr(updates.subprocess, "run",
                        lambda argv, *a, **k: Proc() if "pip" in argv else real_run(argv, *a, **k))
    monkeypatch.setattr(updates, "_child", lambda args, lay: (0, "", ""))
    res = updates.apply(layout, updates.check(layout, fetch=True, max_age_s=0), reinstall=True)
    assert not res.ok and "pip install -e" in res.error and "exit 1" in res.error and live_loop["spawned"] == []
