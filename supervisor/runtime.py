"""Supervisor runtime state: state/runtime/supervisor.json (pid, status, decision, heartbeat, counters)."""
from __future__ import annotations

import os
import time

from ._vendor.contract import atomic_json, read_json
from .paths import Layout
from .store import now_iso

STATUSES = ("RUNNING", "PAUSED", "DRAINING", "STOPPED", "BLOCKED", "CRASHED")
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
    return write(layout, status=status, pid=os.getpid(), heartbeat=now_iso(), heartbeat_epoch=time.time(), **extra)


def is_live(layout: Layout, max_age_s: float = 180.0) -> bool:
    doc = read(layout)
    return doc.get("status") in ("RUNNING", "PAUSED", "DRAINING") and (time.time() - float(doc.get("heartbeat_epoch", 0))) < max_age_s
