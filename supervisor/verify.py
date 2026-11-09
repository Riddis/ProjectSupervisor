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
                         "python -m unittest", "ruff check", "ruff",
                         "python -m ruff", "python3 -m ruff", "py -m ruff"]
# Volatile or oversized trees the repo-wide scan prunes: the supervisor's own run dirs and task store, the venv,
# tool caches, the git object store and the (multi-GB) model cache.
EXCLUDED_DIRS = {".git", ".venv", ".ruff_cache", ".pytest_cache", "__pycache__", "node_modules", "runs", "state",
                 "models", "secrets", "uploads", ".idea", ".vs"}
# Rendered views (supervisor.render): the supervisor rewrites these every iteration, so they are never evidence about
# what an agent wrote.
EXCLUDED_FILES = {"STATE.md", "TASKS.md", "ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md"}
LARGE_FILE_BYTES = 32 * 1024 * 1024
# The supervisor's own writes into the working tree (the Control Center's Settings edits to config/routing.yaml), one
# JSON line each under state/runtime/. The owner changes settings while agents run; before 2.1.1 the next
# verification counted that edit as the running delivery's undeclared write, and the retry reverted it.
OWN_WRITES = "own_writes.jsonl"


def record_own_write(layout: Layout, rel: str, before: bytes | None, after: bytes, actor: str, why: str = "") -> None:
    """Record that the supervisor replaces root-relative `rel`, holding `before` (None: absent), with `after`. Called
    BEFORE the write: a crash between the two leaves a transition nothing took, never a supervisor write that
    verification blames on an agent."""
    import hashlib
    import json

    from .store import now_iso
    entry = {"ts": now_iso(), "path": rel, "before": hashlib.sha256(before).hexdigest() if before is not None else None,
             "after": hashlib.sha256(after).hexdigest(), "actor": actor, "why": why}
    p = layout.runtime / OWN_WRITES
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def own_transitions(layout: Layout) -> dict[str, set[tuple[str | None, str]]]:
    """path -> {(sha256 before, sha256 after)} for every write record_own_write() recorded. An unreadable line is
    skipped: it explains nothing, so the change it would have explained still fails verification."""
    import json
    out: dict[str, set[tuple[str | None, str]]] = {}
    p = layout.runtime / OWN_WRITES
    try:
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
            out.setdefault(str(e["path"]), set()).add((e.get("before"), str(e["after"])))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _supervisor_wrote(before: dict | None, after: dict | None, edges: set[tuple[str | None, str]]) -> bool:
    """True when recorded supervisor writes alone take a path from its snapshot content (`before`; absent counts) to
    its content now (`after`). An agent's edit anywhere in between breaks the chain, so the change stays its own."""
    b, a = before or {}, after or {}
    if not edges or a.get("kind") != "file" or b.get("kind") not in (None, "missing", "file"):
        return False
    start = b.get("sha256") if b.get("kind") == "file" else None
    seen, frontier = {start}, [start]
    while frontier:
        cur = frontier.pop()
        if cur == a.get("sha256"):
            return True
        for src, dst in edges:
            if src == cur and dst not in seen:
                seen.add(dst)
                frontier.append(dst)
    return False


def excluded_files(layout: Layout | None = None) -> set[str]:
    """Root-relative files that are never evidence about what an agent wrote: the built-in rendered views plus
    every projection a project plugin registered (`registry.projection`, supervisor.plugins)."""
    out = set(EXCLUDED_FILES)
    if layout is not None:
        from . import plugins
        out |= plugins.load(layout).projection_paths | plugins.known_projection_paths(layout)
    return out


@dataclass
class Outcome:
    ok: bool
    detail: str
    report: dict = field(default_factory=dict)


