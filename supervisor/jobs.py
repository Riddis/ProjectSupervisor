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


def _current_path(layout: Layout, attempt_id: str) -> Path:
    """Where this id's record is right now: the main dir, unless only the archive has it. The one place a write
    must land (R-23) — without this, updating an already-archived record (set_phase called on it again, say a
    reroute's second call) would write a fresh duplicate at the top level while the archived copy sat there
    stale, instead of there being exactly one file per id."""
    p = path(layout, attempt_id)
    if p.exists():
        return p
    ap = archive_path(layout, attempt_id)
    return ap if ap.exists() else p


def write(layout: Layout, record: dict) -> dict:
    record["updated"] = now_iso()
    atomic_json(_current_path(layout, record["attempt_id"]), record)
    _sync_turn_snapshot(record)
    return record


def read(layout: Layout, attempt_id: str) -> dict:
    """The record this id names: the main dir, falling back to the archive on a miss there (R-23) — the one
    resolver a by-id lookup goes through, so production code (loop.dispatch/reroute/launch all read a record
    straight back after a set_phase that can itself have just archived it, once its task went terminal in the
    meantime) never has to choose between raising on an id that in fact still exists and a second, archive-blind
    lookup. `resolve()` below is this with the miss turned into `None` instead of raised. Raises FileNotFoundError
    when neither location has it; a corrupt (not missing) file at either one raises straight through — no
    fallback is attempted for that, only for an outright miss."""
    try:
        return _cached_read(path(layout, attempt_id))
    except FileNotFoundError:
        pass
    return _cached_read(archive_path(layout, attempt_id))


def prepare(layout: Layout, task_id: str, attempt_id: str, adapter: str, spec: dict, gpu: bool = False, *,
           claim: str | None = None, kind: str | None = None, attempt: int | None = None, seq: int | None = None,
           parent: str | None = None, cause: str | None = None, scope: str | None = None,
           env_digest: str | None = None, **extra) -> dict:
    """One durable record before the job is launched; `extra` (review_role, review_digest, ...) belongs in the same
    write, never in a second one a crash could lose. `kind`/`attempt`/`seq`/`parent`/`cause`/`scope`/`env_digest`
    are the identity fields the attempt model adds (§2.6); each is omitted (not written as null) when not given,
    so a record prepared with none of them looks exactly as it did before this change — no caller passes them yet.
    `claim` is the named resource (docs/plan/LONG_WORK.md section 5.2) this record holds while open, written where
    `gpu` is written — the same rule the gpu flag already uses for "busy" — and `None` for every job no declaration
    names one for, which is every job before this release and every job of a project that declares no resource."""
    identity = {"kind": kind, "attempt": attempt, "seq": seq, "parent": parent, "cause": cause, "scope": scope,
               "env_digest": env_digest}
    extra = {**{k: v for k, v in identity.items() if v is not None}, **extra}
    return write(layout, {"attempt_id": attempt_id, "task_id": task_id, "adapter": adapter, "phase": "PREPARED",
                          "spec": spec, "handle": None, "gpu": gpu, "claim": claim, "created": now_iso(), **extra})


def _task_id_of(job_id: str) -> str:
    """The task id a job id names: `job_id` up to its own `.a<n>` suffix — the same split `all_grouped`'s stub
    already uses for a record it cannot read."""
    return job_id.rsplit(".a", 1)[0]


def archive_dir(layout: Layout, task_id: str) -> Path:
    return layout.jobs / "archive" / task_id


def archive_path(layout: Layout, job_id: str) -> Path:
    return archive_dir(layout, _task_id_of(job_id)) / f"{job_id}.json"


def resolve(layout: Layout, job_id: str) -> dict | None:
    """The job record named `job_id`, or `None` when it does not exist, instead of raising. `read()`'s own
    archive fallback (R-23, §4.5) is what makes a terminal task's old id or URL still resolve — this is that,
    with a miss turned into `None` for a caller (a URL, a past digest) that expects one instead of a raise."""
    try:
        return read(layout, job_id)
    except (OSError, ValueError):
        return None


