"""Live pages: a cheap fingerprint of everything the Control Center renders from, so an open page refreshes itself
when the project changes instead of the owner pressing F5.

Each page carries the fingerprint taken when its request started (`<meta name="live-version">`); `static/live.js`
polls `GET /live/version` every couple of seconds and, when the value moved, re-fetches the page and morphs the new
content into place (scroll position, open `<details>`, and anything the owner is typing are kept). `boot` changes
whenever the Control Center process restarts, so an open tab reloads fully onto new templates and styles.

The fingerprint is stat-only (path, mtime, size) over `state/` (minus applied commands, lock/temp/log files and
caches the UI itself writes), `config/` and `project.yaml`. Rendering a page must never move it, or that page would
refresh itself in a loop (tests/test_live.py renders every page to check). `state/runtime/supervisor.json` is
rewritten on every loop heartbeat, so it counts by content with its heartbeat fields dropped: a heartbeat alone is
not a change, a status or decision change is.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from ..paths import Layout

BOOT_ID = secrets.token_hex(6)                 # new per Control Center process
CACHE_TTL_S = 1.0                              # one scan serves every open tab polling within this window
HEARTBEAT_KEYS = frozenset({"updated", "heartbeat", "heartbeat_epoch", "iteration"})
SKIP_DIRS = frozenset({"applied", "__pycache__"})
SKIP_SUFFIXES = (".lock", ".log", ".tmp", ".part")
# supervisor.json is hashed by content (see _runtime_digest). update_check.json is the update banner's cache, which
# rendering the Overview may rewrite: counting it would make the Overview refresh itself forever (the banner follows
# within live.js's 30 s fallback refresh instead).
SKIP_NAMES = frozenset({"ui_token", "supervisor.json", "update_check.json"})

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def _skip(name: str) -> bool:
    return name.startswith(".") or name.endswith(SKIP_SUFFIXES) or name in SKIP_NAMES


def _walk(base: Path, root: Path, out: list[str]) -> None:
    try:
        entries = list(os.scandir(base))
    except OSError:                                         # missing or vanished mid-scan: nothing to see there
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    _walk(Path(entry.path), root, out)
            elif not _skip(entry.name):
                st = entry.stat(follow_symlinks=False)
                out.append(f"{Path(entry.path).relative_to(root).as_posix()}\0{st.st_mtime_ns}\0{st.st_size}")
        except OSError:                                     # a file replaced during the scan; the next poll sees it
            continue


def _runtime_digest(layout: Layout) -> str:
    path = layout.runtime / "supervisor.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):                           # mid-write; the next poll reads it whole
        return "unreadable"
    if not isinstance(doc, dict):
        return "invalid"
    return json.dumps({k: v for k, v in doc.items() if k not in HEARTBEAT_KEYS}, sort_keys=True, default=str)


def compute(layout: Layout) -> str:
    entries: list[str] = []
    for base in (layout.state, layout.config):
        _walk(base, layout.root, entries)
    try:
        st = layout.project_file.stat()
        entries.append(f"project.yaml\0{st.st_mtime_ns}\0{st.st_size}")
    except OSError:
        pass
    entries.sort()
    h = hashlib.sha1(usedforsecurity=False)
    for line in entries:
        h.update(line.encode("utf-8", "surrogateescape"))
        h.update(b"\n")
    h.update(_runtime_digest(layout).encode("utf-8"))
    return h.hexdigest()[:16]


def fingerprint(layout: Layout, max_age_s: float = CACHE_TTL_S) -> str:
    """The current fingerprint, served from a cache at most `max_age_s` old (0 forces a fresh scan). A cached value
    is never newer than the state it describes, so a page stamped with it can only refresh once too often."""
    key = str(layout.root)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and max_age_s > 0 and now - hit[0] < max_age_s:
            return hit[1]
    value = compute(layout)
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def skip_stamp(path: str) -> bool:
    """Requests that never render a page: no fingerprint is taken for them."""
    return path.startswith(("/static/", "/assets/", "/live/", "/thumb/")) or path.endswith("/stream")