def repo_manifest(root: Path, exclude: set[str] = EXCLUDED_DIRS, exclude_files: set[str] | None = None) -> dict:
    """Content manifest of the whole working tree with `exclude` pruned and the rendered views in `exclude_files`
    (default EXCLUDED_FILES) skipped, root-relative POSIX keys. Links are recorded, never followed or hashed; files
    over LARGE_FILE_BYTES are recorded by size+mtime so one dropped checkpoint cannot make verification cost
    minutes."""
    import hashlib
    entries: dict[str, dict] = {}
    root = Path(root)
    skip = EXCLUDED_FILES if exclude_files is None else exclude_files
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude and not d.endswith(".egg-info"))
        here = Path(dirpath)
        for name in sorted(filenames):
            p = here / name
            key = p.relative_to(root).as_posix()
            if key in skip:
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
    out: dict = {"owned": {}, "repo": repo_manifest(layout.root, exclude_files=excluded_files(layout))}
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


def limits(layout: Layout) -> dict:
    """The three test-evidence limits (G3, 2.4.0): `verification.per_command_timeout_s`, `budget_s` and
    `max_commands`, each falling back to this module's own constant when unconfigured or malformed."""
    out = {"per_command_timeout_s": TEST_TIMEOUT_S, "budget_s": TEST_BUDGET_S, "max_commands": MAX_TEST_COMMANDS}
    cfg = layout.config / "routing.yaml"
    if cfg.exists():
        try:
            doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
        v = (doc.get("verification") or {}) if isinstance(doc, dict) else {}
        for key in ("per_command_timeout_s", "budget_s", "max_commands"):
            if key in v:
                try:
                    out[key] = int(v[key])
                except (TypeError, ValueError):
                    pass
    return out


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


def _unquote(t: str) -> str:
    """Strip one pair of matching quotes wrapping the whole of `t`, never an inner one."""
    return t[1:-1] if len(t) > 1 and t[0] == t[-1] in "\"'" else t


def test_argv(command: str) -> list[str]:
    """argv for a declared test command — no shell, quotes stripped (Windows paths keep their backslashes).
    `shlex.split(posix=False)` quotes a whole token (`"a b"` -> one token) but never unquotes one: an option's own
    value after its first `=` (`--basetemp="…"`) survives as one token whose own first character is `-`, not a
    quote, so the old whole-token check never matched it and the inner quotes reached pytest's own argv (N6,
    2.5.0). The part after the first `=` in an option token (one starting with `-`) is unquoted the same way the
    whole token already is."""
    tokens = shlex.split(command, posix=False)
    out = []
    for t in tokens:
        if t.startswith("-") and "=" in t:
            key, _, val = t.partition("=")
            out.append(f"{_unquote(key)}={_unquote(val)}")
        else:
            out.append(_unquote(t))
    return out


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
    """Concurrent pytest runs (two agents, or an agent and this verifier) sharing a --basetemp wipe each other's temp
    dirs on Windows; every run gets its own. (N6, 2.5.0) The vendored handoff text tells every agent to supply its
    own `--basetemp` for its own run — right advice for that run, which stays unchanged — but this is the
    verifier's re-run of a declared command, in the verifier's own process, and an agent-supplied basetemp here
    would point back at the agent's own evidence directory, which pytest clears on use. So this always replaces
    any `--basetemp=X` or `--basetemp X` an agent declared (both forms stripped) rather than only filling one in
    when none is present: the agent's basetemp is that agent's evidence, the verifier's scratch is the verifier's,
    and the verifier's wins every time, unconditionally."""
    if "pytest" not in " ".join(argv[:3]):
        return argv
    cleaned, skip_next = [], False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("--basetemp="):
            continue
        if a == "--basetemp":
            skip_next = True
            continue
        cleaned.append(a)
    parent = root / "runs" / "pytest_tmp"
    parent.mkdir(parents=True, exist_ok=True)               # pytest makes the basetemp, never a missing parent (1.13.0)
    return cleaned + [f"--basetemp={(parent / tag).as_posix()}"]


def _cwd_interpreter(cwd: Path) -> str:
    """The interpreter a command's `cwd` runs under (G2, 2.4.0): that directory's own `.venv` if it has one, else
    the supervisor's own — never whatever the declared command's argv[0] spells."""
    venv_py = cwd / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv_py) if venv_py.exists() else sys.executable


