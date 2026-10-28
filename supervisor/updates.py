"""Framework update check and apply. The framework is a git clone installed editable into each project, so "update"
means: fetch `origin`, show the commits the clone is behind, and on the owner's yes `git pull --ff-only`, reinstall
the package (dependencies may have changed), run the project's migrations and doctor, and restart the running
processes so they import the new code (the loop is stopped first — `supervisor migrate` refuses while it holds the
state lock — and started again through `supervisor.launcher`; the Control Center exits with RESTART_EXIT_CODE,
which its launcher turns into a restart).

A checkout detached at a release tag (a live project's runtime worktree, docs/UPDATING.md "Pinned checkouts") is
never pulled: the check fetches origin's tags and reports a newer `vX.Y.Z` release, which the project takes in a
maintenance window, and `apply` refuses it (since 1.12.0; before, the check compared HEAD with a missing `origin/HEAD`
and every launch printed git's error).

Everything here is fail-soft: no remote, no network, no git, or a dirty clone all produce a `Status` that says why
nothing can be done, never an exception out of a launch path."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .paths import Layout

RESTART_EXIT_CODE = 3            # same convention as the loop's reload: the launcher restarts the process
FETCH_TIMEOUT_S = 20
CHECK_TTL_S = 300                # a check younger than this is reused instead of fetching again
_VERSION_FILE = "supervisor/__init__.py"
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


@dataclass
class Commit:
    sha: str
    subject: str


@dataclass
class Status:
    ok: bool                                  # a check was performed (false: see `reason`)
    reason: str = ""                          # why not, or a note
    framework_dir: str = ""
    branch: str = ""
    behind: int = 0
    ahead: int = 0
    dirty: bool = False
    commits: list[Commit] = field(default_factory=list)
    installed_version: str = __version__
    remote_version: str = ""
    migration_hint: bool = False              # the remote CHANGELOG mentions a migration since HEAD
    checked_at: float = 0.0
    head: str = ""                            # the commit checked: a status describes this checkout in this state
    pinned: bool = False                      # detached HEAD: a release-tag checkout, never pulled
    pinned_tag: str = ""                      # the release tag HEAD is at
    newer_release: str = ""                   # the newest release tag above it, if any
    refresh_error: str = ""                   # a kept pinned result: why the latest refresh failed

    @property
    def available(self) -> bool:
        return self.ok and self.behind > 0

    @property
    def checked_iso(self) -> str:
        """When the check ran, as an ISO UTC stamp (the UI shows it in local time)."""
        return datetime.fromtimestamp(self.checked_at, timezone.utc).isoformat() if self.checked_at else ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Status":
        d = dict(d)
        d["commits"] = [Commit(**c) for c in d.get("commits") or []]
        return cls(**d)


def framework_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_checkout() -> dict:
    """What is running: the installed framework version, and — when the package is an editable install of a git
    checkout — that checkout's commit, the tag exactly at it (if any) and whether the working tree is dirty.
    {"version", "is_git", "commit", "tag", "dirty", "status_error", "path"}; commit/tag are None without git or
    outside a checkout. `dirty` counts modified tracked files AND non-ignored untracked files: an editable install
    imports whatever Python file sits in the package directory, tracked or not, so an untracked module is
    unreviewed runtime code. `status_error` is set (and `dirty` None) when git could not report the status at all —
    a checkout whose state cannot be inspected is not a clean one."""
    from . import __version__
    fw = framework_dir()
    out = {"version": __version__, "path": str(fw), "is_git": False, "commit": None, "tag": None, "dirty": False,
           "status_error": None}
    code, inside = _git(["rev-parse", "--is-inside-work-tree"], fw, timeout=10)
    if code != 0 or inside.strip() != "true":
        return out
    out["is_git"] = True
    code, sha = _git(["rev-parse", "--short=12", "HEAD"], fw, timeout=10)
    out["commit"] = sha.strip() if code == 0 and sha.strip() else None
    code, tag = _git(["describe", "--tags", "--exact-match", "HEAD"], fw, timeout=10)
    out["tag"] = tag.strip().splitlines()[0] if code == 0 and tag.strip() else None
    code, status = _git(["status", "--porcelain", "--untracked-files=normal"], fw, timeout=10)
    if code != 0:
        out["dirty"] = None
        out["status_error"] = f"git status failed ({status.strip()[:200] or f'exit {code}'})"
    else:
        out["dirty"] = bool(status.strip())
        out["untracked"] = [ln[3:] for ln in status.splitlines() if ln.startswith("?? ")]
    return out


def _git(args: list[str], cwd: Path, timeout: float = FETCH_TIMEOUT_S) -> tuple[int, str]:
    exe = shutil.which("git")
    if not exe:
        return 127, "git not on PATH"
    try:
        proc = subprocess.run([exe, "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)[:300]
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _remote_version(cwd: Path, ref: str) -> str:
    code, out = _git(["show", f"{ref}:{_VERSION_FILE}"], cwd, timeout=10)
    if code != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def _release(tag: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) of a release tag `vX.Y.Z`; None for any other tag name."""
    m = _RELEASE_TAG.match(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _cache_path(layout: Layout | None) -> Path | None:
    return (layout.runtime / "update_check.json") if layout is not None else None


def _identity(fw: Path) -> tuple[str, str, str]:
    """(path, branch, HEAD commit) of the checkout as it is now; branch is "HEAD" when detached."""
    code, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], fw, timeout=10)
    code2, head = _git(["rev-parse", "HEAD"], fw, timeout=10)
    return str(fw), branch.strip() if code == 0 else "", head.strip() if code2 == 0 else ""


