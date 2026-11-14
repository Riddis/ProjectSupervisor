"""Read the tail of state/log.jsonl for display (never parsed back into state; render.py already established that
convention for the markdown projections, this module just mirrors it for the UI)."""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import Layout

CHUNK_S = 8192


def _tail_lines(path: Path, n: int) -> list[str]:
    """The last `n` non-empty lines of `path`, read by seeking backward from the end in chunks (C-1: the Overview
    read the whole event log twice, and a 100- or 500-line tail read it whole too, just to show the last few
    lines). A file smaller than a few chunks is still read in full, in one or two reads."""
    if n <= 0:
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        remaining = fh.tell()
        blocks: list[bytes] = []
        newlines = 0
        while remaining > 0 and newlines <= n:
            read_size = min(CHUNK_S, remaining)
            remaining -= read_size
            fh.seek(remaining)
            block = fh.read(read_size)
            blocks.append(block)
            newlines += block.count(b"\n")
        data = b"".join(reversed(blocks))
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