def _resolve_root_relative(p: str, root: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else root / path


def _cwd_allowed(resolved: Path, root: Path, roots: tuple[str, ...]) -> bool:
    """True when `resolved` is the project root itself or lies inside one resolved entry of `roots` (G2)."""
    if resolved == root:
        return True
    for r in roots:
        try:
            base = _resolve_root_relative(r, root).resolve()
        except OSError:
            continue
        if resolved == base or resolved.is_relative_to(base):
            return True
    return False


def run_declared_tests(root: Path, declared: list[dict], allowed: list[str] | None = None, tag: str = "verify",
                       roots: tuple[str, ...] = (), timeout_s: int = TEST_TIMEOUT_S, budget_s: int = TEST_BUDGET_S,
                       max_commands: int = MAX_TEST_COMMANDS) -> tuple[bool, list[dict]]:
    """Re-run each declared command; returns (all passed, observations). Bounded by `max_commands`, `timeout_s`
    per command and `budget_s` overall (G3, 2.4.0 — defaults are the pre-2.4.0 constants). Each entry's own `cwd`,
    when given, must resolve inside `roots` (the task's own `test_roots`, G2, 2.4.0) or the project root itself;
    otherwise the command is refused and never run. The supervisor is single-threaded and the owner's PAUSE/STOP
    cannot be applied while this runs."""
    allowed = allowed if allowed is not None else list(ALLOWED_TEST_COMMANDS)
    root = Path(root).resolve()
    ok, results = True, []
    deadline = time.monotonic() + budget_s
    for entry in declared[:max_commands]:
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
                            "error": "command not in verification.allowed_test_commands; allowed: "
                                     + ", ".join(allowed) + " (config/routing.yaml verification.allowed_test_commands)"})
            ok = False
            continue
        cwd = root
        declared_cwd = (entry or {}).get("cwd") if isinstance(entry, dict) else None
        if declared_cwd:
            try:
                resolved = _resolve_root_relative(str(declared_cwd), root).resolve()
            except OSError:
                resolved = _resolve_root_relative(str(declared_cwd), root)
            if not resolved.is_dir() or not _cwd_allowed(resolved, root, roots):
                results.append({"command": command, "exit_code": None,
                                "error": f"cwd not inside the task's test_roots: {resolved}; allowed: "
                                         + ", ".join([str(root), *roots])})
                ok = False
                continue
            cwd = resolved
        interpreter = _cwd_interpreter(cwd) if declared_cwd else sys.executable
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"command": command, "exit_code": None, "error": "test budget exhausted; not run"})
            ok = False
            continue
        argv = test_argv(command)
        if _basename(argv[0]).lower().removesuffix(".exe") in ("python", "python3", "py"):
            argv[0] = interpreter               # the resolved interpreter, never whatever `python` is on PATH
        elif _basename(argv[0]).lower().removesuffix(".exe") in ("pytest", "ruff"):
            argv = [interpreter, "-m", _basename(argv[0]).lower().removesuffix(".exe")] + argv[1:]
        argv = _isolate_pytest(argv, root, f"{tag}_{int(time.time())}")
        per_command = int(entry.get("timeout_s") or timeout_s) if isinstance(entry, dict) else timeout_s
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=min(per_command, remaining),
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            code = proc.returncode
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            results.append({"command": command, "exit_code": None, "error": str(exc)[:300]})
            ok = False
            continue
        obs = {"command": command, "exit_code": code}
        if declared_cwd:
            obs["cwd"] = str(declared_cwd)
        if code != 0:
            obs["output_tail"] = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-1500:]
        results.append(obs)
        if code != 0:
            ok = False
    if len(declared) > max_commands:
        results.append({"command": f"[{len(declared) - max_commands} further command(s)]", "exit_code": None,
                        "error": f"more than {max_commands} commands declared (verification.max_commands); not run"})
        ok = False
    return ok, results