def describes_checkout(status: Status) -> bool:
    """Whether `status` was taken of the framework checkout as it is now: same path, branch and HEAD. A check from
    before a `git checkout <tag>` (or any other move) says nothing about the checkout now (review 1.11.1 F1)."""
    path, branch, head = _identity(framework_dir())
    return bool(head) and (status.framework_dir, status.branch, status.head) == (path, branch, head)


def check(layout: Layout | None = None, fetch: bool = True, max_age_s: float = CHECK_TTL_S) -> Status:
    """Fetch origin and report how far the installed clone is behind. Cached per project for `max_age_s`, and only
    while the checkout is still where it was checked (describes_checkout). Without a fetch, a pinned checkout's last
    result from origin is served whatever its age (`checked_at` says when): a pinned check cannot ask origin
    without the network. A failed check returns its failure but does not replace that result: the result stays
    cached with the failure noted in `refresh_error` (review 1.11.1 R2-2: one refresh without the network erased
    the release the overview showed)."""
    cache = _cache_path(layout)
    prior = None
    if cache is not None and cache.exists():
        try:
            prior = Status.from_dict(json.loads(cache.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            prior = None
    if prior is not None and prior.ok and describes_checkout(prior):
        if time.time() - prior.checked_at < max_age_s or (prior.pinned and not fetch):
            return prior
    else:
        prior = None                                        # nothing worth keeping for this checkout
    status = _check(fetch)
    record = status
    if not status.ok and prior is not None and prior.pinned:
        prior.refresh_error = status.reason
        record = prior
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(record.to_dict()), encoding="utf-8")
        except OSError:
            pass
    return status


def _check(fetch: bool) -> Status:
    fw = framework_dir()
    st = Status(ok=False, framework_dir=str(fw), checked_at=time.time())
    if not (fw / ".git").exists():
        st.reason = "framework is not a git clone (installed from a package?)"
        return st
    code, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], fw, timeout=10)
    if code != 0:
        st.reason = f"git: {branch}"
        return st
    st.branch = branch
    st.head = _identity(fw)[2]
    code, remote = _git(["remote", "get-url", "origin"], fw, timeout=10)
    if code != 0:
        st.reason = "no `origin` remote configured on the framework clone"
        return st
    if branch == "HEAD":
        return _check_pinned(fw, st, fetch)
    if fetch:
        code, out = _git(["fetch", "--quiet", "origin", branch], fw)
        if code != 0:
            st.reason = f"fetch failed (offline?): {out[-200:]}"
            return st
    code, counts = _git(["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"], fw, timeout=10)
    if code != 0:
        st.reason = f"origin/{branch} unknown: {counts[-200:]}"
        return st
    ahead, behind = (int(x) for x in counts.split())
    st.ahead, st.behind = ahead, behind
    code, dirty = _git(["status", "--porcelain", "--untracked-files=no"], fw, timeout=10)
    st.dirty = code == 0 and bool(dirty.strip())
    if behind:
        code, log = _git(["log", "--format=%h%x09%s", f"HEAD..origin/{branch}"], fw, timeout=10)
        if code == 0:
            st.commits = [Commit(*line.split("\t", 1)) if "\t" in line else Commit(line, "") for line in log.splitlines()]
        st.remote_version = _remote_version(fw, f"origin/{branch}")
        code, diff = _git(["diff", f"HEAD..origin/{branch}", "--", "CHANGELOG.md"], fw, timeout=10)
        st.migration_hint = code == 0 and "migration" in diff.lower()
    st.ok = True
    return st


