"""Read the tail of state/log.jsonl for display (never parsed back into state; render.py already established that
convention for the markdown projections, this module just mirrors it for the UI)."""
from __future__ import annotations

import json

from ..paths import Layout


def tail(layout: Layout, n: int = 20) -> list[dict]:
    if not layout.log.exists():
        return []
    rows = []
    for line in layout.log.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append({"event": "unparsable", "raw": line})
    rows.reverse()
    return rows
