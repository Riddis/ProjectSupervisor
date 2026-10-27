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


# ---------------------------------------------------------------- a pinned runtime checkout (1.11.1)
# A live project runs a worktree detached at a release tag (docs/UPDATING.md, "Pinned checkouts"). Before 1.11.1 the
# check compared HEAD with `origin/HEAD`, which does not exist, and every launch printed git's error.

def tag(cwd: Path, name: str) -> None:
    git(cwd, "tag", "-a", name, "-m", name)


@pytest.fixture
def pinned(world):
    """The clone detached at v1.0.0, the way a project's runtime checkout is pinned. A runtime worktree of a
    development clone has no `origin/HEAD` (only `git clone` sets one): removed, as it is there."""
    tag(world["author"], "v1.0.0")
    git(world["author"], "push", "-q", "origin", "v1.0.0")
    git(world["clone"], "fetch", "-q", "--tags", "origin")
    git(world["clone"], "checkout", "-q", "v1.0.0")
    git(world["clone"], "remote", "set-head", "origin", "--delete")
    return world


def release(author: Path, version: str, migration: bool = False) -> None:
    push_update(author, version=version, migration=migration)
    tag(author, f"v{version}")
    git(author, "push", "-q", "origin", f"v{version}")


def test_a_pinned_checkout_at_the_newest_release_is_up_to_date_without_a_git_error(pinned):
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.pinned and st.pinned_tag == "v1.0.0" and not st.newer_release and not st.available
    lines = updates.describe(st)
    assert "v1.0.0" in lines[0] and "newest release" in lines[0]
    assert not any("origin/HEAD" in line or "fatal" in line for line in lines)


def test_a_pinned_checkout_reports_a_newer_release_and_never_offers_a_pull(pinned):
    release(pinned["author"], "1.1.0", migration=True)
    push_update(pinned["author"], version="1.2.0-dev")                  # an untagged commit is not a release
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.pinned and st.newer_release == "v1.1.0" and st.remote_version == "1.1.0"
    assert st.migration_hint and not st.available and st.behind == 0
    text = "\n".join(updates.describe(st))
    assert "v1.0.0" in text and "v1.1.0" in text and "maintenance window" in text
    asked = []
    assert updates.prompt_and_apply(pinned["layout"], st, ask=asked.append, echo=lambda _l: None) is None
    assert asked == []                                                  # nothing to say yes to
    head = git(pinned["clone"], "rev-parse", "HEAD")
    res = updates.apply(pinned["layout"], st, reinstall=False)
    assert not res.ok and "pinned" in res.error and "v1.1.0" in res.error and res.steps == []
    assert git(pinned["clone"], "rev-parse", "HEAD") == head


def test_the_newest_release_is_chosen_by_version_number_not_by_name(pinned):
    for version in ("1.9.0", "1.10.0"):
        release(pinned["author"], version)
    tag(pinned["author"], "pre-release-notes")                          # not a vX.Y.Z tag: ignored
    git(pinned["author"], "push", "-q", "origin", "pre-release-notes")
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.newer_release == "v1.10.0" and st.remote_version == "1.10.0"


def test_a_detached_checkout_not_at_a_release_tag_says_so_plainly(pinned):
    push_update(pinned["author"])
    git(pinned["clone"], "fetch", "-q", "origin", "main")
    git(pinned["clone"], "checkout", "-q", "FETCH_HEAD")
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert not st.ok and not st.available and "not at a release tag" in st.reason
    assert "origin/HEAD" not in st.reason and "fatal" not in st.reason


def test_the_launch_check_is_silent_when_pinned_to_the_newest_release_and_names_a_newer_one(pinned, capsys):
    from supervisor import cli
    cli._launch_update_check(pinned["layout"], interactive=True)
    assert capsys.readouterr().out == ""
    release(pinned["author"], "1.1.0")
    (pinned["layout"].runtime / "update_check.json").unlink()
    cli._launch_update_check(pinned["layout"], interactive=True)
    out = capsys.readouterr().out
    assert "v1.1.0" in out and "maintenance window" in out and "[y/N]" not in out


def test_the_overview_names_a_newer_release_for_a_pinned_checkout_without_an_update_button(pinned, monkeypatch):
    from fastapi.testclient import TestClient
    from supervisor.ui.app import create_app
    release(pinned["author"], "1.1.0")
    layout = pinned["layout"]
    monkeypatch.setattr("supervisor.ui.app._start_update_checker", lambda layout: None)
    updates.check(layout, fetch=True, max_age_s=0)
    page = TestClient(create_app(layout), base_url="http://127.0.0.1").get("/overview")
    assert page.status_code == 200 and "v1.1.0" in page.text and "maintenance window" in page.text
    assert "Update now" not in page.text and "Origin last checked" in page.text


# ---------------------------------------------------------------- review 1.11.1 F1: a status from before the pin

@pytest.fixture
def pinned_after_a_branch_check(world, monkeypatch):
    """A real branch check that finds a pull (a release, then an untagged development commit), cached; then the
    clone is pinned at v1.0.0 within the cache's lifetime. Stopping the loop is recorded, never performed."""
    tag(world["author"], "v1.0.0")
    git(world["author"], "push", "-q", "origin", "v1.0.0")
    release(world["author"], "1.1.0")
    push_update(world["author"], version="1.2.0-dev")
    branch_status = updates.check(world["layout"], fetch=True, max_age_s=0)
    assert branch_status.available and not branch_status.pinned
    git(world["clone"], "fetch", "-q", "--tags", "origin")
    git(world["clone"], "checkout", "-q", "v1.0.0")
    stops = []
    monkeypatch.setattr(updates, "_stop_loop", lambda layout, out, timeout_s: stops.append(True) or True)
    return {**world, "branch_status": branch_status, "stops": stops, "head": git(world["clone"], "rev-parse", "HEAD")}