def _check_pinned(fw: Path, st: Status, fetch: bool) -> Status:
    """A detached checkout: the release it runs, and the newest release origin publishes, compared by version
    number (v1.10.0 is above v1.9.0). Tags other than `vX.Y.Z` are not releases. Both come from origin's own list
    (`git ls-remote --tags`, each tag peeled to its commit), never from the local tags: a runtime worktree shares
    those with its development clone, where a tag may exist that was never pushed, on another commit (review 1.11.1
    F2) or on this one (R2-1). The release the checkout runs is the newest one origin publishes at HEAD's commit;
    when origin publishes none there, the check says so and announces nothing. Only the newer release's objects are
    fetched, for its changelog; no local ref is created or moved, and nothing here moves HEAD. Without a fetch there
    is no fresh word from origin: check() serves the last result instead."""
    st.pinned = True
    if not fetch:
        st.reason = "origin's releases have not been checked yet"
        return st
    code, listed = _git(["ls-remote", "--tags", "origin"], fw)
    if code != 0:
        st.reason = f"could not list origin's tags (offline?): {listed[-200:]}"
        return st
    published: dict[str, str] = {}                           # release tag -> the commit it marks
    for line in listed.splitlines():
        target, _, ref = line.partition("\t")
        ref = ref.strip()
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        if _release(name) and (ref.endswith("^{}") or name not in published):
            published[name] = target.strip()                 # an annotated tag's peeled line names its commit
    newest = max(published, key=_release, default=None)
    here = [name for name, commit in published.items() if commit == st.head]
    if not here:
        code, at = _git(["tag", "--points-at", "HEAD"], fw, timeout=10)
        local = sorted((t for t in at.split() if _release(t)), key=_release) if code == 0 else []
        st.reason = ((f"the framework checkout is detached at {', '.join(local)}, but origin publishes no release at "
                      "this commit" if local else
                      "the framework checkout is detached but not at a release tag (vX.Y.Z); `supervisor doctor` "
                      "fails it")
                     + (f"; the newest release origin publishes is {newest}" if newest else "")
                     + ": check out a published release tag")
        return st
    st.pinned_tag = max(here, key=_release)
    if newest and _release(newest) > _release(st.pinned_tag):
        st.newer_release = newest
        st.remote_version = newest[1:]
        code, _out = _git(["fetch", "--quiet", "--no-tags", "origin", f"refs/tags/{newest}"], fw)
        if code == 0:                                        # into the object store only: no ref is written
            code, diff = _git(["diff", f"HEAD..{published[newest]}", "--", "CHANGELOG.md"], fw, timeout=10)
            st.migration_hint = code == 0 and "migration" in diff.lower()
    st.ok = True
    return st


