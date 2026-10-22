"""Deterministic file primitives: atomic writes, hashing manifests, structured-result extraction, process cleanup.

Vendored from an earlier supervisor's `workflow_contract.py` (see package docstring). Generic parts only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable

# Windows refuses a replace while another process holds the target open (sharing violation,
# PermissionError / winerror 5 or 32). Readers hold files briefly, so a bounded backoff (~4 s)
# rides that out instead of crashing a long-lived process.
_REPLACE_ATTEMPTS = 25


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _transient_replace_error(exc, temporary):
    # 5/32/33: sharing violations (AV scanners, concurrent replace). 2: Windows MoveFileEx can report
    # FILE_NOT_FOUND while another writer is replacing the same target; retry as long as our temp file exists.
    code = getattr(exc, "winerror", None)
    return isinstance(exc, PermissionError) or code in (5, 32, 33) or (code == 2 and temporary.exists())


def atomic_write_text(path, text):
    """Write via a unique temporary file and an atomic replace, retrying lock conflicts.

    Prevents torn files. It does NOT prevent lost updates — that is the store's job (rev + lock).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) == 2 and not temporary.exists():
                    # Windows MoveFileEx under concurrent replacers of the same target: our temp is gone. Either the
                    # move landed before the error was reported, or we lost the race to another whole-file writer —
                    # both leave an untorn file, which is all this function promises (lost updates are the store's job).
                    if path.exists():
                        return
                    temporary.write_text(text, encoding="utf-8")       # target vanished too: rewrite and retry
                elif attempt == _REPLACE_ATTEMPTS - 1 or not _transient_replace_error(exc, temporary):
                    raise
                time.sleep(min(0.4, 0.02 * (attempt + 1)))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_json(path, data):
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=False))


def status_token(text):
    """Leading status keyword of a field: 'BLOCKED (reason)' -> 'BLOCKED'. Defensive; the supervisor's own
    state uses strict enums and never needs this, but external/legacy text sometimes does."""
    clean = (text or "").strip().lstrip("*`").strip()
    match = re.match(r"[A-Z][A-Z_]*", clean)
    return match.group(0) if match else clean


# ---------------------------------------------------------------- paths and content identity

def resolve(value, root: Path | None = None) -> Path:
    """Absolute, link-free path. Relative values resolve against `root` (or the cwd)."""
    path = Path(value)
    if not path.is_absolute():
        path = (root or Path.cwd()) / path
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"Paths must not traverse links: {parent}")
    return path.resolve()


def overlaps(first, second, root=None):
    first, second = resolve(first, root), resolve(second, root)
    return first == second or first in second.parents or second in first.parents


def contained(path, roots, root=None):
    path = resolve(path, root)
    return any(path == r or r in path.parents for r in (resolve(x, root) for x in roots))


def manifest(paths: Iterable, *, require_exists=False, root=None) -> dict:
    """sha256 manifest of files under `paths`; includes directories, empty dirs and missing paths; rejects links.

    With `root`, keys are root-relative POSIX paths (so digests survive moving the repository between machines or
    drives) and a path outside the root is an error. Without `root`, keys are absolute (containment checks only)."""
    entries = {}
    root_resolved = resolve(root) if root else None

    def key_of(path: Path) -> str:
        if root_resolved is None:
            return str(path)
        try:
            return path.relative_to(root_resolved).as_posix()
        except ValueError as exc:
            raise ValueError(f"manifest path escapes root: {path}") from exc

    def visit(path: Path):
        key = key_of(path)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Frozen material must not contain links: {path}")
        if not path.exists():
            if require_exists:
                raise ValueError(f"Missing frozen material: {path}")
            entries[key] = {"kind": "missing"}
        elif path.is_dir():
            entries[key] = {"kind": "directory"}
            for child in sorted(path.iterdir(), key=lambda item: str(item).casefold()):
                visit(child)
        elif path.is_file():
            h = hashlib.sha256()
            before = path.stat()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f"Material changed while hashing: {path}")
            entries[key] = {"kind": "file", "size": after.st_size, "sha256": h.hexdigest()}
        else:
            raise ValueError(f"Unsupported frozen material: {path}")

    for r in sorted({resolve(p, root) for p in paths}, key=str):
        visit(r)
    return entries


def digest(entries: dict) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def changed_paths(before: dict, after: dict) -> list:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