def test_the_cached_check_is_not_reused_once_the_checkout_is_pinned(pinned_after_a_branch_check):
    w = pinned_after_a_branch_check
    st = updates.check(w["layout"], fetch=True)                         # the launch path: the default cache age
    assert st.pinned and st.pinned_tag == "v1.0.0" and not st.available
    asked = []
    assert updates.prompt_and_apply(w["layout"], st, ask=asked.append, echo=lambda _l: None) is None
    assert asked == []


@pytest.mark.parametrize("supplied", [True, False], ids=["status-from-before-the-pin", "fresh-check"])
def test_apply_refuses_a_detached_checkout_whatever_status_it_is_given(pinned_after_a_branch_check, supplied):
    w = pinned_after_a_branch_check
    res = updates.apply(w["layout"], w["branch_status"] if supplied else None, reinstall=False)
    assert not res.ok and "pinned" in res.error and res.steps == []
    assert w["stops"] == [] and git(w["clone"], "rev-parse", "HEAD") == w["head"]


def test_apply_refuses_a_status_for_another_state_of_the_checkout(world, monkeypatch):
    """Still on the branch, but HEAD moved since the check: the status no longer describes this checkout."""
    push_update(world["author"])
    st = updates.check(world["layout"], fetch=True, max_age_s=0)
    assert st.available
    (world["clone"] / "local.txt").write_text("x\n", encoding="utf-8")
    git(world["clone"], "add", "local.txt")
    git(world["clone"], "commit", "-q", "-m", "local work")
    stops = []
    monkeypatch.setattr(updates, "_stop_loop", lambda layout, out, timeout_s: stops.append(True) or True)
    res = updates.apply(world["layout"], st, reinstall=False)
    assert not res.ok and "changed since" in res.error and res.steps == [] and stops == []


# ---------------------------------------------------------------- review 1.11.1 F2: only what origin publishes

def test_an_unpublished_local_tag_is_not_a_release_and_the_published_one_is_found(pinned):
    """A runtime worktree shares its tags with the development clone: a tag made there and never pushed is not a
    release. The check also leaves the clone's own tags as they were."""
    release(pinned["author"], "1.1.0")
    push_update(pinned["author"], version="9.0.0-dev")
    git(pinned["clone"], "fetch", "-q", "origin", "main")
    git(pinned["clone"], "tag", "-a", "v9.0.0", "-m", "local only", "FETCH_HEAD")
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.newer_release == "v1.1.0" and st.remote_version == "1.1.0"
    assert "v9.0.0" not in "\n".join(updates.describe(st))
    assert git(pinned["clone"], "tag", "--list", "v1.1.0") == ""


def test_without_a_fetch_a_pinned_checkout_shows_its_last_check_of_origin_and_never_its_local_tags(pinned):
    """The overview reads the check without fetching. It shows the last result taken from origin, however old, with
    its time; with none yet, it shows no release (and never one read from the local tags)."""
    release(pinned["author"], "1.1.0")
    git(pinned["clone"], "fetch", "-q", "--tags", "origin")              # v1.1.0 is now a local tag as well
    cache = pinned["layout"].runtime / "update_check.json"
    st = updates.check(pinned["layout"], fetch=False, max_age_s=0)
    assert not st.ok and not st.newer_release and "origin" in st.reason
    fresh = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert fresh.newer_release == "v1.1.0"
    old = fresh.to_dict() | {"checked_at": fresh.checked_at - 86400}
    cache.write_text(__import__("json").dumps(old), encoding="utf-8")
    last = updates.check(pinned["layout"], fetch=False)
    assert last.newer_release == "v1.1.0" and last.checked_at == old["checked_at"]


# ---------------------------------------------------------------- review 1.11.1 R2-1: the release the checkout is at

def test_an_unpublished_tag_at_the_pinned_commit_does_not_hide_a_published_release(pinned):
    """A local-only v8.0.0 on the very commit the checkout is pinned at: the checkout still runs v1.0.0, the release
    origin publishes at that commit, and v1.1.0 is still newer."""
    release(pinned["author"], "1.1.0")
    git(pinned["clone"], "tag", "-a", "v8.0.0", "-m", "local alias", "HEAD")
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.pinned_tag == "v1.0.0" and st.newer_release == "v1.1.0"
    assert "v8.0.0" not in "\n".join(updates.describe(st))


def test_a_checkout_at_a_tag_origin_does_not_publish_says_so_and_names_the_newest_release(pinned):
    """Detached at a commit only a local tag marks: which release this is cannot be established, and the check says
    so instead of announcing (or hiding) anything."""
    release(pinned["author"], "1.1.0")
    push_update(pinned["author"], version="5.0.0-dev")
    git(pinned["clone"], "fetch", "-q", "origin", "main")
    git(pinned["clone"], "checkout", "-q", "FETCH_HEAD")
    git(pinned["clone"], "tag", "-a", "v5.0.0", "-m", "local only", "HEAD")
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert not st.ok and not st.newer_release
    assert "origin publishes no release at this commit" in st.reason and "v1.1.0" in st.reason


def test_a_lightweight_release_tag_is_recognised_as_the_pinned_release(pinned):
    """`ls-remote` lists a lightweight tag by its commit alone (no peeled line); it is still the release here."""
    push_update(pinned["author"], version="1.2.0")
    git(pinned["author"], "tag", "v1.2.0")
    git(pinned["author"], "push", "-q", "origin", "v1.2.0")
    git(pinned["clone"], "fetch", "-q", "--tags", "origin")
    git(pinned["clone"], "checkout", "-q", "v1.2.0")
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.pinned_tag == "v1.2.0" and not st.newer_release