def describe(status: Status) -> list[str]:
    """Human lines for the console and the Control Center."""
    if not status.ok:
        return [f"update check skipped: {status.reason}"]
    if status.pinned:
        if not status.newer_release:
            return [f"framework {status.installed_version} is pinned at {status.pinned_tag}, the newest release"]
        lines = [f"framework release {status.newer_release} is available; this checkout is pinned at {status.pinned_tag}",
                 ("  take it in a maintenance window: drain, stop, in the framework checkout "
                  f"`git fetch origin tag {status.newer_release}` and `git checkout {status.newer_release}`, migrate, "
                  "doctor, start (docs/UPDATING.md, \"Pinned checkouts\"); the updater never moves a pinned checkout")]
        if status.migration_hint:
            lines.append("  note: the changelog mentions a migration; run `supervisor migrate --dry-run`, then "
                         "`supervisor migrate`")
        return lines
    if not status.behind:
        return [f"framework {status.installed_version} is up to date with origin/{status.branch}"]
    head = f"framework update available: {status.behind} commit(s) on origin/{status.branch}"
    if status.remote_version and status.remote_version != status.installed_version:
        head += f" ({status.installed_version} → {status.remote_version})"
    lines = [head] + [f"  {c.sha} {c.subject}" for c in status.commits[:20]]
    if len(status.commits) > 20:
        lines.append(f"  … {len(status.commits) - 20} more")
    if status.migration_hint:
        lines.append("  note: the changelog mentions a migration; `supervisor migrate` runs as part of the update")
    if status.dirty:
        lines.append("  blocked: the framework clone has uncommitted changes; commit or stash them first")
    if status.ahead:
        lines.append(f"  note: the clone is also {status.ahead} commit(s) ahead of origin (unpushed work)")
    return lines


@dataclass
class Applied:
    ok: bool
    steps: list[str] = field(default_factory=list)
    error: str = ""                           # on failure: what went wrong and what the owner should do now
    restart_ui: bool = False
    loop_restarted: bool = False              # a loop was running, was stopped for the update and started again


STOP_TIMEOUT_S = 120.0           # how long apply() waits for a running loop to reach its safe point and release the lock


def _stop_loop(layout: Layout, out: Applied, timeout_s: float) -> bool:
    """Ask a running loop to stop (`loop.stop` through the commands library: it stops at the next safe point and
    releases state/.lock) and wait for the lock. Returns False, with `out.error` set, when the lock is still held
    after `timeout_s`. A lock held by something that is not the loop (e.g. `supervisor task import`) is waited for
    the same way. Nothing is pulled or migrated while anything holds the lock: `supervisor migrate` refuses then."""
    from . import commands as commands_mod, runtime
    from .locks import holder
    live = runtime.is_live(layout)
    h = holder(layout.lock)
    if not live and h is None:
        return True
    if live:
        commands_mod.submit_or_apply(layout, "loop.stop", {}, actor="update")
        out.steps.append("loop: stop requested (it stops at its next safe point; running jobs are picked up on restart)")
    deadline = time.monotonic() + timeout_s
    while holder(layout.lock) is not None:
        if time.monotonic() >= deadline:
            out.error = (f"the loop did not release state/.lock within {int(timeout_s)}s (holder: "
                         f"{(holder(layout.lock) or {}).get('owner')}); nothing was changed. Stop it (Stop button or "
                         "`supervisor stop`), wait for it to exit, then run the update again")
            return False
        time.sleep(0.5)
    if live:
        out.steps.append("loop: stopped")
    return True


