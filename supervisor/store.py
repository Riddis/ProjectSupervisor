"""Document store over YAML files with revisions and compare-and-swap.

`update(path, fn, expected_rev)` re-reads under the store lock, refuses stale revs, applies fn, bumps rev, writes
atomically. Used for tasks, issues, inbox items, gates. Markdown projections are rendered from it, never parsed.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Callable

import yaml

from ._vendor.contract import CannotKeep, atomic_write_text
from .locks import FileLock

_thread_lock = threading.RLock()


class StaleRevision(RuntimeError):
    pass


def rewrite_text(path: Path, text: str) -> bool:
    """Replace a text file's content with `text` (newlines as `read_text` returns them) in the file's own line-ending
    style, and only when the content changes: True when it was written. Migrations use this so a second run reports
    nothing and a file keeps LF or CRLF as it had it (Path.write_text turns LF files into CRLF on Windows). The new
    content replaces the file atomically, so an interruption leaves the old file whole, never a truncated one, and
    the file keeps its owner, group, permissions and links (atomic_write_text keep=True); when it cannot, CannotKeep
    is raised and the old file stays as it was."""
    path = Path(path)
    raw = path.read_bytes() if path.exists() else b""
    text = text.replace("\r\n", "\n")
    if path.exists() and raw.decode("utf-8").replace("\r\n", "\n") == text:
        return False
    atomic_write_text(path, as_written(raw, text), newline="", keep=True)
    return True


def as_written(raw: bytes, text: str) -> str:
    """`text` as rewrite_text() stores it over a file whose bytes are `raw`: in that file's line-ending style."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n" if b"\r\n" in raw else "\n")


def newline_of(path: Path) -> str:
    """The line ending a text file uses: CRLF when it has any, else LF, which is also what a new file gets (1.12.1).
    git stores LF, so a state file the framework creates shows no line-ending change on any platform."""
    try:
        with open(path, "rb") as f:
            return "\r\n" if b"\r\n" in f.read() else "\n"
    except FileNotFoundError:
        return "\n"


def write_text(path: Path, text: str) -> None:
    """Replace a text file atomically, in the file's own line endings (LF for a new one). The framework's state
    writers use this: writing with the platform's newline turned every LF file into CRLF on Windows, so a one-line
    change showed in git as the whole file changed (1.12.1)."""
    newline = newline_of(path)
    atomic_write_text(path, text.replace("\r\n", "\n").replace("\n", newline), newline="")


_NEWLINE_CACHE: dict[str, str] = {}
_NEWLINE_CACHE_LOCK = threading.Lock()


def _cached_newline(path: Path) -> str:
    """`newline_of(path)`, read from disk once per process and remembered by path (R-23): `append_line` is the
    only writer of the files it is used for (the event log), so nothing else can move its convention while this
    process runs — the same assumption `tasks._PARSE_CACHE` and `jobs._RECORD_CACHE` already make about their own
    files, just for a line ending instead of a parse."""
    key = str(path)
    with _NEWLINE_CACHE_LOCK:
        hit = _NEWLINE_CACHE.get(key)
    if hit is not None:
        return hit
    nl = newline_of(path)
    with _NEWLINE_CACHE_LOCK:
        _NEWLINE_CACHE[key] = nl
    return nl


def append_line(path: Path, line: str) -> None:
    """Append one line in the file's own line endings (LF for a new file): the event log's writer."""
    newline = _cached_newline(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(line.rstrip("\r\n") + newline)


ROTATE_MAX_BYTES = 10 * 1024 * 1024    # generous for one project's event log, next to a multi-GB disk
ROTATE_KEEP = 3                        # path.1 .. path.<keep>, oldest dropped


def rotate_log(path: Path, max_bytes: int = ROTATE_MAX_BYTES, keep: int = ROTATE_KEEP) -> bool:
    """Rotate `path` to `path.1` (shifting `.1`..`.<keep-1>` up one, dropping the oldest) once it is at least
    `max_bytes` (R-23): the event log is appended every turn and otherwise grows forever, and every tail — by
    line count or by byte offset — still opens and seeks within whatever is there. Leaves a fresh, empty file at
    `path`. Returns whether it rotated; a missing or small file is left alone."""
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    for n in range(keep - 1, 0, -1):
        src, dst = path.with_name(f"{path.name}.{n}"), path.with_name(f"{path.name}.{n + 1}")
        if src.exists():
            src.replace(dst)
    path.replace(path.with_name(f"{path.name}.1"))
    path.touch()
    with _NEWLINE_CACHE_LOCK:
        _NEWLINE_CACHE.pop(str(path), None)       # the fresh file's own ending is read again on its first append
    return True


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _default_loader(yaml_module=yaml):
    """libyaml's loader when the interpreter has it (about 10x faster on the task store, R-09; same documents —
    `tests/test_store_loader.py` proves it over the fixtures) — the pure-Python one otherwise, so a machine
    without libyaml still runs, just slower."""
    return getattr(yaml_module, "CSafeLoader", yaml_module.SafeLoader)


_LOADER = _default_loader()


def read(path: Path) -> dict:
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_LOADER) or {}


def dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


def write_new(path: Path, doc: dict) -> dict:
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    doc = dict(doc)
    doc.setdefault("created", now_iso())
    doc["updated"] = doc.get("updated", doc["created"])
    doc["rev"] = 1
    write_text(path, dump(doc))
    return doc


def update(path: Path, fn: Callable[[dict], dict | None], expected_rev: int | None = None, lock_path: Path | None = None) -> dict:
    """Atomically transform the document at `path` under the lock. `fn` receives the freshly read doc and must decide
    from it (never from an earlier read); it mutates/returns the doc. Stale expected_rev → StaleRevision.
    `fn` may raise to abort without writing. Never call update() from inside another update's fn (same lock path)."""
    path = Path(path)
    lock = FileLock(lock_path or path.with_suffix(path.suffix + ".lock"), owner="store", timeout=5.0)
    with _thread_lock, lock:
        doc = read(path)
        current = int(doc.get("rev", 0))
        if expected_rev is not None and expected_rev != current:
            raise StaleRevision(f"{path.name}: expected rev {expected_rev}, found {current}")
        result = fn(doc)
        doc = result if isinstance(result, dict) else doc
        doc["rev"] = current + 1
        doc["updated"] = now_iso()
        write_text(path, dump(doc))
        return doc
