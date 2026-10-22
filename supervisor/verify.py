"""Deterministic post-delivery verification. `verify()` produces an Outcome bound to one artifact digest;
it never changes task status itself — only supervisor.tasks.record_verification() writes it into the task store, and only
tasks.advance() may move the task from it.

What is actually checked, and what is not:
  * every declared artifact exists — the task's own list is the floor, a worker can only add to it (loop.ingest);
  * every declared artifact lies inside the task's scoped owned_paths (no restriction in exclusive mode, `["."]`,
    where the whole tree is the task's to touch — but then nothing else writes concurrently, supervisor.gate);
  * no undeclared change inside the scoped owned_paths (sha256 manifest before/after);
  * no change anywhere else in the working tree (repo_manifest before/after) — for EVERY writing job, exclusive or
    scoped, whenever a pre-dispatch snapshot exists — except the volatile directories in EXCLUDED_DIRS, the `ignore`
    roots a caller passes for work running concurrently, and files above LARGE_FILE_BYTES which are compared by
    size+mtime instead of content. An agent can therefore still hide a write under runs/, state/, models/ or .venv —
    those are the supervisor's own scratch, the task store, the model cache and the environment, all of which
    legitimately change while an attempt runs;
  * the task's fixed `checks` re-run here with exit 0 (always, whatever the worker reported), then the worker's
    reported tests, all from allow-listed commands, inside a bounded time budget.

A self-reported exit code is not a success marker: run_declared_tests() re-executes every command the delivery claims
to have run and trusts the observed exit code, never the agent's. The commands come from model output, so they are
matched against an allow-list (config/routing.yaml `verification.allowed_test_commands`) and executed as an argv list,
never through a shell. A command outside the allow-list is a verification failure, not a silent
skip — the same for a blank or malformed entry (code_review #3)."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ._vendor.contract import changed_paths, contained, manifest
from .models.task import Task
from .paths import Layout

TEST_TIMEOUT_S = 300          # per command
TEST_BUDGET_S = 900           # total for one verification: the loop is single-threaded and holds state/.lock
MAX_TEST_COMMANDS = 10
ALLOWED_TEST_COMMANDS = ["pytest", "python -m pytest", "python3 -m pytest", "py -m pytest",
                         "python -m unittest", "ruff check", "ruff"]
# Volatile or oversized trees the repo-wide scan prunes: the supervisor's own run dirs and task store, the venv,
# tool caches, the git object store and the (multi-GB) model cache.
EXCLUDED_DIRS = {".git", ".venv", ".ruff_cache", ".pytest_cache", "__pycache__", "node_modules", "runs", "state",
                 "models", "secrets", "uploads", ".idea", ".vs"}
# Rendered views (supervisor.render): the supervisor rewrites these every iteration, so they are never evidence about
# what an agent wrote.
EXCLUDED_FILES = {"STATE.md", "TASKS.md", "ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md"}
LARGE_FILE_BYTES = 32 * 1024 * 1024


@dataclass
class Outcome:
    ok: bool
    detail: str
    report: dict = field(default_factory=dict)


def repo_manifest(root: Path, exclude: set[str] = EXCLUDED_DIRS) -> dict:
    """Content manifest of the whole working tree with `exclude` pruned, root-relative POSIX keys. Links are
    recorded, never followed or hashed; files over LARGE_FILE_BYTES are recorded by size+mtime so one dropped
    checkpoint cannot make verification cost minutes."""
    import hashlib
    entries: dict[str, dict] = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude and not d.endswith(".egg-info"))
        here = Path(dirpath)
        for name in sorted(filenames):
            p = here / name
            key = p.relative_to(root).as_posix()
            if key in EXCLUDED_FILES:
                continue
            try:
                if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                    entries[key] = {"kind": "link"}
                    continue
                st = p.stat()
                if st.st_size > LARGE_FILE_BYTES:
                    entries[key] = {"kind": "large", "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                    continue
                h = hashlib.sha256()
                with p.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(chunk)
                entries[key] = {"kind": "file", "size": st.st_size, "sha256": h.hexdigest()}
            except OSError as exc:                 # a file being written/deleted under us is not a verdict
                entries[key] = {"kind": "unreadable", "error": str(exc)[:120]}
    return entries


def snapshot(layout: Layout, task: Task) -> dict:
    """Pre-dispatch state: the sha256 manifest of the task's owned paths plus a working-tree manifest. A manifest
    error (a link or a path escaping the root inside owned_paths) is recorded, not raised: it is a verification
    failure, and raising here would kill the dispatch that is taking the snapshot."""
    out: dict = {"owned": {}, "repo": repo_manifest(layout.root)}
    if task.scoped_paths():                       # exclusive mode owns the tree: the repo manifest is its manifest
        try:
            out["owned"] = manifest(task.scoped_paths(), root=layout.root)
        except ValueError as exc:
            out["error"] = f"owned_paths manifest: {exc}"
    return out


def containment_violations(paths: list[str], owned_paths: list[str], root: Path) -> list[str]:
    """Declared paths lying outside every owned root. No restriction without scoped roots (exclusive mode or a
    deterministic job that owns nothing) — the repo-wide scan still applies to those."""
    if not owned_paths:
        return []
    return [p for p in paths if not contained(p, owned_paths, root)]


def allowed_test_commands(layout: Layout) -> list[str]:
    cfg = layout.config / "routing.yaml"
    if cfg.exists():
        try:
            doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
        configured = (doc.get("verification") or {}).get("allowed_test_commands")
        if configured:
            return [str(c) for c in configured]
    return list(ALLOWED_TEST_COMMANDS)


def test_argv(command: str) -> list[str]:
    """argv for a declared test command — no shell, quotes stripped (Windows paths keep their backslashes)."""
    tokens = shlex.split(command, posix=False)
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] in "\"'" else t for t in tokens]


def _basename(path: str) -> str:
    """Last path component for either separator (a Windows path must parse the same on the Linux test VM)."""
    return re.split(r"[\\/]", path)[-1]


def permitted(command: str, allowed: list[str]) -> bool:
    """True when the command's argv matches an allow-listed prefix, with the interpreter compared by basename so
    `.venv/Scripts/python.exe -m pytest tests` and `python -m pytest tests` are the same lane."""
    argv = test_argv(command)
    if not argv or any(re.search(r"[;&|<>`$]", a) for a in argv):   # no shell operators, even though no shell runs it
        return False
    norm = [_basename(argv[0]).lower()] + [a.lower() for a in argv[1:]]
    if norm[0].endswith(".exe"):
        norm[0] = norm[0][:-4]
    for entry in allowed:
        want = [w.lower() for w in test_argv(entry)]
        if want and norm[:len(want)] == want:
            return True
    return False


def _isolate_pytest(argv: list[str], root: Path, tag: str) -> list[str]:
    """Concurrent pytest runs (two agents, or an agent and this verifier) sharing pyproject's --basetemp wipe each
    other's temp dirs on Windows; give every run its own basetemp."""
    if "pytest" in " ".join(argv[:3]) and not any(a.startswith("--basetemp") for a in argv):
        argv = argv + [f"--basetemp={(root / 'runs' / 'pytest_tmp' / tag).as_posix()}"]
    return argv


