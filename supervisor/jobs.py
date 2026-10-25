"""Durable dispatch records: state/runtime/jobs/<attempt_id>.json

phase: PREPARED (record written, nothing launched) → SUBMITTED (handle recorded) → RUNNING (first output seen) →
DONE (result ingested into the task store) | FAILED (lost/errored). Reconciliation before any dispatch makes a crash at
any point safe: PREPARED → re-dispatch same attempt; SUBMITTED/RUNNING → adopt if alive, collect if finished, else lost.
"""
from __future__ import annotations

from pathlib import Path

from ._vendor.contract import atomic_json, read_json
from .paths import Layout
from .store import now_iso

PHASES = ("PREPARED", "SUBMITTED", "RUNNING", "DONE", "FAILED")


def path(layout: Layout, attempt_id: str) -> Path:
    return layout.jobs / f"{attempt_id}.json"


def write(layout: Layout, record: dict) -> dict:
    record["updated"] = now_iso()
    atomic_json(path(layout, record["attempt_id"]), record)
    return record


def read(layout: Layout, attempt_id: str) -> dict:
    return read_json(path(layout, attempt_id))


def prepare(layout: Layout, task_id: str, attempt_id: str, adapter: str, spec: dict, gpu: bool = False, **extra) -> dict:
    """One durable record before the job is launched; `extra` (review_role, review_digest, ...) belongs in the same
    write, never in a second one a crash could lose."""
    return write(layout, {"attempt_id": attempt_id, "task_id": task_id, "adapter": adapter, "phase": "PREPARED",
                          "spec": spec, "handle": None, "gpu": gpu, "created": now_iso(), **extra})


def set_phase(layout: Layout, attempt_id: str, phase: str, **fields) -> dict:
    if phase not in PHASES:
        raise ValueError(phase)
    rec = read(layout, attempt_id)
    rec["phase"] = phase
    rec.update(fields)
    return write(layout, rec)


def all_records(layout: Layout) -> list[dict]:
    out = []
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            out.append(read_json(p))
        except (OSError, ValueError):
            continue
    return out


def open_records(layout: Layout) -> list[dict]:
    out = []
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            rec = read_json(p)
        except (OSError, ValueError):
            atomic_json(p.with_suffix(".corrupt.json"), {"attempt_id": p.stem, "phase": "FAILED", "error": "corrupt record"})
            p.unlink(missing_ok=True)
            continue
        if rec.get("phase") in ("PREPARED", "SUBMITTED", "RUNNING"):
            out.append(rec)
    return out


def attempt_no(attempt_id: str) -> int:
    """The attempt number of `<task>.a<n>` (0 when the id has no such suffix)."""
    try:
        return int(str(attempt_id).rsplit(".a", 1)[1])
    except (IndexError, ValueError):
        return 0


def for_task(layout: Layout, task_id: str) -> list[dict]:
    """The task's job records, oldest attempt first by attempt NUMBER: file names sort `T.a10` before `T.a9`, and
    callers take `[-1]` as the newest."""
    out = []
    for p in layout.jobs.glob(f"{task_id}.*.json"):
        try:
            r = read_json(p)
        except (OSError, ValueError):
            r = {"attempt_id": p.stem, "task_id": task_id, "phase": "FAILED", "error": "corrupt record"}
        if r.get("task_id") == task_id:
            out.append(r)
    return sorted(out, key=lambda r: (attempt_no(r.get("attempt_id", "")), str(r.get("attempt_id", ""))))
