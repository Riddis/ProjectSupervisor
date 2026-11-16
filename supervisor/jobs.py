"""Durable dispatch records: state/runtime/jobs/<attempt_id>.json

phase: PREPARED (record written, nothing launched) → SUBMITTED (handle recorded) → RUNNING (first output seen) →
DONE (result ingested into the task store) | FAILED (lost/errored). Reconciliation before any dispatch makes a crash at
any point safe: PREPARED → re-dispatch same attempt; SUBMITTED/RUNNING → adopt if alive, collect if finished, else lost.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
from pathlib import Path

from ._vendor.contract import atomic_json, read_json
from .paths import Layout
from .store import now_iso

PHASES = ("PREPARED", "SUBMITTED", "RUNNING", "DONE", "FAILED")
OPEN_PHASES = ("PREPARED", "SUBMITTED", "RUNNING")


def path(layout: Layout, attempt_id: str) -> Path:
    return layout.jobs / f"{attempt_id}.json"


# The current loop turn's own in-memory copy of the open-job list (R-09): while active, open_records() returns a
# copy of this list instead of re-scanning state/runtime/jobs/, and write() keeps it in step with every job record
# this turn writes (a launch, a settle, a retire, ...), so a step that reads job state an earlier step of the SAME
# turn changed sees the change. None outside a turn: every other caller still gets a real, fresh scan.
_TURN_OPEN: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar("supervisor_turn_jobs", default=None)


@contextlib.contextmanager
def turn_snapshot(open_records_list: list[dict]):
    """Hold `open_records_list` (already loaded) as the open-job list for every `open_records()` call made inside
    the block, in this thread, kept current by `write()`. One loop turn opens this once, after its own single real
    scan (`once()`); nothing inside the block pays for a second one."""
    token = _TURN_OPEN.set(open_records_list)
    try:
        yield
    finally:
        _TURN_OPEN.reset(token)


def _sync_turn_snapshot(record: dict) -> None:
    snap = _TURN_OPEN.get()
    if snap is None:
        return
    attempt_id = record.get("attempt_id")
    for i, r in enumerate(snap):
        if r.get("attempt_id") == attempt_id:
            if record.get("phase") in OPEN_PHASES:
                snap[i] = record
            else:
                del snap[i]
            return
    if record.get("phase") in OPEN_PHASES:
        snap.append(record)


def write(layout: Layout, record: dict) -> dict:
    record["updated"] = now_iso()
    atomic_json(path(layout, record["attempt_id"]), record)
    _sync_turn_snapshot(record)
    return record


def read(layout: Layout, attempt_id: str) -> dict:
    return read_json(path(layout, attempt_id))


def prepare(layout: Layout, task_id: str, attempt_id: str, adapter: str, spec: dict, gpu: bool = False, *,
           kind: str | None = None, attempt: int | None = None, seq: int | None = None, parent: str | None = None,
           cause: str | None = None, scope: str | None = None, env_digest: str | None = None, **extra) -> dict:
    """One durable record before the job is launched; `extra` (review_role, review_digest, ...) belongs in the same
    write, never in a second one a crash could lose. `kind`/`attempt`/`seq`/`parent`/`cause`/`scope`/`env_digest`
    are the identity fields the attempt model adds (§2.6); each is omitted (not written as null) when not given,
    so a record prepared with none of them looks exactly as it did before this change — no caller passes them yet."""
    identity = {"kind": kind, "attempt": attempt, "seq": seq, "parent": parent, "cause": cause, "scope": scope,
               "env_digest": env_digest}
    extra = {**{k: v for k, v in identity.items() if v is not None}, **extra}
    return write(layout, {"attempt_id": attempt_id, "task_id": task_id, "adapter": adapter, "phase": "PREPARED",
                          "spec": spec, "handle": None, "gpu": gpu, "created": now_iso(), **extra})


def resolve(layout: Layout, job_id: str) -> dict | None:
    """The job record named `job_id`, or `None` when it does not exist, instead of raising — the one place a
    later fallback to the archive (§4.5, not built yet) will add itself, and the one way a caller that merely
    wants to know whether a job exists avoids catching an exception to find out."""
    p = path(layout, job_id)
    try:
        return _cached_read(p)
    except (OSError, ValueError):
        return None


def set_phase(layout: Layout, attempt_id: str, phase: str, **fields) -> dict:
    if phase not in PHASES:
        raise ValueError(phase)
    rec = read(layout, attempt_id)
    rec["phase"] = phase
    rec.update(fields)
    return write(layout, rec)


# A read shared by every caller in the process, not only one turn's own snapshot (C-1): keyed by path, kept only
# while a file's size and mtime match the stamp it was read at, same shape as tasks.py's `_PARSE_CACHE`. A finished
# job's record never changes again, so a Control Center request that scans the whole job directory (`/agents`,
# the Overview) pays for a stat per file, not a JSON parse, once the cache is warm.
_RECORD_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_RECORD_CACHE_LOCK = threading.Lock()


def _cached_read(p: Path) -> dict:
    """`read_json(p)`, reusing `_RECORD_CACHE` while the file's stamp (mtime, size) has not moved. Raises
    OSError/ValueError straight through — every caller already wraps its own glob loop in `except (OSError,
    ValueError)` for exactly this file."""
    st = p.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    key = str(p)
    with _RECORD_CACHE_LOCK:
        hit = _RECORD_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    rec = read_json(p)
    with _RECORD_CACHE_LOCK:
        _RECORD_CACHE[key] = (stamp, rec)
    return rec


# Last error per (project root, file name), so a record that keeps failing to read is logged once, not on every
# scan (R-03; the same dedup tasks.py's `_report_unreadable` already uses for an unreadable task file).
_last_job_error: dict[tuple[str, str], str] = {}


def _report_unreadable(layout: Layout, p: Path, exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}"[:300]
    key = (str(layout.root), p.name)
    if _last_job_error.get(key) != msg:
        _last_job_error[key] = msg
        from . import tasks as tasks_mod  # local: tasks.py does not import jobs.py
        tasks_mod.log(layout, "job_unreadable", "loop", path=p.name, error=msg)
    return msg


def all_records(layout: Layout) -> list[dict]:
    out = []
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            out.append(_cached_read(p))
        except (OSError, ValueError):
            continue
    return out


def _scan_open(layout: Layout) -> list[dict]:
    """The real glob and parse: every job file read once, filtered to the ones still in flight. The only place
    `open_records()` actually touches the store (R-09) — called once per turn_snapshot() block, never from inside
    one. Read-only (R-03): a record that does not parse is reported once and left exactly as it is — never
    rewritten, quarantined or deleted. Enumeration must never be the reason a job's bytes are lost."""
    out = []
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            rec = _cached_read(p)
        except (OSError, ValueError) as exc:
            _report_unreadable(layout, p, exc)
            continue
        if rec.get("phase") in OPEN_PHASES:
            out.append(rec)
    return out


