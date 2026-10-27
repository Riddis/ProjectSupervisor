"""Owner commands and log lines from an extension (docs/EXTENSION_API.md, Commands and the log)."""
from __future__ import annotations

import re

from .. import commands as _commands
from .. import tasks as _tasks
from ..extensions import ExtensionError
from ..paths import Layout

_COMMAND_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")


def submit_command(layout: Layout, kind: str, payload: dict, actor: str = "owner-ui",
                   command_id: str | None = None) -> dict:
    """Submit an owner command: applied now when no loop holds the state lock, else queued for the loop, exactly as
    the Control Center's own actions are. Returns the applied record (`{"id", "ok", "result" | "error", ...}`) or
    `{"id", "queued": True}`. `command_id` makes a resubmission harmless: a command with an id already submitted is
    not applied again, and the answer is that command's record (or `queued`) with `"replayed": True`. It is the
    command's `_id` in the handler's payload; letters, digits, `_`, `.` and `-`, at most 80. Without it every call
    is a new command."""
    if command_id is not None:
        if not isinstance(command_id, str) or not _COMMAND_ID.fullmatch(command_id):
            raise ValueError(f"command_id must be a plain name (letters, digits, _ . -; at most 80), got {command_id!r}")
        known = _commands.status_of(layout, command_id)
        if known is not None:
            return {**known, "queued": bool(known.get("pending")), "replayed": True}
    outcome = _commands.submit_or_apply(layout, kind, payload, actor, command_id=command_id)
    if command_id is not None and outcome.get("queued"):
        known = _commands.status_of(layout, command_id)
        if known is not None and not known.get("pending"):     # applied by someone else in between: report that
            return {**known, "queued": False, "replayed": True}
    return outcome


def log_event(layout: Layout, event: str, actor: str | None = None, **fields) -> None:
    """One line in state/log.jsonl through the framework's single log path (its credential redaction included). The
    actor is the calling extension's name, or `<extension>:<actor>` when `actor` is given (`review_rounds:owner-ui`),
    so a line always says which extension wrote it. Raises ExtensionError when the caller is not an extension's
    code."""
    from .. import extensions
    who = extensions.calling_extension(layout)
    if who is None:
        raise ExtensionError(f"log_event({event!r}) writes an extension's log lines: call it from that extension's code")
    _tasks.log(layout, event, f"{who}:{actor}" if actor else who, **fields)