def set_phase(layout: Layout, attempt_id: str, phase: str, *, release_reason: str | None = None, **fields) -> dict:
    """`release_reason` names why a claim-carrying record is leaving the open set (docs/plan/LONG_WORK.md section
    5.3): `None` (read as `"closed"`) for the ordinary case — the adapter's own result, a continuation's own
    close, a reroute — and an explicit override for the three callers that know a different one: `jobs.retire`
    (`"terminal"`, a task reaching COMPLETE/CANCELLED), `agent.terminate` (`"cancelled"`, the owner's own stop),
    and `Supervisor.ingest` (`"dead"`, a `collect()` that found the process gone). One release point: whichever
    phase a caller moves a claim-carrying record *to*, leaving PREPARED/SUBMITTED/RUNNING always logs
    `claim_released` here, so a fifth way of stopping a job needs no caller to remember this."""
    if phase not in PHASES:
        raise ValueError(phase)
    rec = read(layout, attempt_id)
    was_open, claim = rec.get("phase") in OPEN_PHASES, rec.get("claim")
    rec["phase"] = phase
    rec.update(fields)
    out = write(layout, rec)
    if was_open and phase not in OPEN_PHASES and claim:
        from . import tasks as tasks_mod
        tasks_mod.log(layout, "claim_released", "loop", claim=claim, task=rec.get("task_id"),
                      attempt=attempt_id, reason=release_reason or "closed")
    if phase in ("DONE", "FAILED"):
        # R-23: the job that is itself completing a task's final transition is still PREPARED/SUBMITTED/RUNNING
        # at the moment advance()/set_status() archives everything else of this task (archive_terminal only
        # moves already-terminal records, on purpose — see there); once IT reaches DONE/FAILED too, sweep it
        # into the archive if its task has in the meantime gone terminal, so nothing is left behind forever.
        from . import tasks as tasks_mod            # local: tasks.py already imports jobs.py at call time
        task_id = rec.get("task_id")
        if task_id:
            try:
                terminal = tasks_mod.load(layout, task_id).status in tasks_mod.TERMINAL
            except tasks_mod.TaskError:
                terminal = False
            if terminal:
                archive_terminal(layout, task_id)
    return out


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


KIND_ORDER = {"delivery": 0, "verify": 1, "review": 2, "diagnosis": 3, "plan_review": 4}


def kind_of(rec: dict) -> str:
    """This job's kind: the promoted `kind` field (§2.6 of the attempt-identity design) when set, `spec["kind"]`
    otherwise, `"delivery"` when neither is — the shape every record before this field existed is read as."""
    return rec.get("kind") or (rec.get("spec") or {}).get("kind") or "delivery"


def attempt_of(rec: dict) -> int | None:
    """This job's attempt number (`None` for a task-scoped job): the `attempt` field, taken as-is. The writer
    switch (ATTEMPT_MODEL.md §8.6's start-up guard) is what makes this safe to read with no fallback — every
    record on disk by then is either migration-stamped or allocator-written, so it always carries this field."""
    return rec.get("attempt")


def sort_key(rec: dict) -> tuple:
    """Oldest first: `(attempt, kind order, seq)`, fields only — a task-scoped job (`attempt` is `None`) sorts
    before attempt 1, matching §2.8's "displayed as a task-level row above Attempt 1". No id-suffix fallback
    (§6): the start-up guard already refuses to dispatch on a store holding a record without these fields, so
    every record this ever sorts carries them."""
    attempt = attempt_of(rec)
    return (attempt if attempt is not None else -1, KIND_ORDER.get(kind_of(rec), 99),
           rec.get("seq") or 0, str(rec.get("attempt_id", "")))


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
                 closed_by=actor, release_reason="terminal")
        closed.append(rec["attempt_id"])
    return closed


def _oldest_first(records: list[dict]) -> list[dict]:
    return sorted(records, key=sort_key)


