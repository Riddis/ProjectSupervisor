"""SSE helpers: tail a growing file and push new lines to the browser as they are appended."""
from __future__ import annotations

import asyncio
import html
import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request

from .timefmt import local_stamp


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
            existing = path.read_text(encoding="utf-8", errors="replace").split("\n")
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


def log_line_text(raw: str) -> str:
    try:
        ev = json.loads(raw)
    except ValueError:
        return raw
    bits = [local_stamp(ev.get("ts", "")), ev.get("actor", ""), ev.get("event", "")]
    extra = {k: v for k, v in ev.items() if k not in ("ts", "actor", "event") and v not in (None, "")}
    if extra:
        bits.append(json.dumps(extra, ensure_ascii=False))
    return " ".join(str(b) for b in bits if b)
