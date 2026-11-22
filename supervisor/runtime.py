"""Supervisor runtime state: state/runtime/supervisor.json (pid, status, decision, heartbeat, counters)."""
from __future__ import annotations

import os
import time

import psutil   # hard dependency already (adapters/process.py, leases.py): {pid, create_time} liveness

from ._vendor.contract import atomic_json, read_json
from .paths import Layout
from .store import now_iso

# Fallback only (doc.has no {pid, create_time}, e.g. one written by an older build): a heartbeat window
# widened by the last turn's own length must still not hide a truly dead loop forever.
HEARTBEAT_CEILING_S = 600.0

# DRAIN: launch nothing more, let running jobs finish and be ingested, then STOP — and shut the
# workstation down when `shutdown_on_drain` is set on the runtime document (the Control Center's "Shut down PC
# when idle" button). RUN cancels it at any point before the loop has stopped.
DECISIONS = ("RUN", "PAUSE", "DRAIN", "STOP", "RELOAD")


def read(layout: Layout) -> dict:
    p = layout.runtime / "supervisor.json"
    try:
        return read_json(p)
    except (OSError, ValueError):
        return {"status": "STOPPED", "decision": "RUN", "pid": None}


def write(layout: Layout, **fields) -> dict:
    doc = read(layout)
    doc.update(fields)
    doc["updated"] = now_iso()
    atomic_json(layout.runtime / "supervisor.json", doc)
    return doc


def set_decision(layout: Layout, decision: str, actor: str = "owner", **fields) -> dict:
    if decision not in DECISIONS:
        raise ValueError(decision)
    if decision != "DRAIN":
        fields.setdefault("shutdown_on_drain", False)     # leaving DRAIN (RUN/PAUSE/STOP) always disarms the shutdown
    return write(layout, decision=decision, decision_by=actor, decision_at=now_iso(), **fields)


def heartbeat(layout: Layout, status: str = "RUNNING", **extra) -> dict:
    pid = os.getpid()
    try:
        create_time = psutil.Process(pid).create_time()
    except psutil.Error:
        create_time = None
    return write(layout, status=status, pid=pid, create_time=create_time, heartbeat=now_iso(),
                 heartbeat_epoch=time.time(), **extra)


def _process_alive(pid, create_time) -> bool:
    """Same {pid, create_time} match as leases._pid_alive and adapters/process.py: a bare pid is not enough,
    since the OS reuses them -- the recorded create_time must still match the live process's own."""
    if not isinstance(pid, int) or create_time is None:
        return False
    try:
        p = psutil.Process(pid)
        return abs(p.create_time() - float(create_time)) < 2.0 and p.is_running()
    except (psutil.Error, ValueError, OSError):
        return False


def is_live(layout: Layout, max_age_s: float = 180.0) -> bool:
    """A fixed heartbeat-age window alone (R-28) reads a turn that is still legitimately blocking -- a repo-wide
    snapshot, a git commit, a synchronous dispatch -- as an offline loop the moment it runs longer than
    `max_age_s`, which also enables Start in the Control Center on a loop that is still running. Scaling that
    window by the last turn's own recorded length fixes the long-turn case but then has no ceiling: a turn that
    legitimately ran a whole verification reads as live for however long that turn took, doubled, even after the
    process is gone. `heartbeat()` now records the writer's own {pid, create_time} (R-09's established pattern,
    leases._pid_alive / adapters/process.py), so a live document is believed only while that exact process is
    still running -- immediately false the moment it dies, no matter how long its last turn was. The age window,
    capped at HEARTBEAT_CEILING_S, is a fallback only, for a document an older build left with no pid recorded."""
    doc = read(layout)
    if doc.get("status") not in ("RUNNING", "PAUSED", "DRAINING"):
        return False
    if doc.get("pid") is not None and doc.get("create_time") is not None:
        return _process_alive(doc.get("pid"), doc.get("create_time"))
    try:
        turn_s = float(doc.get("turn_s") or 0)
    except (TypeError, ValueError):
        turn_s = 0.0
    window = min(max(max_age_s, turn_s * 2), HEARTBEAT_CEILING_S)
    return (time.time() - float(doc.get("heartbeat_epoch", 0))) < window