# ---------------------------------------------------------------- structured results

def _embedded_objects(text):
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            yield value
        index = text.find("{", end)


def _unwrap_kind(value, kind):
    """Accept `{"delivery": {...}}` (Codex answers do this) and a lone fenced object without `kind`: normalise to the
    canonical shape so a well-formed result is never rejected on wrapper style alone."""
    if isinstance(value, dict) and set(value) == {kind} and isinstance(value[kind], dict):
        value = dict(value[kind])
        value.setdefault("kind", kind)
    elif isinstance(value, dict) and "kind" not in value and "schema_version" in value and ("status" in value or "verdict" in value):
        value = dict(value); value["kind"] = kind
    return value


def result_object(text, kind, schema_version=1):
    """The agent's structured result: exactly one distinct `kind` object (fenced preferred, bare accepted).

    Repeating the identical object is fine; two different objects of the same kind are ambiguous and
    rejected, so a quoted draft can never flip a verdict.
    """
    text = text or ""
    fenced = re.findall(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", text, re.DOTALL)
    values = []
    for candidate in fenced:
        try:
            value = json.loads(candidate.strip())
        except (TypeError, json.JSONDecodeError):
            continue
        value = _unwrap_kind(value, kind)
        if isinstance(value, dict) and value.get("kind") == kind:
            values.append(value)
    if not values:
        values = [v for v in (_unwrap_kind(o, kind) for o in _embedded_objects(text)) if isinstance(v, dict) and v.get("kind") == kind]
    distinct = {json.dumps(v, sort_keys=True): v for v in values if v.get("schema_version") == schema_version}
    if len(distinct) != 1:
        raise ValueError(f"Expected exactly one schema_version {schema_version} {kind} JSON result, found {len(distinct)}")
    return next(iter(distinct.values()))


REVIEW_VERDICTS = ("ACCEPT", "REVISE", "REJECT", "BLOCKED")
FINDING_CATEGORIES = {"correctness", "requirement", "regression", "architecture", "quality", "advisory"}


def review_result(text):
    value = result_object(text, "review")
    if value.get("verdict") not in REVIEW_VERDICTS:
        raise ValueError("Invalid review verdict")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Review findings must be a list")
    for f in findings:
        if (not isinstance(f, dict) or f.get("category") not in FINDING_CATEGORIES
                or not isinstance(f.get("blocking"), bool) or not f.get("evidence") or not f.get("message")):
            raise ValueError("Each finding needs category, blocking, message and evidence")
        if f["category"] == "advisory" and f["blocking"] and value["verdict"] != "BLOCKED":
            raise ValueError("Advisory findings cannot block")
    if value["verdict"] == "ACCEPT" and any(f["blocking"] for f in findings):
        raise ValueError("Accepted review contains blocking findings")
    return value


def structured_result(text, kind, schema_version=1):
    """result_object(), except a `review` kind is also checked against review_result()'s contract (verdict enum,
    finding shape, the ACCEPT/advisory cross-field rules) — a reviewer's self-contradictory verdict (an ACCEPT
    carrying a blocking finding) must never reach the task store as a recorded verdict (review round 3, code_review)."""
    if kind == "review":
        return review_result(text)
    return result_object(text, kind, schema_version)


# ---------------------------------------------------------------- exceptions and process cleanup

class DeterministicCheckFailed(ValueError):
    """A declared deterministic gate rejected the work (not an infrastructure fault)."""

    def __init__(self, check, detail=""):
        super().__init__(f"{check}: {detail}" if detail else check)
        self.check, self.detail = check, detail


class CleanupRequired(RuntimeError):
    def __init__(self, pid, message):
        super().__init__(message)
        self.pid = pid


class CapacityDeferred(RuntimeError):
    """No model was launched: the measured capacity reserve gates this job (costs no attempt)."""

    def __init__(self, reset_epoch, message):
        super().__init__(message)
        self.reset_epoch = reset_epoch


def terminate_owned(process):
    """Terminate a child process tree; never report quiescence unless termination and wait both succeed."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=15, check=True)
            except subprocess.CalledProcessError:
                process.wait(timeout=15)
                return
        else:
            process.kill()
        process.wait(timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanupRequired(process.pid, f"Owned process {process.pid} cleanup unresolved: {exc}") from exc