def for_task(layout: Layout, task_id: str) -> list[dict]:
    """The task's job records, oldest first by `(attempt, kind, seq)` (`sort_key`) — no id-suffix fallback (§6):
    the start-up guard already refuses to dispatch on a store holding a record without these fields, so every
    record this reads carries them, and callers take `[-1]` as the newest. Read-only (R-03): an unreadable
    record is reported once and represented by a stub, never rewritten on disk.

    Reads the archive too (R-23): a terminal task's records have moved to `archive_dir(layout, task_id)`, but the
    Task page's own Attempts table is this function — archiving must stop the *loop's* bulk scans from seeing a
    finished record, never make a task's own history disappear from the one place that still asks for it by id."""
    out = []
    for base in (layout.jobs, archive_dir(layout, task_id)):
        for p in base.glob(f"{task_id}.*.json"):
            try:
                r = _cached_read(p)
            except (OSError, ValueError) as exc:
                msg = _report_unreadable(layout, p, exc)
                r = {"attempt_id": p.stem, "task_id": task_id, "phase": "FAILED", "error": msg}
            if r.get("task_id") == task_id:
                out.append(r)
    return _oldest_first(out)


def archive_terminal(layout: Layout, task_id: str) -> list[str]:
    """Move every already-DONE/FAILED job record of now-terminal `task_id` from `state/runtime/jobs/` into
    `state/runtime/jobs/archive/<task_id>/` (R-23): the loop's own scans (`_scan_open`, `all_records`,
    `all_grouped`) glob only the top level, non-recursively, so this is what makes a finished task's records stop
    costing every turn a stat, however many tasks have finished. `resolve()` and `for_task()` still find them
    there. Deliberately skips a record still PREPARED/SUBMITTED/RUNNING: the job that is itself completing this
    task's final transition can be exactly that, mid-call, one line above its own `jobs.set_phase(..., "DONE")`
    (`loop.ingest`'s delivery branch advances the task before closing its own job record) — moving it out from
    under that write would turn the next read into a FileNotFoundError. `set_phase` sweeps it in once it, too,
    reaches DONE/FAILED. Returns the attempt ids moved; a task with none movable, or already archived, is a
    no-op."""
    dest = archive_dir(layout, task_id)
    moved = []
    for p in sorted(layout.jobs.glob(f"{task_id}.*.json")):
        try:
            if _cached_read(p).get("phase") not in ("DONE", "FAILED"):
                continue
        except (OSError, ValueError):
            continue
        dest.mkdir(parents=True, exist_ok=True)
        p.replace(dest / p.name)
        moved.append(p.stem)
        with _RECORD_CACHE_LOCK:
            _RECORD_CACHE.pop(str(p), None)
    if moved:
        _invalidate_archive_marker(layout)   # P3-R2-B: files just moved in that the marker has not yet seen
    return moved


# P3-R2-B (RELEASE_REVIEW_2.8.0_round2.md): the loop's start-up guard used to read and parse every archived job
# record on every process start, once the archive holds thousands of finished tasks' history that never changes
# again. This marker lets it skip that read once the archive has already been found, in full, to hold nothing but
# migration-stamped or allocator-written records -- the live top level is cheap enough (bounded by work in
# flight, not by history) to keep scanning every start-up regardless.
_ARCHIVE_MARKER_NAME = ".archive-migrated"


def _archive_marker_path(layout: Layout) -> Path:
    return layout.jobs / "archive" / _ARCHIVE_MARKER_NAME


def archive_known_migrated(layout: Layout) -> bool:
    """Whether the start-up guard can trust the archive without re-reading it: `mark_archive_migrated` wrote this
    the last time the whole archive scanned clean, and nothing has moved a new file into it since."""
    return _archive_marker_path(layout).exists()


def mark_archive_migrated(layout: Layout) -> None:
    """Record that every file currently under the archive is migration-stamped or allocator-written."""
    p = _archive_marker_path(layout)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(p, {"checked": now_iso()})


def _invalidate_archive_marker(layout: Layout) -> None:
    with contextlib.suppress(OSError):
        _archive_marker_path(layout).unlink()


def attempt_for_digest(layout: Layout, task_id: str, digest: str) -> int | None:
    """The delivery attempt a past review digest belongs to, read from this task's own review job records (the
    store, never a run directory): the newest review job whose `review_digest` matches it names its own
    `attempt` — the same join `review._delivery_for_digest` uses from a verify job, available here too because
    every review job already carries `attempt` once dispatched through the allocator (ATTEMPT_MODEL.md step 9).
    `None` when no such record exists (a digest reviewed before this field existed, or never reviewed at all)."""
    matches = [r for r in for_task(layout, task_id) if kind_of(r) == "review" and r.get("review_digest") == digest]
    return attempt_of(matches[-1]) if matches else None


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
