"""Accepted-work checkpoints. When a task reaches COMPLETE, the supervisor commits exactly the contents
that were accepted — and nothing else.

Binding: the delivery digest is `digest(manifest(paths))` (loop.ingest). Recomputing that manifest now and comparing
digests proves the bytes on disk are the bytes verification, the reviews and the owner's approval covered. Any
difference refuses the checkpoint (an issue is opened once, the task is annotated, nothing is committed): a changed
accepted file is a fact the owner must know about, never something to paper over with a commit.

Scope of a commit: the accepted paths that git tracks or can track (ignored paths such as runs/ are skipped and
listed), plus the task's own record `state/tasks/<id>.yaml`. `git commit -- <paths>` commits only those paths and
leaves the owner's unrelated staged or unstaged edits untouched. Tasks completed before `checkpoint.since` (routing.yaml)
were accepted without this mechanism and are left alone; the git identity is whatever the repository has configured.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from . import escalation, store, tasks as tasks_mod
from ._vendor.contract import digest as digest_of, manifest
from .models.task import Task, TaskStatus
from .paths import Layout

GIT_TIMEOUT_S = 60


def settings(layout: Layout) -> dict:
    out = {"enabled": True, "since": None}
    cfg = layout.config / "routing.yaml"
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        cp = data.get("checkpoint") or {}
        if "enabled" in cp:
            out["enabled"] = bool(cp["enabled"])
        if cp.get("since"):
            out["since"] = str(cp["since"])
    return out


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)[:300]


run_git = _git            # monkeypatched in tests that must not touch git


def is_repo(root: Path) -> bool:
    code, out = run_git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip().endswith("true")


def completed_at(task: Task) -> str:
    for entry in reversed(task.history):
        if entry.get("to") == TaskStatus.COMPLETE.value:
            return str(entry.get("at") or "")
    return str(task.updated or "")


def pending(layout: Layout, tasks: dict[str, Task] | None = None) -> list[Task]:
    """COMPLETE tasks with a delivery digest, completed on/after `since`, that carry no checkpoint record yet."""
    cfg = settings(layout)
    if not cfg["enabled"]:
        return []
    tasks = tasks or tasks_mod.load_all(layout)
    out = []
    for t in tasks.values():
        if t.status != TaskStatus.COMPLETE or not t.delivery.digest:
            continue
        if (t.model_extra or {}).get("checkpoint"):
            continue
        if cfg["since"] and completed_at(t) < cfg["since"]:
            continue
        out.append(t)
    return sorted(out, key=lambda t: completed_at(t))


def _record(layout: Layout, task_id: str, **fields) -> None:
    def fn(doc):
        doc["checkpoint"] = {"at": store.now_iso(), **fields}
    tasks_mod._update(layout, task_id, fn)


def _ignored(root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    code, out = run_git(root, "check-ignore", "--", *paths)
    if code not in (0, 1):               # 0: some ignored, 1: none ignored, 128: error → treat none as ignored
        return set()
    return {line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()}


def checkpoint(layout: Layout, task: Task) -> dict:
    """Commit the accepted contents of one COMPLETE task, or refuse. Always records the outcome on the task so it is
    attempted exactly once (a refused checkpoint is re-attempted only when the owner clears `checkpoint`)."""
    root = layout.root
    paths = sorted(set(task.paths))
    if not paths:
        _record(layout, task.id, status="skipped", reason="no delivery paths")
        return {"status": "skipped", "reason": "no delivery paths"}
    try:
        current = digest_of(manifest([root / p for p in paths], root=root))
    except (ValueError, OSError) as exc:
        current = f"error: {exc}"
    if current != task.delivery.digest:
        detail = (f"{task.id} was accepted on digest {task.delivery.digest[:12]} but its delivered paths now hash to "
                  f"{current[:12]}: the accepted contents changed after acceptance. Nothing was committed. Paths: {', '.join(paths[:20])}")
        sig = escalation.signature(f"checkpoint:{task.id}:{task.delivery.digest}")
        if escalation.find_issue(layout, task.id, sig) is None:
            escalation.open_issue(layout, title=f"{task.id}: accepted contents changed before checkpoint", severity="high",
                                  owner_role="owner", detail=detail, resolution_task=task.id, signature=sig)
        _record(layout, task.id, status="refused", reason="digest mismatch", expected=task.delivery.digest, found=current)
        tasks_mod.add_note(layout, task.id, "checkpoint refused: accepted contents changed (see issue)", actor="checkpoint")
        tasks_mod.log(layout, "checkpoint_refused", "checkpoint", task=task.id, expected=task.delivery.digest, found=current)
        return {"status": "refused", "reason": "digest mismatch"}
    if not is_repo(root):
        _record(layout, task.id, status="skipped", reason="not a git repository")
        tasks_mod.log(layout, "checkpoint_skipped", "checkpoint", task=task.id, reason="not a git repository")
        return {"status": "skipped", "reason": "not a git repository"}
    ignored = _ignored(root, paths)
    committable = [p for p in paths if p not in ignored and (root / p).exists()]
    record_path = tasks_mod.path_for(layout, task.id).relative_to(root).as_posix()
    if not committable:
        _record(layout, task.id, status="skipped", reason="every delivered path is git-ignored", ignored=sorted(ignored))
        tasks_mod.log(layout, "checkpoint_skipped", "checkpoint", task=task.id, reason="all paths ignored", ignored=sorted(ignored))
        return {"status": "skipped", "reason": "all paths ignored"}
    # Record the checkpoint on the task BEFORE committing so the task record inside the commit says it was checkpointed,
    # and a crash between record and commit leaves a record that names no commit (re-run attempts nothing: status set).
    _record(layout, task.id, status="committing", paths=committable, ignored=sorted(ignored), digest=task.delivery.digest)
    code, out = run_git(root, "add", "--", *committable, record_path)
    if code != 0:
        _record(layout, task.id, status="failed", reason=f"git add: {out[:300]}")
        tasks_mod.log(layout, "checkpoint_failed", "checkpoint", task=task.id, error=out[:300])
        return {"status": "failed", "reason": out[:300]}
    message = (f"{task.id}: {task.title} (delivery {task.delivery.digest[:12]})\n\n"
               f"Accepted checkpoint by the supervisor: verification, required reviews and any owner approval "
               f"all cover digest {task.delivery.digest}.\n" + "".join(f"- {p}\n" for p in committable)
               + (f"\nNot committed (git-ignored): {', '.join(sorted(ignored))}\n" if ignored else ""))
    code, out = run_git(root, "commit", "-q", "-m", message, "--", *committable, record_path)
    if code != 0:
        if "nothing to commit" in out or "no changes added" in out:
            code2, head = run_git(root, "rev-parse", "HEAD")
            _record(layout, task.id, status="unchanged", reason="already committed", commit=head if code2 == 0 else None,
                    paths=committable, digest=task.delivery.digest)
            tasks_mod.log(layout, "checkpoint_unchanged", "checkpoint", task=task.id)
            return {"status": "unchanged"}
        _record(layout, task.id, status="failed", reason=f"git commit: {out[:300]}", paths=committable)
        tasks_mod.log(layout, "checkpoint_failed", "checkpoint", task=task.id, error=out[:300])
        return {"status": "failed", "reason": out[:300]}
    code, head = run_git(root, "rev-parse", "HEAD")
    commit = head.strip() if code == 0 else None
    _record(layout, task.id, status="committed", commit=commit, paths=committable, ignored=sorted(ignored), digest=task.delivery.digest)
    tasks_mod.log(layout, "checkpoint_committed", "checkpoint", task=task.id, commit=commit, paths=committable)
    return {"status": "committed", "commit": commit, "paths": committable}


def run(layout: Layout, tasks: dict[str, Task] | None = None, limit: int = 3) -> list[dict]:
    """One loop step: checkpoint up to `limit` pending tasks (git is slow on a large tree; the loop must stay responsive)."""
    out = []
    for t in pending(layout, tasks)[:limit]:
        try:
            out.append({"task": t.id, **checkpoint(layout, t)})
        except Exception as exc:          # noqa: BLE001 - a checkpoint problem must never kill the loop
            tasks_mod.log(layout, "checkpoint_error", "checkpoint", task=t.id, error=str(exc)[:300])
            try:
                _record(layout, t.id, status="failed", reason=str(exc)[:300])
            except Exception:             # noqa: BLE001
                pass
    return out
