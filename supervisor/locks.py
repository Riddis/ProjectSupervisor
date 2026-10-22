"""Exclusive OS-level file lock (single-writer rule, ). Windows (msvcrt) and POSIX (fcntl).

Locks a fixed one-byte region at offset 0 of the lock file (never the fd's current position). Owner info goes to a
`<lock>.owner` sidecar so it can be read while the lock is held. Not re-entrant: re-acquiring a path the current
thread already holds raises LockHeld immediately (never call store.update from inside another update's fn).
"""
from __future__ import annotations

import errno
import json
import os
import threading
import time
from pathlib import Path

if os.name == "nt":  # pragma: no cover - platform specific
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    _CONTENTION = (errno.EACCES, errno.EDEADLK, 33, 36)
else:
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    _CONTENTION = (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)

_held: set[tuple[int, str]] = set()
_held_guard = threading.Lock()
FOREIGN_FRESH_S = 180.0      # a sidecar from another host counts as held while its mtime is younger than this
HOST = os.environ.get("SUPERVISOR_HOST") or __import__("socket").gethostname()


def _sidecar(path: Path) -> Path:
    return Path(path).with_suffix(Path(path).suffix + ".owner")


def foreign_holder(path: Path) -> dict | None:
    """OS byte locks do not cross hosts (a Linux VM on a mounted Windows share takes its own lock unopposed).
    The sidecar carries host+pid and the holder refreshes its mtime (see `touch`); a fresh sidecar from another
    host means the lock is held even though the OS lock would succeed."""
    sc = _sidecar(path)
    try:
        info = json.loads(sc.read_text(encoding="utf-8"))
        age = time.time() - sc.stat().st_mtime
    except (OSError, ValueError):
        return None
    if info.get("host") and info["host"] != HOST and age < FOREIGN_FRESH_S:
        return info
    return None


class LockHeld(RuntimeError):
    pass


class FileLock:
    """`with FileLock(path): ...` — raises LockHeld if another process/thread holds it (after `timeout` seconds)."""

    def __init__(self, path: Path, owner: str = "supervisor", timeout: float = 0.0):
        self.path = Path(path)
        self.owner = owner
        self.timeout = timeout
        self._fh = None
        self._key = (threading.get_ident(), str(self.path.absolute()))

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _held_guard:
            if self._key in _held:
                raise LockHeld(f"{self.path} is already held by this thread (nested update?)")
        deadline = time.monotonic() + self.timeout
        while True:
            foreign = foreign_holder(self.path)
            if foreign is not None:
                if time.monotonic() >= deadline:
                    raise LockHeld(f"{self.path} is held on host {foreign.get('host')} by {foreign.get('owner')}")
                time.sleep(0.05)
                continue
            fh = open(self.path, "a+b")
            try:
                if fh.tell() == 0 and os.fstat(fh.fileno()).st_size == 0:
                    fh.write(b"\0")        # the lockable byte
                    fh.flush()
                _lock(fh)
            except OSError as exc:
                fh.close()
                if exc.errno not in _CONTENTION and getattr(exc, "winerror", None) not in (33, 36):
                    raise
                if time.monotonic() >= deadline:
                    raise LockHeld(f"{self.path} is held by another process")
                time.sleep(0.05)
                continue
            self._fh = fh
            with _held_guard:
                _held.add(self._key)
            try:
                _sidecar(self.path).write_text(
                    json.dumps({"owner": self.owner, "pid": os.getpid(), "host": HOST, "since": time.time()}), encoding="utf-8")
            except OSError:
                pass
            return self

    def touch(self):
        """Refresh the sidecar mtime (the supervisor calls this every heartbeat so other hosts see the lock as live)."""
        try:
            os.utime(_sidecar(self.path), None)
        except OSError:
            pass

    def release(self):
        if self._fh:
            try:
                sc = _sidecar(self.path)
                info = json.loads(sc.read_text(encoding="utf-8"))
                if info.get("host", HOST) == HOST and info.get("pid") == os.getpid():
                    sc.unlink()                                   # before unlocking: still ours — never another host's
            except (OSError, ValueError):
                pass
            try:
                _unlock(self._fh)
            except OSError:
                pass
            self._fh.close()
            self._fh = None
            with _held_guard:
                _held.discard(self._key)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def holder(path: Path) -> dict | None:
    """Who holds the lock, if anyone (best effort: tries to take it, then reads the owner sidecar)."""
    try:
        with FileLock(path, owner="probe"):
            return None
    except LockHeld:
        try:
            return json.loads(_sidecar(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"owner": "unknown"}
