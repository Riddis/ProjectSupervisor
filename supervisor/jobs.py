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


# Last error per (project root, file name), so a record that keeps failing to read is logged once, not on every
# scan (R-03; the same dedup tasks.py's `_report_unreadable` already uses for an unreadable task file).
_last_job_error: dict[tuple[str, str], str] = {}


def _report_unreadable(layout: Layout, p: Path, exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}"[:300]
    key = (str(layout.root), p.name)
    if _last_job_error.get(key) != msg:
        _last_job_error[key] = msg
        from . import tasks as tasks_mod            # local: tasks.py does not import jobs.py
        tasks_mod.log(layout, "job_unreadable", "loop", path=p.name, error=msg)
    return msg


def all_records(layout: Layout) -> list[dict]:
    out = []
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            out.append(read_json(p))
        except (OSError, ValueError):
            continue
    return out


def open_records(layout: Layout) -> list[dict]:
    """Every job still in flight. Read-only (R-03): a record that does not parse is reported once and left
    exactly as it is — never rewritten, quarantined or deleted. Enumeration must never be the reason a job's
    bytes are lost."""
    out = []
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            rec = read_json(p)
        except (OSError, ValueError) as exc:
            _report_unreadable(layout, p, exc)
            continue
        if rec.get("phase") in ("PREPARED", "SUBMITTED", "RUNNING"):
            out.append(rec)
    return out


def unreadable(layout: Layout, task_id: str | None = None) -> dict[str, str]:
    """The job files that do not parse, as {file stem: short error} — for a page that must show a broken
    record as such (R-03), never work around it. Pass `task_id` to see only that task's own files."""
    pattern = f"{task_id}.a*.json" if task_id else "*.json"
    out = {}
    for p in sorted(layout.jobs.glob(pattern)):
        try:
            read_json(p)
        except (OSError, ValueError) as exc:
            out[p.stem] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def attempt_no(attempt_id: str) -> int:
    """The attempt number of `<task>.a<n>` (0 when the id has no such suffix)."""
    try:
        return int(str(attempt_id).rsplit(".a", 1)[1])
    except (IndexError, ValueError):
        return 0


def highest_attempt_no(layout: Layout, task_id: str) -> int:
    """The highest attempt number among this task's job files, from the file names alone — never opens a
    record, so it is unaffected by one being unreadable (R-03). Seeds the task's own counter the first time a
    task dispatches after this change, so a project with no `job_seq` yet never reuses a number already on
    disk."""
    best = 0
    for p in layout.jobs.glob(f"{task_id}.a*.json"):
        best = max(best, attempt_no(p.stem))
    return best


def for_task(layout: Layout, task_id: str) -> list[dict]:
    """The task's job records, oldest attempt first by attempt NUMBER: file names sort `T.a10` before `T.a9`, and
    callers take `[-1]` as the newest. Read-only (R-03): an unreadable record is reported once and represented
    by a stub, never rewritten on disk."""
    out = []
    for p in layout.jobs.glob(f"{task_id}.*.json"):
        try:
            r = read_json(p)
        except (OSError, ValueError) as exc:
            msg = _report_unreadable(layout, p, exc)
            r = {"attempt_id": p.stem, "task_id": task_id, "phase": "FAILED", "error": msg}
        if r.get("task_id") == task_id:
            out.append(r)
    return sorted(out, key=lambda r: (attempt_no(r.get("attempt_id", "")), str(r.get("attempt_id", ""))))