def run_declared_tests(root: Path, declared: list[dict], allowed: list[str] | None = None, tag: str = "verify") -> tuple[bool, list[dict]]:
    """Re-run each declared command; returns (all passed, observations). Bounded by MAX_TEST_COMMANDS,
    TEST_TIMEOUT_S per command and TEST_BUDGET_S overall — the supervisor is single-threaded and the owner's
    PAUSE/STOP cannot be applied while this runs."""
    allowed = allowed if allowed is not None else list(ALLOWED_TEST_COMMANDS)
    ok, results = True, []
    deadline = time.monotonic() + TEST_BUDGET_S
    for entry in declared[:MAX_TEST_COMMANDS]:
        command = str((entry or {}).get("command", "")).strip() if isinstance(entry, dict) else ""
        if not command:
            results.append({"command": "", "exit_code": None, "error": "malformed test entry: no command"})
            ok = False
            continue
        try:
            allowed_here = permitted(command, allowed)
        except ValueError as exc:               # shlex chokes on an unclosed quote: a failed observation, not a crash
            results.append({"command": command, "exit_code": None, "error": f"unparsable command: {exc}"[:300]})
            ok = False
            continue
        if not allowed_here:
            results.append({"command": command, "exit_code": None,
                            "error": "command not in verification.allowed_test_commands"})
            ok = False
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"command": command, "exit_code": None, "error": "test budget exhausted; not run"})
            ok = False
            continue
        argv = test_argv(command)
        if _basename(argv[0]).lower().removesuffix(".exe") in ("python", "python3", "py"):
            argv[0] = sys.executable            # the supervisor's own interpreter, never whatever `python` is on PATH
        elif _basename(argv[0]).lower().removesuffix(".exe") in ("pytest", "ruff"):
            argv = [sys.executable, "-m", _basename(argv[0]).lower().removesuffix(".exe")] + argv[1:]
        argv = _isolate_pytest(argv, root, f"{tag}_{int(time.time())}")
        per_command = int(entry.get("timeout_s") or TEST_TIMEOUT_S) if isinstance(entry, dict) else TEST_TIMEOUT_S
        try:
            proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=min(per_command, remaining),
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            code = proc.returncode
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            results.append({"command": command, "exit_code": None, "error": str(exc)[:300]})
            ok = False
            continue
        obs = {"command": command, "exit_code": code}
        if code != 0:
            obs["output_tail"] = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-1500:]
        results.append(obs)
        if code != 0:
            ok = False
    if len(declared) > MAX_TEST_COMMANDS:
        results.append({"command": f"[{len(declared) - MAX_TEST_COMMANDS} further command(s)]", "exit_code": None,
                        "error": f"more than MAX_TEST_COMMANDS={MAX_TEST_COMMANDS} declared; not run"})
        ok = False
    return ok, results


