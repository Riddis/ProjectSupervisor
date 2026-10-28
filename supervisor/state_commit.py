"""Commits of the supervisor's own records (1.12.1). A checkpoint (checkpoint.py) commits exactly a completed task's
accepted paths and its record; everything else the loop keeps in tracked files is the loop's own bookkeeping, and
this module commits it: the other task records, the inbox, issues, gates, applied commands, the seed, the decision
records, the projections, a checkpoint's final record (written after its commit) and what `supervisor migrate`
rewrote. Before 1.12.1 nothing committed them, so a live project collected them as uncommitted changes.

A state commit names its paths (`git commit -- <paths>`), so the owner's edits elsewhere, staged or not, are never
swept in. It commits `state/` and the projections only when something under `state/` changed: a projection carries a
render stamp, and projections alone would make a commit on every render. It leaves out a path a task has delivered but
not yet had accepted (its checkpoint commits that), and stops when someone else has staged one of its paths. The loop
calls `maybe()`: at once after a checkpoint, otherwise at most every `checkpoint.state_interval_minutes` (15).
`checkpoint.state_commits: false` (routing.yaml) turns it off, for migrate too."""
from __future__ import annotations

import json
import subprocess
import time

_now = time.time                               # patched in tests
from pathlib import Path

import yaml

from . import checkpoint, store, tasks as tasks_mod
from .models.task import TaskStatus
from .paths import Layout

STATE = "state"
RECORD = "state_commit.json"                   # state/runtime/: when the last state commit was made


def _git(root: Path, *args: str) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). Kept apart: git's warnings (line endings, say) go to stderr and must never be read
    as file names."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=checkpoint.GIT_TIMEOUT_S, check=False)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)[:300]


def settings(layout: Layout) -> dict:
    out = {"enabled": True, "interval_minutes": 15.0}
    cfg = layout.config / "routing.yaml"
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        cp = data.get("checkpoint") or {}
        if "state_commits" in cp:
            out["enabled"] = bool(cp["state_commits"])
        if cp.get("state_interval_minutes") is not None:
            out["interval_minutes"] = float(cp["state_interval_minutes"])
    return out


def projections(layout: Layout) -> list[str]:
    """The rendered files: the framework's and the ones extensions' projections are known to write."""
    from . import extensions, verify
    return sorted(set(verify.EXCLUDED_FILES) | extensions.known_projection_paths(layout))


def changed(layout: Layout, paths: list[str]) -> list[str]:
    """Root-relative files under `paths` that differ from HEAD (deleted ones included) or are new and not ignored."""
    out = set()
    for args in (("diff", "--name-only", "-z", "HEAD", "--"),
                 ("ls-files", "--others", "--exclude-standard", "-z", "--")):
        code, text, _err = _git(layout.root, *args, *paths)
        if code == 0:
            out |= {name.replace("\\", "/") for name in text.split("\0") if name.strip()}
    return sorted(out)


def _norm(path: str) -> str:
    """A root-relative path in one form: forward slashes, no leading `./`, no trailing slash, case folded (the
    comparison errs towards holding a file back on a case-insensitive file system). The root itself is ""."""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return "" if p == "." else p.casefold()


def _not_yet_accepted(layout: Layout) -> list[str]:
    """Delivered paths of tasks without a checkpoint yet, normalized: their own checkpoint commits them, as accepted.
    A path may be a directory, and then everything under it waits (review 1.12.1 R1)."""
    out = set()
    for t in tasks_mod.load_all(layout).values():
        if t.status == TaskStatus.CANCELLED:
            continue
        if t.status == TaskStatus.COMPLETE and (t.model_extra or {}).get("checkpoint"):
            continue
        out |= {_norm(p) for p in t.paths}
    return sorted(out)


def _held_back(path: str, delivered: list[str]) -> bool:
    """Whether `path` is, or lies under, a path a task delivered without acceptance ("" is the whole project)."""
    p = _norm(path)
    return any(d == "" or p == d or p.startswith(d + "/") for d in delivered)


