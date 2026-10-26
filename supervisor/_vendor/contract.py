"""Deterministic file primitives: atomic writes, hashing manifests, structured-result extraction, process cleanup.

Vendored from an earlier supervisor's `workflow_contract.py` (see package docstring). Generic parts only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
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


class CannotKeep(PermissionError):
    """atomic_write_text(keep=True) could not give the new file what the old one had (owner, group, permissions,
    links). Nothing was replaced: the old file is exactly as it was."""


def _windows_keep_access(source, target):
    """Give `target` the DACL of `source`, its protection from inheritance included, and its owner and group where they
    differ (setting an owner other than oneself needs a privilege, so that fails and the caller refuses)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = advapi32.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD)]
    get_security.restype = wintypes.BOOL
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    get_control.restype = wintypes.BOOL
    sid_getters = (advapi32.GetSecurityDescriptorOwner, advapi32.GetSecurityDescriptorGroup)
    for getter in sid_getters:
        getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        getter.restype = wintypes.BOOL
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal_sid.restype = wintypes.BOOL
    set_control = advapi32.SetSecurityDescriptorControl
    set_control.argtypes = [ctypes.c_void_p, wintypes.WORD, wintypes.WORD]
    set_control.restype = wintypes.BOOL
    OWNER, GROUP, DACL = 0x1, 0x2, 0x4
    PROTECTED_DACL, UNPROTECTED_DACL, SE_DACL_PROTECTED = 0x80000000, 0x20000000, 0x1000
    SE_DACL_AUTO_INHERITED, SE_DACL_AUTO_INHERIT_REQ = 0x0400, 0x0100

    def check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def descriptor(path):
        needed = wintypes.DWORD(0)
        get_security(str(path), OWNER | GROUP | DACL, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        check(get_security(str(path), OWNER | GROUP | DACL, buffer, needed, ctypes.byref(needed)))
        return buffer

    def sid(buffer, getter):
        value, defaulted = ctypes.c_void_p(), wintypes.BOOL()
        check(getter(buffer, ctypes.byref(value), ctypes.byref(defaulted)))
        return value

    old, new = descriptor(source), descriptor(target)
    control, revision = wintypes.WORD(), wintypes.DWORD()
    check(get_control(old, ctypes.byref(control), ctypes.byref(revision)))
    info = DACL | (PROTECTED_DACL if control.value & SE_DACL_PROTECTED else UNPROTECTED_DACL)
    if control.value & SE_DACL_AUTO_INHERITED:              # SetFileSecurity keeps that bit only when asked to
        check(set_control(old, SE_DACL_AUTO_INHERIT_REQ, SE_DACL_AUTO_INHERIT_REQ))
    for flag, getter in zip((OWNER, GROUP), sid_getters):
        if not equal_sid(sid(old, getter), sid(new, getter)):
            info |= flag                                   # an unchanged owner or group is not rewritten
    check(set_security(str(target), info, old))


def _windows_private_temporary(path):
    """Create a new, empty temporary file beside `path` whose DACL is set in the same call that creates it
    (CreateFileW with a security descriptor) and grants only its owner (OWNER RIGHTS, protected from inheritance), so
    no other account can open it at any moment, with or without content. Returns (fd, name), like mkstemp."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    to_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    to_descriptor.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                              ctypes.POINTER(wintypes.ULONG)]
    to_descriptor.restype = wintypes.BOOL
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    GENERIC_WRITE, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, ERROR_FILE_EXISTS = 0x40000000, 1, 0x80, 80
    invalid = ctypes.c_void_p(-1).value
    descriptor = ctypes.c_void_p()
    if not to_descriptor("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        for _ in range(100):
            name = os.path.join(str(Path(path).parent), f".{Path(path).name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
            handle = create_file(name, GENERIC_WRITE, 0, ctypes.byref(attributes), CREATE_NEW, FILE_ATTRIBUTE_NORMAL,
                                 None)
            if handle not in (None, invalid):
                return msvcrt.open_osfhandle(handle, os.O_WRONLY), name   # binary fd; the fd now owns the handle
            error = ctypes.get_last_error()
            if error != ERROR_FILE_EXISTS:
                raise ctypes.WinError(error)
        raise FileExistsError(f"no free temporary name beside {path}")
    finally:
        kernel32.LocalFree(descriptor)


def _keep_access(source, target):
    """Give `target` the owner, group and permissions of `source`: on POSIX its uid, gid and mode bits; on Windows its
    DACL (protection included), owner and group. Raises OSError when any of it cannot be set."""
    if os.name == "nt":
        _windows_keep_access(source, target)
        return
    st = os.stat(source)
    os.chown(target, st.st_uid, st.st_gid)                 # a different owner, or a group one is not in: EPERM
    os.chmod(target, stat.S_IMODE(st.st_mode))             # after chown, which may clear set-id bits


def atomic_write_text(path, text, newline=None, keep=False):
    """Write via a unique temporary file and an atomic replace, retrying lock conflicts.

    Prevents torn files. It does NOT prevent lost updates — that is the store's job (rev + lock).
    `newline` is open()'s: None writes "\\n" as the platform's line ending, "" writes `text` exactly as given.
    `keep=True` makes the new file keep what an existing one had: its owner, group and permissions are carried onto
    the temporary file before it replaces the old one, a symlink is followed (its target is replaced, the link stays),
    and a file with several hard links is not replaced. When any of that is impossible it raises CannotKeep and the
    old file stays exactly as it was. The default leaves the new file with the directory's defaults, as before.
    """
    path = Path(path)
    if keep and path.is_symlink():
        path = Path(os.path.realpath(path))
    if keep and path.exists() and os.stat(path).st_nlink > 1:
        raise CannotKeep(f"{path}: has {os.stat(path).st_nlink} hard links, which replacing it would split; "
                         f"remove the extra links or rewrite it in place by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp: the OS guarantees a fresh name (O_EXCL). A PID + clock name is not unique on Windows, where the clock
    # ticks coarsely enough for two threads to pick the same name and one to hit the other's open handle.
    # With keep=True on Windows the temporary file is private from creation instead (mkstemp's 0600 is a POSIX mode,
    # not a DACL there): it holds the new content before the old file's access is carried over (review 1.10.1 F5).
    if keep and os.name == "nt":
        fd, name = _windows_private_temporary(path)
    else:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
        if keep and path.exists():
            try:
                _keep_access(path, temporary)
            except OSError as exc:
                raise CannotKeep(f"{path}: the rewritten file could not keep its owner, group and permissions "
                                 f"({exc}); nothing was replaced. Rewrite it as the account that owns it") from exc
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
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
                    if keep:                               # the old file, and what it had, are gone: nothing to keep
                        raise CannotKeep(f"{path}: replaced or removed by another writer during the rewrite")
                    temporary.write_text(text, encoding="utf-8", newline=newline)  # target vanished too: rewrite, retry
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
# Near-misses reviewers actually write ("acceptance", "requirements", "bug") mapped onto the canonical set, so a
# real verdict is never discarded over a category spelling. Anything else is still rejected.
CATEGORY_ALIASES = {"requirements": "requirement", "acceptance": "requirement", "acceptance_criteria": "requirement",
                    "spec": "requirement", "scope": "requirement", "completeness": "requirement",
                    "bug": "correctness", "bugs": "correctness", "correct": "correctness",
                    "regressions": "regression", "design": "architecture", "invariant": "architecture",
                    "maintainability": "quality", "style": "quality", "tests": "quality", "testing": "quality",
                    "docs": "quality", "documentation": "quality", "nit": "advisory", "suggestion": "advisory"}


def normalize_category(value):
    """A finding category as the contract spells it: canonical names pass, known aliases are mapped, anything
    else is returned unchanged (and then refused by review_result)."""
    if not isinstance(value, str):
        return value
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    return key if key in FINDING_CATEGORIES else CATEGORY_ALIASES.get(key, value)


def review_result(text):
    value = result_object(text, "review")
    if value.get("verdict") not in REVIEW_VERDICTS:
        raise ValueError("Invalid review verdict")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Review findings must be a list")
    for f in findings:
        if isinstance(f, dict) and "category" in f:
            f["category"] = normalize_category(f["category"])
        if (not isinstance(f, dict) or f.get("category") not in FINDING_CATEGORIES
                or not isinstance(f.get("blocking"), bool) or not f.get("evidence") or not f.get("message")):
            # Descriptive on purpose: claude_cli.follow_up quotes this back to the model, which then fixes exactly
            # what is named instead of guessing.
            raise ValueError("Each finding needs category (one of " + ", ".join(sorted(FINDING_CATEGORIES))
                             + "), blocking (true/false), message and evidence; offending finding: "
                             + json.dumps(f, ensure_ascii=False)[:300])
        if f["category"] == "advisory" and f["blocking"] and value["verdict"] != "BLOCKED":
            raise ValueError("Advisory findings cannot block")
    if value["verdict"] == "ACCEPT" and any(f["blocking"] for f in findings):
        raise ValueError("Accepted review contains blocking findings")
    return value


def structured_result(text, kind, schema_version=1):
    """result_object(), except a `review` kind is also checked against review_result()'s contract (verdict enum,
    finding shape, the ACCEPT/advisory cross-field rules) — a reviewer's self-contradictory verdict (an ACCEPT
    carrying a blocking finding) must never reach the task store as a recorded verdict."""
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


class LaneUnavailable(CapacityDeferred):
    """No model was launched: this lane cannot serve any job until `reset_epoch` (its prepaid balance is spent, say).
    A delivery the scheduler moved here may run on the lane it came from instead; anything else waits, as for
    CapacityDeferred, which every handler of that keeps doing for this subclass."""


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
