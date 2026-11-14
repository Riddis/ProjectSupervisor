"""Read the tail of state/log.jsonl for display (never parsed back into state; render.py already established that
convention for the markdown projections, this module just mirrors it for the UI)."""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import Layout

CHUNK_S = 8192


def tail_bytes(path: Path, min_lines: int, chunk_size: int = CHUNK_S) -> bytes:
    """The bytes from the end of `path` that cover at least `min_lines` newlines, read by seeking backward in
    chunks rather than loading the whole file (C-1: the Overview read the whole event log twice, and a 100- or
    500-line tail read it whole too, just to show the last few lines; sse.tail_file's replay reuses this for the
    same reason on a long-running job's transcript). A file smaller than a few chunks is still read in full, in
    one or two reads."""
    if min_lines <= 0:
        return b""
    with path.open("rb") as fh:
        fh.seek(0, 2)
        remaining = fh.tell()
        blocks: list[bytes] = []
        newlines = 0
        while remaining > 0 and newlines <= min_lines:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            fh.seek(remaining)
            block = fh.read(read_size)
            blocks.append(block)
            newlines += block.count(b"\n")
        return b"".join(reversed(blocks))


def _tail_lines(path: Path, n: int) -> list[str]:
    """The last `n` non-empty lines of `path`."""
    if n <= 0:
        return []
    data = tail_bytes(path, n)
    lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln]
    return lines[-n:]


def tail(layout: Layout, n: int = 20) -> list[dict]:
    if not layout.log.exists():
        return []
    rows = []
    for line in _tail_lines(layout.log, n):
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append({"event": "unparsable", "raw": line})
    rows.reverse()
    return rows