def _undeclared(before: dict, after: dict, declared: set[str], owned_paths: list[str], root: Path,
               ignore: tuple[str, ...], skip: set[str] = frozenset(),
               own: dict[str, set] | None = None) -> tuple[list[str], list[str]]:
    """(changes inside owned_paths, changes outside it) that the delivery did not declare. `skip` names rendered
    views (excluded_files) — also filtered here, because a projection first learned after the `before` snapshot
    was taken is in that snapshot and not in the next one. `own` (own_transitions) explains a change the
    supervisor's own writes account for."""
    inside, outside = [], []
    for p in changed_paths(before, after):
        if p in declared or p in skip or (ignore and contained(p, list(ignore), root)):
            continue
        if own and _supervisor_wrote(before.get(p), after.get(p), own.get(p) or set()):
            continue
        kinds = {(before.get(p) or {}).get("kind"), (after.get(p) or {}).get("kind")}
        if kinds <= {"directory", "missing", None}:
            continue                    # a folder appearing or going away is no delivery; its files are judged (1.13.0)
        (inside if contained(p, owned_paths, root) else outside).append(p)
    return inside, outside


def undeclared_residue(before: dict, after: dict, outside: list[str]) -> dict[str, dict]:
    """M11(a), first shape (2.5.0): the `before`/`after` manifest entry for each of `_undeclared()`'s own `outside`
    paths — a rejected attempt's writes to paths it did not own. `supervisor.loop` reverts these at the moment the
    attempt is rejected, before the retry dispatches, so there is nothing left to clean up (and be blamed for
    cleaning up). No second definition of "unowned change": `outside` is exactly what `verify()` already computed
    to fail the delivery."""
    return {p: {"before": before.get(p), "after": after.get(p)} for p in outside}


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
        skip = excluded_files(layout)
        own = own_transitions(layout)
        if scoped:
            inside, _ = _undeclared(before.get("owned") or {}, after.get("owned") or {}, set(files), scoped, layout.root,
                                    ignore, skip, own)
            if inside:
                return Outcome(False, "undeclared changes inside owned paths: " + ", ".join(inside))
        inside_repo, outside_repo = _undeclared(before.get("repo") or {}, after.get("repo") or {}, set(files),
                                                scoped, layout.root, ignore, skip, own)
        undeclared = outside_repo if scoped else inside_repo + outside_repo
        if undeclared:
            what = "undeclared writes outside owned paths: " if scoped else "undeclared writes: "
            residue = undeclared_residue(before.get("repo") or {}, after.get("repo") or {}, outside_repo)
            return Outcome(False, what + ", ".join(sorted(undeclared)[:20]),
                           report={"residue": residue} if residue else {})
    checks = list(checks or [])
    if not declared_tests and require_tests:
        if task.evidence == "commands":
            why = "this task requires command evidence (`evidence: commands`)"
        else:
            why = "this task declares tests on a model lane"
        return Outcome(False, f"no test evidence: {why}, so the delivery must report the commands "
                              "it ran as `tests: [{command, exit_code}]` and they must pass when re-run here")
    to_run = checks + [t for t in (declared_tests or [])
                       if not any(str((t or {}).get("command", "")).strip() == c["command"].strip() for c in checks)]
    if to_run:
        fixed = [c["command"] for c in checks]
        if not run_commands:
            return Outcome(True, "artifacts present; commands pending", report={"pending_commands": to_run, "fixed": fixed})
        lim = limits(layout)              # the inline path clamps the two time limits (G3): it holds state/.lock
        ok, results = run_declared_tests(layout.root, to_run, allowed_test_commands(layout), tag=f"verify_{task.id}",
                                         roots=tuple(task.test_roots), timeout_s=min(lim["per_command_timeout_s"], TEST_TIMEOUT_S),
                                         budget_s=min(lim["budget_s"], TEST_BUDGET_S), max_commands=lim["max_commands"])
        return commands_outcome(ok, results, fixed, bool(checks))
    return Outcome(True, "artifacts present" if files else "no artifacts declared")