def open_records(layout: Layout) -> list[dict]:
    """Every job still in flight. Inside an active `turn_snapshot()`, this returns a fresh list copy of the
    shared snapshot instead of re-scanning state/runtime/jobs/ (R-09): one loop turn calls this from thirteen
    places that each used to scan 1,380 files on their own."""
    snap = _TURN_OPEN.get()
    return [dict(r) for r in snap] if snap is not None else _scan_open(layout)


def unreadable(layout: Layout, task_id: str | None = None) -> dict[str, str]:
    """The job files that do not parse, as {file stem: short error} — for a page that must show a broken
    record as such (R-03), never work around it. Pass `task_id` to see only that task's own files."""
    pattern = f"{task_id}.a*.json" if task_id else "*.json"
    out = {}
    for p in sorted(layout.jobs.glob(pattern)):
        try:
            _cached_read(p)
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


CLOSED_RESULT = "CANCELLED"


def retire(layout: Layout, task_id: str, reason: str, actor: str = "loop") -> list[str]:
    """Close every open record (PREPARED/SUBMITTED/RUNNING) of `task_id`: a queued one is marked closed before it
    can ever launch, a running one is terminated through its own adapter first (R-04). Every closed record is
    given `result: "CANCELLED"`, so a late result that still arrives for it (`loop.ingest`'s own terminal-task
    check) is never mistaken for one this call missed. Returns the attempt ids closed."""
    from .adapters import REGISTRY
    closed = []
    for rec in for_task(layout, task_id):
        if rec.get("phase") not in ("PREPARED", "SUBMITTED", "RUNNING"):
            continue
        if rec["phase"] in ("SUBMITTED", "RUNNING") and rec.get("handle"):
            ad = REGISTRY.get(rec.get("adapter"))
            if ad is not None:
                with contextlib.suppress(Exception):  # the task retires either way
                    ad.terminate(rec["handle"])
        set_phase(layout, rec["attempt_id"], "DONE", result=CLOSED_RESULT, error=reason, finished=now_iso(),
                 closed_by=actor)
        closed.append(rec["attempt_id"])
    return closed


def _oldest_first(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: (attempt_no(r.get("attempt_id", "")), str(r.get("attempt_id", ""))))


def for_task(layout: Layout, task_id: str) -> list[dict]:
    """The task's job records, oldest attempt first by attempt NUMBER: file names sort `T.a10` before `T.a9`, and
    callers take `[-1]` as the newest. Read-only (R-03): an unreadable record is reported once and represented
    by a stub, never rewritten on disk."""
    out = []
    for p in layout.jobs.glob(f"{task_id}.*.json"):
        try:
            r = _cached_read(p)
        except (OSError, ValueError) as exc:
            msg = _report_unreadable(layout, p, exc)
            r = {"attempt_id": p.stem, "task_id": task_id, "phase": "FAILED", "error": msg}
        if r.get("task_id") == task_id:
            out.append(r)
    return _oldest_first(out)


def all_grouped(layout: Layout) -> dict[str, list[dict]]:
    """Every job record, read once (one glob, one read per file) and grouped by task id, each group ordered
    exactly as `for_task()` orders it — for a caller (`repair_continuations`) that would otherwise call
    `for_task()` once per task, re-globbing the whole directory every time (R-09). An unreadable record is
    reported once and represented by a stub, grouped under the task id its own file name names, same as
    `for_task()`."""
    groups: dict[str, list[dict]] = {}
    for p in sorted(layout.jobs.glob("*.json")):
        try:
            r = _cached_read(p)
        except (OSError, ValueError) as exc:
            msg = _report_unreadable(layout, p, exc)
            r = {"attempt_id": p.stem, "task_id": p.stem.rsplit(".a", 1)[0], "phase": "FAILED", "error": msg}
        groups.setdefault(r.get("task_id", p.stem), []).append(r)
    return {task_id: _oldest_first(recs) for task_id, recs in groups.items()}
