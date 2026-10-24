"""Timestamp display for the Control Center: store values are ISO-8601 UTC; the owner reads them as local wall-clock
`YYYY-MM-DD HH:MM` (the machine's timezone). Anything that does not parse is shown unchanged."""
from __future__ import annotations

from datetime import datetime, timezone

FORMAT = "%Y-%m-%d %H:%M"


def local_stamp(value: object, fmt: str = FORMAT) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:  # store writes UTC; a naive value is treated as UTC rather than guessed as local
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)