def _undeclared(before: dict, after: dict, declared: set[str], owned_paths: list[str], root: Path,
               ignore: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """(changes inside owned_paths, changes outside it) that the delivery did not declare."""
    inside, outside = [], []
    for p in changed_paths(before, after):
        if p in declared or (ignore and contained(p, list(ignore), root)):
            continue
        (inside if contained(p, owned_paths, root) else outside).append(p)
    return inside, outside


def as_job(layout: Layout) -> bool:
    """run the command part of verification as a tracked process job (default) instead of inside the loop."""
    cfg = layout.config / "routing.yaml"
    if cfg.exists():
        try:
            doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
        v = doc.get("verification_job") or {}
        if "as_job" in v:
            return bool(v["as_job"])
    return True


def commands_outcome(ok: bool, results: list[dict], fixed: list[str], checks_declared: bool) -> Outcome:
    """The Outcome for a finished command run — shared by the inline path and the verify job (supervisor.verify_job)."""
    if not ok:
        failed = [r for r in results if r.get("exit_code") != 0]
        fixed_set = {c.strip() for c in fixed}
        label = "required check failed: " if any(str(r.get("command", "")).strip() in fixed_set for r in failed) else "declared test failed: "
        return Outcome(False, label
                       + "; ".join(f"{r['command']} -> {r.get('exit_code') if r.get('exit_code') is not None else r.get('error')}"
                                   for r in failed),
                       report={"tests": results})
    return Outcome(True, "artifacts present; " + ("required checks and " if checks_declared else "") + "declared tests passed",
                   report={"tests": results})


def verify(layout: Layout, task: Task, files: list[str], declared_tests: list[dict] | None = None,
          before: dict | None = None, require_tests: bool = False, ignore: tuple[str, ...] = (),
          checks: list[dict] | None = None, run_commands: bool = True) -> Outcome:
    """Verify one delivery. `before` is a snapshot() taken before dispatch (present for every writing job); `ignore`
    names roots that other work was writing during the attempt (concurrent jobs), which this delivery is not
    answerable for; `checks` are the task contract's own executable checks, re-run before the worker's evidence.
    With `run_commands=False` the file checks run now and the commands are returned as `report["pending_commands"]`
    for a tracked verify job to execute — the Outcome is then provisional (ok=True, nothing recorded)."""
    missing = [f for f in files if not (layout.root / f).exists()]
    if missing:
        return Outcome(False, "missing artifacts: " + ", ".join(missing))
    scoped = task.scoped_paths()
    violations = containment_violations(files, scoped, layout.root)
    if violations:
        return Outcome(False, "writes outside owned paths: " + ", ".join(violations))
    if before and before.get("error"):
        return Outcome(False, f"pre-dispatch snapshot failed: {before['error']}")
    if before:
        try:
            after = snapshot(layout, task)
        except OSError as exc:
            return Outcome(False, f"post-delivery snapshot failed: {exc}")
        if after.get("error"):
            return Outcome(False, f"post-delivery snapshot failed: {after['error']}")
        if scoped:
            inside, _ = _undeclared(before.get("owned") or {}, after.get("owned") or {}, set(files), scoped, layout.root, ignore)
            if inside:
                return Outcome(False, "undeclared changes inside owned paths: " + ", ".join(inside))
        inside_repo, outside_repo = _undeclared(before.get("repo") or {}, after.get("repo") or {}, set(files),
                                                scoped, layout.root, ignore)
        undeclared = outside_repo if scoped else inside_repo + outside_repo
        if undeclared:
            what = "undeclared writes outside owned paths: " if scoped else "undeclared writes: "
            return Outcome(False, what + ", ".join(sorted(undeclared)[:20]))
    checks = list(checks or [])
    if not declared_tests and require_tests:
        return Outcome(False, "no test evidence: the task declares tests, so the delivery must report the commands "
                              "it ran as `tests: [{command, exit_code}]` and they must pass when re-run here")
    to_run = checks + [t for t in (declared_tests or [])
                       if not any(str((t or {}).get("command", "")).strip() == c["command"].strip() for c in checks)]
    if to_run:
        fixed = [c["command"] for c in checks]
        if not run_commands:
            return Outcome(True, "artifacts present; commands pending", report={"pending_commands": to_run, "fixed": fixed})
        ok, results = run_declared_tests(layout.root, to_run, allowed_test_commands(layout), tag=f"verify_{task.id}")
        return commands_outcome(ok, results, fixed, bool(checks))
    return Outcome(True, "artifacts present" if files else "no artifacts declared")