def _child(args: list[str], layout: Layout) -> tuple[int, str, str] | None:
    """Run `python -m supervisor <args>` on the project in a child (the NEW code after a pull); None if it could
    not be started at all."""
    try:
        proc = subprocess.run([sys.executable, "-m", "supervisor", *args], cwd=str(layout.root), capture_output=True,
                              text=True, timeout=300, check=False, env={**os.environ, "SUPERVISOR_ROOT": str(layout.root)},
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def apply(layout: Layout, status: Status | None = None, reinstall: bool = True, stop_timeout_s: float = STOP_TIMEOUT_S) -> Applied:
    """Stop a running loop, pull, reinstall, migrate, doctor, start the loop again. The caller restarts the UI
    (restart_ui). Any failing step fails the update (ok=False, `error` says what to do); the loop is not started
    on a project that did not migrate or does not pass the doctor."""
    from . import doctor as doctor_mod, launcher, migrations, tasks as tasks_mod
    fw = framework_dir()
    status = status or check(layout, fetch=True, max_age_s=0)
    out = Applied(ok=False)
    if not status.ok:
        out.error = status.reason
        return out
    # The checkout as it is now decides, not the status handed in: a status from before the checkout was pinned
    # still says "branch, N behind" (review 1.11.1 F1). Nothing is stopped or pulled before these two checks.
    _path, branch_now, _head = _identity(fw)
    if status.pinned or branch_now == "HEAD":
        pinned_at = status.pinned_tag if status.pinned else "a detached HEAD"
        out.error = (f"the framework checkout is pinned at {pinned_at}"
                     + (f"; take {status.newer_release} in a maintenance window" if status.newer_release else
                        "; take a release in a maintenance window")
                     + " (docs/UPDATING.md, \"Pinned checkouts\"); nothing was changed")
        return out
    if not describes_checkout(status):
        out.error = ("the framework checkout changed since this update check (another branch or commit); nothing was "
                     "changed: check again")
        return out
    if status.dirty:
        out.error = "framework clone has uncommitted changes; commit or stash them first"
        return out
    from . import runtime
    was_live = runtime.is_live(layout)
    if not _stop_loop(layout, out, stop_timeout_s):
        return out
    failing_before = {c.name for c in doctor_mod.run(layout) if not c.ok}   # the update must not make the doctor worse
    pulled = False
    if not status.behind:
        out.steps.append("already up to date")
    else:
        code, text = _git(["pull", "--ff-only", "origin", status.branch], fw, timeout=120)
        if code != 0:
            out.error = f"git pull --ff-only failed: {text[-300:]}"
            return out
        pulled = True
        out.steps.append(f"pulled {status.behind} commit(s) from origin/{status.branch}")
        if reinstall:
            try:
                proc = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", f"{fw}[ui]"], capture_output=True,
                                      text=True, timeout=600, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode == 0:
                    err = ""
                else:                                   # a failed pip may say nothing on stderr: the exit code is the verdict
                    err = (proc.stderr or proc.stdout or "").strip()[-300:] or f"exit {proc.returncode}"
            except (OSError, subprocess.SubprocessError) as exc:
                err = str(exc)[:300] or type(exc).__name__
            if err:
                out.steps.append("pip install -e failed")
                out.error = (f"pip install -e {fw} failed: {err} — the clone is on the new code but its dependencies are not "
                             "installed; run the install by hand (tools/setup_env), then `supervisor migrate`, "
                             "`supervisor doctor`, and start the loop")
                return out
            out.steps.append("reinstalled the package")
    # The running process still has the old code; migrations run in a child on the NEW code so the project is
    # brought forward before anything restarts into it.
    res = _child(["migrate"], layout)
    if res is None:
        try:
            notes = migrations.run(layout)              # fall back to in-process (old code, but still correct for its version)
            out.steps.append("migrate (in-process): " + ("; ".join(notes) if notes else "nothing to migrate"))
        except Exception as exc:                        # noqa: BLE001 - reported as the update's failure
            out.steps.append("migrate (in-process) failed")
            out.error = f"supervisor migrate failed: {exc}; fix it and run `supervisor migrate` by hand, then start the loop"
            return out
    else:
        code, stdout, stderr = res
        notes = [ln for ln in stdout.splitlines() if ln.strip()]
        if code != 0:
            detail = (stderr.strip() or "; ".join(notes))[-300:]
            out.steps.append(f"migrate failed (exit {code})")
            out.error = (f"supervisor migrate failed (exit {code}): {detail}" + (" — the framework clone is already on the new code;" if pulled else ";")
                         + " fix the cause and run `supervisor migrate` then `supervisor doctor` by hand before starting the loop")
            return out
        out.steps.append("migrate: " + ("; ".join(notes) if notes else "nothing to migrate"))
    res = _child(["doctor"], layout)
    if res is None:
        fails = {c.name: c.detail for c in doctor_mod.run(layout) if not c.ok}
    else:
        code, stdout, stderr = res
        fails = {}
        for ln in stdout.splitlines():
            if ln.startswith("FAIL "):
                name, _, detail = ln[5:].partition(":")
                fails[name.strip()] = detail.strip()
        if code != 0 and not fails:
            # Doctor died before it could print its verdict (an exception on the new code, say): that is a failure
            # of the update, never "all checks OK" — the exit code is authoritative, the FAIL lines are only detail.
            detail = (stderr.strip() or stdout.strip())[-300:] or "no output"
            fails["doctor"] = f"exited {code} without a verdict: {detail}"
    new_fails = {k: v for k, v in fails.items() if k not in failing_before}
    if new_fails:
        out.steps.append("doctor: " + "; ".join(f"{k}: {v}" for k, v in fails.items()))
        out.error = ("supervisor doctor fails after the update: " + "; ".join(f"{k}: {v}" for k, v in new_fails.items())
                     + " — fix it (`supervisor doctor`), then start the loop")
        return out
    out.steps.append("doctor: all checks OK" if not fails else "doctor: OK apart from pre-existing " + ", ".join(fails))
    if was_live:
        try:
            launcher.spawn(layout)
            out.loop_restarted = True
            out.steps.append("loop: started again on the new code (through the restart-on-reload wrapper)")
        except OSError as exc:
            out.error = f"the loop was stopped for the update but could not be started again: {exc}; press Start"
            return out
    else:
        out.steps.append("loop: not running; it starts on the new code next time")
    tasks_mod.log(layout, "framework_update", "owner", steps=out.steps, behind=status.behind,
                  from_version=status.installed_version, to_version=status.remote_version or None)
    try:
        cache = _cache_path(layout)
        if cache and cache.exists():
            cache.unlink()
    except OSError:
        pass
    out.ok = True
    out.restart_ui = status.behind > 0
    return out


def restart_process_later(delay_s: float = 1.0, code: int = RESTART_EXIT_CODE) -> None:
    """Exit this process shortly with the restart code (after the HTTP response has gone out). The launcher
    (`start_control_center.bat`, `tools/run_supervisor.ps1`) restarts it; a manually started process just exits and
    the message on screen says to start it again."""
    def _bye():
        time.sleep(delay_s)
        os._exit(code)
    threading.Thread(target=_bye, daemon=True).start()


def prompt_and_apply(layout: Layout, status: Status, ask=input, echo=print) -> Applied | None:
    """Console flow for `supervisor ui`/`run` launches: list the updates, ask y/N, apply on yes."""
    for line in describe(status):
        echo(line)
    if not status.available or status.dirty:
        return None
    try:
        answer = ask("Update now? Stops the loop, pulls, reinstalls, runs migrate + doctor, restarts the loop [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer not in ("y", "yes"):
        echo("not updating; run `supervisor update` later")
        return None
    result = apply(layout, status)
    for step in result.steps:
        echo("  " + step)
    if not result.ok:
        echo("update failed: " + result.error)
    return result