def _own_repository(root: Path) -> bool:
    """The project root is a repository's top level. A project folder inside another repository (a test project in
    an ignored folder of the framework's checkout, say) is not its own repository, and nothing is committed there."""
    code, out, _err = _git(root, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        return False
    try:
        return Path(out.strip().splitlines()[-1]).resolve() == Path(root).resolve()
    except OSError:
        return False


def commit(layout: Layout, reason: str, extra: list[str] | None = None, title: str | None = None) -> dict:
    """Commit the loop's records now (and `extra` root-relative files, as migrate does). {"status": "committed",
    "commit", "paths"} or {"status": "unchanged"} or {"status": "skipped", "reason"}."""
    if not settings(layout)["enabled"]:
        return {"status": "skipped", "reason": "checkpoint.state_commits is off"}
    root = layout.root
    if not _own_repository(root):
        return {"status": "skipped", "reason": "not a git repository"}
    delivered = _not_yet_accepted(layout)
    state_files = [p for p in changed(layout, [STATE]) if not _held_back(p, delivered)]
    extra_files = [p for p in changed(layout, list(extra)) if not _held_back(p, delivered)] if extra else []
    if not state_files and not extra_files:
        return {"status": "unchanged"}
    files = sorted(set(state_files) | set(extra_files)
                   | {p for p in changed(layout, projections(layout)) if not _held_back(p, delivered)})
    code, staged, _err = _git(root, "diff", "--cached", "--name-only", "--", *files)
    if code == 0 and staged.strip():
        names = ", ".join(staged.split()[:5])
        tasks_mod.log(layout, "state_commit_skipped", "state_commit", reason="staged", paths=staged.split()[:20])
        return {"status": "skipped", "reason": f"staged by someone else: {names}"}
    code, _out, err = _git(root, "add", "--", *files)
    if code != 0:
        tasks_mod.log(layout, "state_commit_failed", "state_commit", error=err[:300])
        return {"status": "failed", "reason": f"git add: {err[:300]}"}
    message = (f"{title or 'state: the supervisor records (' + reason + ')'}\n\n"
               f"Committed by the supervisor (checkpoint.state_commits), these {len(files)} file(s) and nothing else:\n"
               + "".join(f"- {p}\n" for p in files[:40])
               + (f"- ... and {len(files) - 40} more\n" if len(files) > 40 else ""))
    code, out, err = _git(root, "commit", "-q", "-m", message, "--", *files)
    if code != 0:
        if "nothing to commit" in out + err or "no changes added" in out + err:
            return {"status": "unchanged"}
        tasks_mod.log(layout, "state_commit_failed", "state_commit", error=(err or out)[:300])
        return {"status": "failed", "reason": f"git commit: {(err or out)[:300]}"}
    code, head, _err = _git(root, "rev-parse", "HEAD")
    sha = head.strip() if code == 0 else None
    _remember(layout, sha)
    return {"status": "committed", "commit": sha, "paths": files}


def _record_path(layout: Layout) -> Path:
    return layout.root / STATE / "runtime" / RECORD


def _remember(layout: Layout, sha: str | None) -> None:
    p = _record_path(layout)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(p, json.dumps({"at": _now(), "commit": sha}) + "\n")
    except OSError:
        pass


def _last(layout: Layout) -> float:
    try:
        return float(json.loads(_record_path(layout).read_text(encoding="utf-8")).get("at") or 0)
    except (OSError, ValueError, AttributeError):
        return 0.0


def maybe(layout: Layout, after_checkpoint: bool) -> dict | None:
    """One loop step: commit at once after a checkpoint, otherwise when the interval has passed since the last state
    commit. None when it is not yet time."""
    cfg = settings(layout)
    if not cfg["enabled"]:
        return None
    if not after_checkpoint and _now() - _last(layout) < cfg["interval_minutes"] * 60:
        return None
    out = commit(layout, reason="after a checkpoint" if after_checkpoint else "periodic")
    if out["status"] in ("unchanged", "skipped"):
        _remember(layout, None)                          # checked now: wait a full interval before asking git again
    return out
