"""SSE helpers: tail a growing file and push new lines to the browser as they are appended."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request


def _format(data: str, event: str | None = None) -> str:
    lines = "".join(f"data: {line}\n" for line in data.splitlines() or [""])
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}{lines}\n"


async def tail_file(request: Request, path: Path, poll_s: float = 0.5, from_end: bool = True,
                    max_initial_lines: int = 200) -> AsyncIterator[str]:
    """Yield SSE frames for lines appended to `path`. Starts at the end of the file by default (live tail); pass
    from_end=False to replay the last `max_initial_lines` first (used for the initial page load of a finished run)."""
    offset = 0
    if path.exists():
        size = path.stat().st_size
        if from_end:
            offset = size
        else:
            existing = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_initial_lines:]
            for line in existing:
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
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    if line:
                        yield _format(line)
            elif size < offset:
                offset = 0  # file was truncated/replaced
        await asyncio.sleep(poll_s)


def log_line_text(raw: str) -> str:
    try:
        ev = json.loads(raw)
    except ValueError:
        return raw
    bits = [ev.get("ts", ""), ev.get("actor", ""), ev.get("event", "")]
    extra = {k: v for k, v in ev.items() if k not in ("ts", "actor", "event") and v not in (None, "")}
    if extra:
        bits.append(json.dumps(extra, ensure_ascii=False))
    return " ".join(str(b) for b in bits if b)
