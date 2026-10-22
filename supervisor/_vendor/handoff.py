"""Cooperative capacity checkpointing: HANDOFF_REQUEST.json / HANDOFF.md protocol and hash-bound scope baselines.

Vendored from an earlier supervisor's `agent_handoff.py`; iteration-planning fields and the evidence-view dependency removed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contract import atomic_json

HANDOFF_LIMIT = 6000


def instructions(out: Path, prior: str | None = None, reserve_percent: int = 10) -> str:
    prior_line = f"Read {prior} first and verify its claims against the referenced files. " if prior else ""
    return (
        "Continuity protocol: this is a fresh, NON-INTERACTIVE session (`claude -p`): nobody answers questions, no "
        "notification ever arrives, and every background task or child process you leave running is killed or "
        "orphaned the moment your turn ends — run long steps (installs, downloads, tests) in the FOREGROUND with an "
        "explicit timeout, re-running idempotent scripts in chunks if one call cannot finish, and only end your turn "
        "with the final result JSON or a handoff. Never end a turn 'waiting' for anything. When you run pytest, add "
        "`--basetemp=<your run dir>/pytest_tmp` (other agents run tests concurrently; the shared default basetemp "
        "collides on Windows). "
        "Do not assume any memory of earlier chats. "
        f"{prior_line}Read only the packet and the files it references. "
        f"Check {out / 'HANDOFF_REQUEST.json'} between meaningful steps (edits, tests, dispatch). "
        f"If it exists, or your provider capacity is at or below the {reserve_percent}% reserve, finish the current "
        "atomic step, write the handoff and end this turn; do not start another expensive operation and never "
        "claim incomplete work as COMPLETE. "
        f"Writable roles: create {out / 'HANDOFF.md'} early and update it after each checkpoint; keep it under "
        f"{HANDOFF_LIMIT} characters: completed work, changed paths, exact evidence references, remaining steps, "
        "blockers, next action — no transcript. If you stop before finishing, start HANDOFF.md with the line "
        "`CAPACITY HANDOFF: INCOMPLETE`. Read-only reviewers return the same concise handoff as prose before a "
        "final BLOCKED review JSON with empty findings (an interruption is not a finding)."
    )


def request(out: Path, reset_epoch=None, reason="capacity"):
    atomic_json(out / "HANDOFF_REQUEST.json", {"requested": True, "reason": reason, "reset_at": reset_epoch})


def _tail(path: Path, lines=24):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return None


def save(out: Path, status: dict, purpose: str = "bounded assignment", packet: str = "") -> Path:
    """Mechanical fallback handoff when the agent wrote none; clearly marked unverified."""
    out = Path(out)
    path = out / "HANDOFF.md"
    if not path.exists():
        excerpt = _tail(out / "result.txt")
        content = (f"# Continuation: {purpose}\n\nPacket: {packet}\nStatus: {out / 'status.json'}\n"
                   f"Result: {out / 'result.txt'}\n\nRunner checkpoint (mechanical, UNVERIFIED): inspect owned files "
                   "and evidence before continuing. Never infer completion.\n\n"
                   + ("Result excerpt:\n" + "\n".join(excerpt) if excerpt else "No result text was captured."))
        path.write_text(content[:HANDOFF_LIMIT - 100], encoding="utf-8")
    atomic_json(out / "handoff-state.json", {"schema_version": 1, "session_id": status.get("session_id"),
                                             "status": status.get("status"), "handoff": str(path),
                                             "scope_baseline": str(out / "scope-before.json")})
    return path


def incomplete(out: Path) -> bool:
    try:
        return (Path(out) / "HANDOFF.md").read_text(encoding="utf-8").lstrip().startswith("CAPACITY HANDOFF: INCOMPLETE")
    except OSError:
        return False


def continuation_fields(out: Path, writer: bool) -> dict:
    """Fields for a successor spec: never point a fresh session at a handoff that was never written."""
    out = Path(out)
    fields = {"continuation_handoff": str(out / "HANDOFF.md")} if (out / "HANDOFF.md").exists() else {}
    if writer and (out / "scope-before.json").exists():
        fields["continuation_scope_before"] = str(out / "scope-before.json")
        fields["continuation_scope_sha256"] = hashlib.sha256((out / "scope-before.json").read_bytes()).hexdigest()
    return fields


def scope_baseline(spec: dict, current: dict) -> dict:
    """The pre-work manifest a resumed worker must be judged against (hash-bound to the original)."""
    if not spec.get("continuation_scope_before"):
        return current
    path = Path(spec["continuation_scope_before"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != spec.get("continuation_scope_sha256"):
        raise ValueError("Continuation baseline changed; inspection required")
    return json.loads(path.read_text(encoding="utf-8"))
