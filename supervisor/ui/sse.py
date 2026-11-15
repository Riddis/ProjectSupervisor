"""SSE helpers: tail a growing file and push new lines to the browser as they are appended."""
from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request

from .logtail import tail_bytes


def _format(data: str, event: str | None = None) -> str:
    """Escape before framing: the browser swaps `data:` lines straight into the DOM (see static/live.js), so a log
    or transcript line that carries markup must reach it as literal text, not parsed as HTML."""
    escaped = html.escape(data)
    lines = "".join(f"data: {line}\n" for line in escaped.splitlines() or [""])
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}{lines}\n"


async def tail_file(request: Request, path: Path, poll_s: float = 0.5, from_end: bool = True,
                    max_initial_lines: int = 200) -> AsyncIterator[str]:
    """Yield SSE frames for lines appended to `path`. Starts at the end of the file by default (live tail); pass
    from_end=False to replay the last `max_initial_lines` first (used for the initial page load of a finished run,
    and for a still-running one: its element must not start empty either). A write that lands in two chunks (a
    long line flushed partway) never reaches the browser as two lines: a trailing fragment with no newline yet is
    held in `buffer` and joined to whatever arrives next."""
    offset = 0
    buffer = ""
    if path.exists():
        size = path.stat().st_size
        if from_end:
            offset = size
        else:
            # Seeks backward in chunks (same as logtail's event-log tail) rather than reading the whole file:
            # a long-running job's stdout.jsonl is the largest file the Control Center opens, and every load of
            # its stream must not pull all of it into memory just to show the last `max_initial_lines`.
            existing = tail_bytes(path, max_initial_lines).decode("utf-8", errors="replace").split("\n")
            buffer = existing.pop()                  # the file's own last line may itself still be mid-write
            for line in existing[-max_initial_lines:]:
                if line:
                    yield _format(line)
            offset = size
    yield _format("", event="ready")
    while True:
        if await request.is_disconnected():
            return
        if path.exists():
            size = path.stat().st_size
            if size > offset:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(size - offset)
                offset = size
                text = buffer + chunk.decode("utf-8", errors="replace")
                lines = text.split("\n")
                buffer = lines.pop()                  # the tail after the last '\n' (whole line, or a partial one)
                for line in lines:
                    if line:
                        yield _format(line)
            elif size < offset:
                offset = 0                            # file was truncated/replaced
                buffer = ""
        await asyncio.sleep(poll_s)
