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


def test_apply_pulls_migrates_doctors_and_reloads(world, monkeypatch):
    from supervisor import commands as commands_mod, runtime
    push_update(world["author"])
    layout = world["layout"]
    runtime.heartbeat(layout, "RUNNING")                   # pretend a loop is live so a reload gets queued
    submitted = []
    monkeypatch.setattr(commands_mod, "submit_or_apply", lambda lay, kind, payload, actor: submitted.append(kind) or {"ok": True})
    st = updates.check(layout, fetch=True, max_age_s=0)
    res = updates.apply(layout, st, reinstall=False)
    assert res.ok and res.restart_ui, res
    assert git(world["clone"], "rev-parse", "HEAD") == git(world["author"], "rev-parse", "HEAD")
    assert any(s.startswith("pulled 1") for s in res.steps)
    assert any(s.startswith("migrate:") for s in res.steps) and any(s.startswith("doctor:") for s in res.steps)
    assert submitted == ["loop.reload"]
    assert updates.check(layout, fetch=True, max_age_s=0).behind == 0


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
    monkeypatch.setattr(updates, "apply", lambda layout, status, reinstall=True: updates.Applied(ok=True, steps=["pulled"], restart_ui=True))
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
    monkeypatch.setattr(updates, "apply", lambda layout, status, reinstall=True: updates.Applied(ok=True, steps=["pulled 1 commit(s)"], restart_ui=True))
    r = client.post("/overview/update", data={"csrf_token": "t0ken"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "restarting" in r.text and restarted == [True]
