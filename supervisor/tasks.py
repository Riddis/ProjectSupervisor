"""Task store: load/validate/import, eligibility, the single transition function `advance()`, manual transitions.

Rules:
- every status change happens here, inside the store lock, deciding from the freshly read document (no stale reads);
- `_evaluate()` is the one rule: verification ∧ every required review ACCEPT on the current digest ∧ (no approval
  needed ∨ owner ACCEPT for this digest) → COMPLETE. `advance()` applies it; `completion_check()` reports it;
- a new delivery digest supersedes older verification/review/approval records;
- `set_status()` never reaches COMPLETE except when the owner forces it with a note (persisted, rendered, logged).
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml
from pydantic import ValidationError

from . import gates as gates_mod
from . import policy as policy_mod
from . import store
from .models.task import (ALLOWED, TERMINAL, ApprovalRecord, ApprovalResponse, Attempt, HumanApproval,
                          ReviewRecord, ReviewVerdict, Task, TaskStatus, VerificationRecord)
from .paths import Layout

ADVANCEABLE = {TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_REVIEW, TaskStatus.WAITING_FOR_HUMAN, TaskStatus.FAILED}
CONTRACT_FIELDS = ["phase", "title", "objective", "owner_role", "depends_on", "actions", "artifacts", "tests",
                   "checks", "owned_paths", "reviewer", "review_note", "acceptance", "escalation", "human_approval",
                   "approval_note", "cost_category", "evidence", "test_roots"]
# A refresh is a whole-contract replace: a seed row that omits a CONTRACT_FIELDS key clears it (R-07d, by design —
# use patch_task for a change to one field without resending the rest). These four are the exception: once set,
# they outlive a seed row that does not mention them, the same way the store itself (migrate_ownership) may set
# owned_paths outside any seed. evidence/test_roots joined CONTRACT_FIELDS in this change; without the exemption a
# refresh touching only an unrelated field would have silently reset them to their defaults.
REFRESH_STICKY_FIELDS = frozenset({"owned_paths", "checks", "evidence", "test_roots"})
# `job` is a contract field too (what a task runs), but it keeps its own writer (apply_job/task.set_job, D-018):
# never copied by a refresh or a patch. A seed or patch row that carries one is reported, not silently dropped.


class TaskError(ValueError):
    pass


def ownership_errors(tasks: Iterable[Task], layout: Layout | None = None) -> list[str]:
    """A task whose delivery runs on a model lane must say what it owns — explicit roots, or `["."]` for
    exclusive mode. Applied to tasks being created or imported; the runtime (supervisor.gate) treats an old record
    without it as exclusive, and `migrate_ownership()` makes that explicit."""
    from . import scheduler                      # local: scheduler imports this module
    return [f"{t.id}: a {scheduler.adapter_name(t, layout)} task must declare owned_paths (explicit roots, or ['.'] for exclusive mode)"
            for t in tasks if scheduler.writing_lane(t, layout) and not t.owned_paths]


# ------------------------------------------------------------------ loading

def path_for(layout: Layout, task_id: str) -> Path:
    return layout.tasks / f"{task_id}.yaml"


def load(layout: Layout, task_id: str) -> Task:
    p = path_for(layout, task_id)
    if not p.exists():
        raise TaskError(f"no such task {task_id}")
    try:
        doc = store.read(p)
    except Exception as exc:
        raise TaskError(f"{task_id}: unreadable: {exc}") from exc
    try:
        return Task.model_validate(doc)
    except ValidationError as exc:
        raise TaskError(f"{task_id}: {exc}") from exc


# Last error per (project root, file name), so a file that keeps failing is logged once, not on every load (R-02;
# the same dedup render.py's _guarded already uses for a failing view).
_last_task_error: dict[tuple[str, str], str] = {}


def _report_unreadable(layout: Layout, path: Path, exc: Exception) -> None:
    msg = f"{type(exc).__name__}: {exc}"[:300]
    key = (str(layout.root), path.name)
    if _last_task_error.get(key) != msg:
        _last_task_error[key] = msg
        log(layout, "task_unreadable", "loop", path=path.name, error=msg)
    from . import inbox as inbox_mod  # local: inbox imports this module
    try:
        inbox_mod.create(layout, for_role="owner", subject=f"Task file unreadable: {path.name}",
                         summary=f"{path.name} could not be loaded and is skipped: {msg}", kind="question",
                         from_="loop", dedup_key=f"unreadable_task:{path.name}")
    except inbox_mod.InboxFull:
        pass


# The current loop turn's own in-memory copy of the store (R-09): while active, load_all() returns a snapshot of
# this dict instead of re-parsing every task file, and _update() keeps it in step with every write this turn makes,
# so a step that reads a task an earlier step of the SAME turn changed sees the change. None outside a turn (the
# CLI, a UI request, a test that never opens one): every caller there still gets a real, fresh scan.
_TURN_SNAPSHOT: contextvars.ContextVar[dict[str, Task] | None] = contextvars.ContextVar("supervisor_turn_tasks", default=None)


@contextlib.contextmanager
def turn_snapshot(tasks: dict[str, Task]):
    """Hold `tasks` (already loaded) as the store for every `load_all()` call made inside the block, in this
    thread, kept current by `_update()`. One loop turn opens this once, after its own single real load (`once()`);
    nothing inside the block pays for a second one."""
    token = _TURN_SNAPSHOT.set(tasks)
    try:
        yield
    finally:
        _TURN_SNAPSHOT.reset(token)


# A parse shared by every caller in the process, not only one turn's own snapshot (C-1): keyed by path, kept only
# while a file's size and mtime match the stamp it was parsed at. A Control Center request that follows an
# unchanged file pays for a stat, not a re-parse; the loop's own once-per-turn scan benefits the same way.
# Unbounded for the life of the process, same tradeoff `ui.live`'s own fingerprint cache already makes — a finished
# task's file stops changing, so this never grows faster than the store itself. An index that forgets a task once
# it is COMPLETE (R-09's "Step 2") is a separate, larger change.
_PARSE_CACHE: dict[str, tuple[tuple[int, int], Task]] = {}
_PARSE_CACHE_LOCK = threading.Lock()


def _cached_task(p: Path) -> tuple[Task | None, Exception | None]:
    """(the parsed Task, None) or (None, the exception) for `p`, reusing `_PARSE_CACHE` while its stamp (mtime,
    size) has not moved. Raises OSError straight through (a vanished file mid-scan: the caller already handles
    that for its own glob)."""
    st = p.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    key = str(p)
    with _PARSE_CACHE_LOCK:
        hit = _PARSE_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1], None
    try:
        t = Task.model_validate(store.read(p))
    except Exception as exc:                     # noqa: BLE001 - one bad file must never stop the loop
        return None, exc
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE[key] = (stamp, t)
    return t, None


def _scan_disk(layout: Layout) -> dict[str, Task]:
    """The real glob and parse: every task file read and validated once (reusing `_PARSE_CACHE` for one that has
    not changed). The only place `load_all()` actually touches the store (R-09) — called once per turn_snapshot()
    block, never from inside one."""
    out = {}
    for p in sorted(layout.tasks.glob("*.yaml")):
        try:
            t, parse_exc = _cached_task(p)
        except OSError as stat_exc:
            _report_unreadable(layout, p, stat_exc)
            continue
        if parse_exc is not None:
            _report_unreadable(layout, p, parse_exc)
            continue
        out[t.id] = t
    return out


def load_all(layout: Layout) -> dict[str, Task]:
    """Every task that parses and validates. A file that does not is skipped, not raised (R-02): it is reported
    once (`_report_unreadable`) and left out of the graph, so a dependent's `unmet_dependency` falls through to
    `load()` and reports it as "<id> (unreadable)" rather than treating it as done. Use `unreadable()` for a view
    that must show the skipped file, not merely work around it.

    Inside an active `turn_snapshot()`, this returns a fresh dict copy of the shared snapshot instead of
    re-scanning the store (R-09): one loop turn calls nine functions that each used to load_all() on their own,
    parsing 365 files nine times over."""
    snap = _TURN_SNAPSHOT.get()
    return dict(snap) if snap is not None else _scan_disk(layout)


def unreadable(layout: Layout) -> dict[str, str]:
    """The task files `load_all` is currently skipping, as {file stem: short error} — for a page that must show a
    broken file as such. Shares `_PARSE_CACHE` with `load_all`/`_scan_disk`: called right after it (as
    `overview_context` does), this costs a stat per file, not a second parse of all 365."""
    out = {}
    for p in sorted(layout.tasks.glob("*.yaml")):
        try:
            _, parse_exc = _cached_task(p)
        except OSError as stat_exc:
            out[p.stem] = f"{type(stat_exc).__name__}: {stat_exc}"[:300]
            continue
        if parse_exc is not None:
            out[p.stem] = f"{type(parse_exc).__name__}: {parse_exc}"[:300]
    return out


def validate_graph(tasks: dict[str, Task]) -> list[str]:
    errors = []
    for t in tasks.values():
        for d in t.depends_on:
            if d not in tasks:
                errors.append(f"{t.id}: unknown dependency {d}")
            elif tasks[d].phase > t.phase:
                errors.append(f"{t.id}: depends on later-phase task {d}")
    seen: dict[str, int] = {}

    def visit(n, stack):
        if n in stack:
            errors.append(f"dependency cycle through {n}")
            return
        if seen.get(n):
            return
        for m in tasks[n].depends_on if n in tasks else []:
            visit(m, stack | {n})
        seen[n] = 1

    for n in tasks:
        visit(n, set())
    return errors


# {name}, S1..S6, EXP-1xx, T10-xx, round_N: shorthand a person reads — usually, but not always, never a file that will
# exist (`out/xx.json` and `round_N_results.txt` are legal names), so these are warnings, not refusals
_PLACEHOLDER = re.compile(r"[{}]|\.\.(?!/)|(?<![A-Za-z])[0-9]*xx(?![A-Za-z])|_N(?![A-Za-z0-9])")
# characters no file name can hold on Windows (`<id>` is the usual placeholder); `*?` are globs, `:` is checked as a drive
_ILLEGAL = re.compile(r'[<>"|\x00-\x1f]')


def _matches_on_disk(root: Path | None, a: str) -> bool:
    if root is None:
        return False
    try:
        if any(c in a for c in "*?["):
            return any(p.is_file() for p in root.glob(a))
        return (root / a).is_file()
    except (OSError, ValueError):
        return False


def artifact_problems(tasks: Iterable[Task], root: Path | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for artifact entries. loop._expand_artifacts reads each entry as a file path relative to the
    root, or a glob when it contains *?[.

    Errors — structurally no file can ever satisfy the entry, so it would fail every delivery forever and a new or
    re-imported contract carrying one is refused: empty or padded entries, absolute or escaping paths, directories
    ("cycle_N/", or with `root` an existing directory), characters no file name can hold ("<id>"), and a path under
    `runs/` or under `state/` outside `AUTHORED_STATE` — the loop drops both as evidence, never a deliverable
    (G4, 2.4.0), so no artifact entry there can ever be satisfied.

    Warnings — the spelling looks like prose or a placeholder ("runs contact sheets", "S1..S6", "EXP-1xx",
    "round_N") but is a legal file name, and real projects do name files "Final Report.md" or "out/xx.json". The
    contract is accepted and the warning returned to the caller; an entry that already matches a file under `root`
    raises none."""
    errors, warnings = [], []
    for t in tasks:
        for a in t.artifacts:
            why = None
            if not a or a != a.strip():
                why = "is empty or has leading/trailing whitespace"
            elif a.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", a) or ".." in Path(a).parts:
                why = "must be a path relative to the project root"
            elif a.endswith(("/", "\\")) or (root is not None and not any(c in a for c in "*?[")
                                                and (root / a).is_dir()):
                why = "is a directory — list its files or a glob such as dir/*"
            elif a.startswith("runs/") or (a.startswith("state/") and not a.startswith(AUTHORED_STATE)):
                why = "is under runs/ or state/ (outside state/decisions/) — never a deliverable (G4, 2.4.0)"
            elif _ILLEGAL.search(a):
                why = "has a placeholder or a character no file name can hold (<...>, \", |) — use a glob such as out/*/summary.json"
            if why:
                errors.append(f"{t.id}: artifact {a!r} {why}")
                continue
            if _matches_on_disk(root, a):
                continue
            if any(ch.isspace() for ch in a):
                warnings.append(f"{t.id}: artifact {a!r} contains spaces — fine for a real file name; if it is a "
                                "description, name the file or a glob instead")
            elif _PLACEHOLDER.search(a):
                warnings.append(f"{t.id}: artifact {a!r} looks like a placeholder ({{...}}, a..b, xx, _N) — fine if "
                                "that is the real file name; otherwise use a glob such as S[1-6].yaml")
    return errors, warnings


def artifact_errors(tasks: Iterable[Task], root: Path | None = None) -> list[str]:
    """Artifact entries no file can ever satisfy (artifact_problems errors)."""
    return artifact_problems(tasks, root)[0]


def contract_warnings(tasks: Iterable[Task], layout: Layout) -> list[str]:
    """What a contract asks that the project cannot give, though the contract itself is valid (1.13.0):
    - an `owner_role` or a `reviewer` that config/routing.yaml does not configure (only when it configures roles);
    - one that is inactive (`active: false`), which parks the task until the role is activated;
    - a check command outside `verification.allowed_test_commands`, which would fail only at verification;
    - a `job` whose adapter, or whose python `fn`, neither the framework nor a plugin's manifest provides, which
      would fail only when it ran. No plugin is set up for this (`_registered_lanes`).
    Warnings, not errors: a project whose seed already holds one keeps importing."""
    from . import gate, verify
    roles = gate.routing_doc(layout).get("roles")
    roles = roles if isinstance(roles, dict) else {}
    allowed = verify.allowed_test_commands(layout)
    out = []
    loaded = None
    for t in tasks:
        job = (getattr(t, "model_extra", None) or {}).get("job")
        if isinstance(job, dict) and job.get("adapter"):
            if loaded is None:
                loaded = _registered_lanes(layout)
            lanes, python_jobs, unloaded = loaded
            if job["adapter"] not in lanes:
                out.append(f"{t.id}: job adapter {job['adapter']!r} is not a registered lane{unloaded}; the job "
                           f"would fail when it ran")
            elif job["adapter"] == "python" and job.get("fn", "noop") not in python_jobs:
                out.append(f"{t.id}: job fn {job.get('fn')!r} is not a registered python job{unloaded}; the job "
                           f"would fail when it ran")
        named = [("owner_role", t.owner_role)] + [("reviewer", r) for r in t.reviewer]
        for field_name, role in named:
            if not role:
                continue
            if roles and role not in roles:
                out.append(f"{t.id}: {field_name} {role!r} is not a role in config/routing.yaml; the task would wait "
                           f"for it forever")
            elif gate.role_inactive(layout, role):
                out.append(f"{t.id}: {field_name} {role!r} is inactive (routing.yaml roles.{role}.active: false); "
                           f"the task waits until it is activated")
        for check in t.checks:
            if not verify.permitted(check.command, allowed):
                out.append(f"{t.id}: check {check.command!r} is not in verification.allowed_test_commands; "
                           f"verification would refuse it")
    return out


def _registered_lanes(layout: Layout) -> tuple[set[str], set[str], str]:
    """The adapter and python job names the framework registers, plus those each configured plugin's manifest
    declares (`provides`, which G5 holds to what its setup() registers), and a note for the warning naming any
    plugin whose names could not be read. Import-free: no plugin is set up, so `task import --dry-run` and
    `supervisor check` write nothing (review 1.13.0 round 2)."""
    from . import plugins
    lanes, python_jobs = plugins._built_in("adapters"), plugins._built_in("python_jobs")
    unread = []
    found = plugins.approval(layout)
    for name, _required in found.entries:
        m = found.finds[name].manifest
        if name in found.refused or m is None:
            unread.append(name)
            continue
        lanes |= set(m.provides.get("adapters", ()))
        python_jobs |= set(m.provides.get("python_jobs", ()))
    note = (f" (not checked against {', '.join(sorted(unread))}: no usable manifest, see `supervisor doctor`)"
            if unread else "")
    return lanes, python_jobs, note


def unmet_dependency(layout: Layout, task: Task, tasks: dict[str, Task] | None = None) -> str | None:
    """The first entry of `task.depends_on` that is not COMPLETE, as "<id> (<status>)" (G6, 2.4.0) — a dependency
    this project has no task for is reported as "<id> (missing)", and one whose file exists but `load_all` skipped
    (R-02) as "<id> (unreadable)", so a task waiting on a broken file stays waiting rather than being released as
    though the dependency did not exist. `tasks` is an optional preloaded graph (`eligible()`'s own cache); without
    it each dependency is loaded from disk."""
    for dep_id in task.depends_on:
        dep = (tasks or {}).get(dep_id)
        if dep is None:
            try:
                dep = load(layout, dep_id)
            except TaskError:
                return f"{dep_id} (unreadable)" if path_for(layout, dep_id).exists() else f"{dep_id} (missing)"
        if dep.status != TaskStatus.COMPLETE:
            return f"{dep_id} ({dep.status.value})"
    return None


def eligible(layout: Layout, tasks: dict[str, Task] | None = None) -> list[Task]:
    tasks = tasks or load_all(layout)
    gate_data = gates_mod.load(layout)
    out = [t for t in tasks.values()
           if t.status in (TaskStatus.NOT_STARTED, TaskStatus.READY)
           and unmet_dependency(layout, t, tasks) is None
           and gates_mod.phase_open(layout, t.phase, gate_data)]
    return sorted(out, key=lambda t: (-t.priority, t.phase, t.id))


def current_phase(tasks: dict[str, Task]) -> int:
    """The lowest phase with a task still open. COMPLETE and CANCELLED both close a task, so a cancelled leftover
    never holds the project in an earlier phase; once every task is closed it is the last phase that had tasks."""
    open_phases = [t.phase for t in tasks.values() if t.status not in TERMINAL]
    return min(open_phases, default=max((t.phase for t in tasks.values()), default=0))


def active(tasks: dict[str, Task]) -> list[Task]:
    """The tasks that count towards progress: every task except the CANCELLED ones."""
    return [t for t in tasks.values() if t.status != TaskStatus.CANCELLED]


def check_new(layout: Layout, doc: dict, combined: dict[str, Task] | None = None) -> tuple[dict, list[str], list[str]]:
    """(the stored document, errors, warnings) for a task `create()` would add: the contract (pydantic), the graph
    with it in place, its ownership and its artifacts; `contract_warnings` stay warnings. `combined` (default: the
    store) is the graph to check it against, so a caller that changes another task in the same step
    (diagnosis.ingest's dependency) checks the graph as it will be. Raises ValidationError on a bad contract."""
    doc = dict(doc)
    doc.setdefault("status", "NOT_STARTED")
    doc.update({"attempts": [], "result": None, "paths": [], "decisions": []})
    task = Task.model_validate(doc)                       # fail before touching the graph or the disk
    combined = dict(load_all(layout) if combined is None else combined)
    combined[task.id] = task
    art_errors, warnings = artifact_problems([task], root=layout.root)
    warnings += contract_warnings([task], layout)
    return doc, validate_graph(combined) + ownership_errors([task], layout) + art_errors, warnings


def create(layout: Layout, doc: dict, actor: str = "system") -> dict:
    """Add a task that is not in the seed (probes, owner-added work, Control Center conversions). Validated like
    the seed; an existing id is a no-op, so a retried command (or a second click) is never a duplicate."""
    doc = dict(doc)
    path = path_for(layout, doc["id"])
    if path.exists():
        return {"skipped": "exists", "id": doc["id"]}
    doc, errors, warnings = check_new(layout, doc)
    if errors:
        raise TaskError("; ".join(errors))
    store.write_new(path, doc)
    log(layout, "task_created", actor, task=doc["id"], **({"warnings": warnings} if warnings else {}))
    out = {"id": doc["id"], "status": doc["status"]}
    if warnings:
        out["warnings"] = warnings
    return out


def import_seed(layout: Layout, seed_path: Path, update_ids: Iterable[str] = (), create: bool = True,
                actor: str = "cli", dry_run: bool = False) -> dict:
    """Create tasks that do not exist yet (unless `create` is False); refresh only the named tasks' contract fields.
    Idempotent. Runs in-process for the CLI when no supervisor is live, and as the `task.import` command otherwise.

    Validates the graph exactly as it will read once this call returns (R-07a/b): a row this call does not create
    or refresh keeps its STORED contract for validation, never the seed's version of it — a seed cannot pass only
    because of a row it will not actually write. Current contract rules (artifacts, ownership, routing warnings)
    apply only to rows this call creates or changes, so an old row that predates a rule added since it was written
    is neither refused nor warned about just for being named in the seed. All or nothing: every row is validated
    before any row is written. `job` has its own writer (`task.set_job`, D-018) and is never copied by a refresh;
    a seed row that carries one for a row being updated is named in `job_not_applied`, not silently dropped.

    `dry_run` runs the same validation and reports what would be created and updated, writing and logging
    nothing — safe while a supervisor runs."""
    seed = yaml.safe_load(Path(seed_path).read_text(encoding="utf-8"))["tasks"]
    update_ids = set(update_ids)
    existing = load_all(layout)
    resulting = dict(existing)            # the real graph this call will leave on disk, built up below
    changed: list[Task] = []              # rows being created or refreshed: only these take current contract rules
    new_docs: dict[str, dict] = {}        # id -> the document a create() would write
    created, updated, untouched, ignored, job_not_applied = [], [], [], [], []

    for t in seed:
        tid = t["id"]
        if tid not in existing:
            if not create:
                ignored.append(tid)
                continue
            doc = dict(t)
            doc.setdefault("status", TaskStatus.NOT_STARTED.value)      # the store keeps the runtime fields explicit
            doc.update({"attempts": [], "result": None, "paths": [], "decisions": []})
            task = Task.model_validate(doc)                              # fail before touching the graph or the disk
            resulting[tid], new_docs[tid] = task, doc
            changed.append(task)
            created.append(tid)
        elif tid in update_ids:
            merged = _refresh_contract(dict(store.read(path_for(layout, tid))), t)   # validates in memory
            resulting[tid] = Task.model_validate(merged)
            changed.append(resulting[tid])
            updated.append(tid)
            if "job" in t:
                job_not_applied.append(tid)
        else:
            untouched.append(tid)

    art_errors, warnings = artifact_problems(changed, root=layout.root)
    warnings += contract_warnings(changed, layout)
    errors = validate_graph(resulting) + ownership_errors(changed, layout) + art_errors
    if errors:
        raise TaskError("seed invalid: " + "; ".join(errors))   # nothing written

    if not dry_run:
        for tid in created:
            store.write_new(path_for(layout, tid), new_docs[tid])
        for t in seed:
            if t["id"] in updated:
                store.update(path_for(layout, t["id"]), lambda doc, t=t: _refresh_contract(doc, t),
                            lock_path=layout.state / ".tasks.lock")
        log(layout, "task_import", actor, created=created, updated=updated, **({"warnings": warnings} if warnings else {}))

    out = {"created": created, "updated": updated, "untouched": untouched, "ignored": ignored}
    if job_not_applied:
        out["job_not_applied"] = job_not_applied
    if dry_run:
        out["dry_run"] = True
    if warnings:
        out["warnings"] = warnings
    return out


def _refresh_contract(doc: dict, t: dict) -> dict:
    """Copy the seed's contract fields onto a stored task document and validate the result. Whole-contract
    replace except REFRESH_STICKY_FIELDS (R-07d): an omitted key elsewhere is cleared, an omitted sticky one is
    left at whatever the document already had."""
    for k in CONTRACT_FIELDS:
        if k in t:
            doc[k] = t[k]
        elif k not in REFRESH_STICKY_FIELDS:
            doc.pop(k, None)
    Task.model_validate(doc)
    return doc


PATCH_FIELDS = frozenset(CONTRACT_FIELDS) | {"add_dependency", "remove_dependency"}


def _apply_patch(doc: dict, fields: dict) -> dict:
    """Apply one patch's fields onto a stored task document (mutates and returns it, validated): a direct
    CONTRACT_FIELDS assignment, or `add_dependency`/`remove_dependency` (an id, or a list) against the document's
    OWN current `depends_on` — never a caller's possibly-stale copy of it, so two concurrent dependency edits
    cannot race each other."""
    deps = list(doc.get("depends_on") or [])
    for dep in ([fields["add_dependency"]] if isinstance(fields.get("add_dependency"), str)
                else list(fields.get("add_dependency") or [])):
        if dep not in deps:
            deps.append(dep)
    for dep in ([fields["remove_dependency"]] if isinstance(fields.get("remove_dependency"), str)
                else list(fields.get("remove_dependency") or [])):
        if dep in deps:
            deps.remove(dep)
    if "add_dependency" in fields or "remove_dependency" in fields:
        doc["depends_on"] = deps
    for k, v in fields.items():
        if k in CONTRACT_FIELDS:
            doc[k] = v
    Task.model_validate(doc)
    return doc


def patch_task(layout: Layout, task_id: str, fields: dict, actor: str = "cli") -> dict:
    """Change named contract fields of one existing task without resending the whole contract (R-07d). Validated
    exactly like a refresh (R-07a/b): the graph as it will actually be once this call returns, with current
    contract rules applied only to this one task."""
    bad = set(fields) - PATCH_FIELDS
    if bad:
        raise TaskError(f"{task_id}: patch may set only a contract field, add_dependency or remove_dependency, "
                        f"not {', '.join(sorted(bad))}")
    existing = load_all(layout)
    if task_id not in existing:
        raise TaskError(f"no such task {task_id}")
    merged = _apply_patch(dict(store.read(path_for(layout, task_id))), fields)
    task = Task.model_validate(merged)
    combined = dict(existing)
    combined[task_id] = task
    art_errors, warnings = artifact_problems([task], root=layout.root)
    warnings += contract_warnings([task], layout)
    errors = validate_graph(combined) + ownership_errors([task], layout) + art_errors
    if errors:
        raise TaskError("patch invalid: " + "; ".join(errors))
    store.update(path_for(layout, task_id), lambda doc: _apply_patch(doc, fields), lock_path=layout.state / ".tasks.lock")
    log(layout, "task_patch", actor, task=task_id, fields=sorted(fields))
    out = {"id": task_id}
    if warnings:
        out["warnings"] = warnings
    return out


def migrate_ownership(layout: Layout, actor: str = "cli") -> dict:
    """One-off migration: every live task on a writing lane that declares no ownership becomes explicitly exclusive
    (`owned_paths: ["."]`), with a note in its record. Terminal tasks are left alone. Idempotent."""
    from . import scheduler
    from .models.task import EXCLUSIVE, TERMINAL
    migrated = []
    for t in load_all(layout).values():
        if t.status in TERMINAL or t.owned_paths or not scheduler.writing_lane(t, layout):
            continue

        def fn(doc):
            doc["owned_paths"] = [EXCLUSIVE]
            doc.setdefault("notes", []).append({"at": store.now_iso(), "by": actor,
                                                "note": "owned_paths set to ['.'] (exclusive mode) by migrate-ownership"})
        _update(layout, t.id, fn)
        migrated.append(t.id)
    log(layout, "ownership_migrated", actor, tasks=migrated)
    return {"migrated": migrated}


# ------------------------------------------------------------------ logging

_COMMAND: contextvars.ContextVar[dict | None] = contextvars.ContextVar("supervisor_command", default=None)


@contextlib.contextmanager
def command_scope(command_id: str | None):
    """While a queued command's handler runs (commands.apply_all), every log line it writes carries the command's
    id, and the yielded dict counts them: a handler that logged its own event needs no separate generic line."""
    scope = {"id": command_id, "logged": 0}
    token = _COMMAND.set(scope)
    try:
        yield scope
    finally:
        _COMMAND.reset(token)


def log(layout: Layout, event: str, actor: str = "system", **fields):
    """The single append path to state/log.jsonl. Everything a caller supplies is screened here: the log is read
    by the Control Center, quoted into revision packets and committed in projections, so a credential that
    reaches it has effectively left `secrets/`. Redacted, not refused — losing an audit record would be the
    worse failure.

    Named, not asserted in general: `**fields` and everything nested inside them go through `redact_fields()`,
    which also blanks any value long enough to be a credential while the store cannot be enumerated; `actor`
    and `event` go through `screen_log_identity()`, which screens them for literals and patterns but never
    blanks them, so the trail stays readable. `actor` needs it because it is data — `commands.apply_all()`
    reads it from a queued `state/commands/*.json` file. `ts` is this machine's clock and is the one value no
    caller supplies."""
    layout.log.parent.mkdir(parents=True, exist_ok=True)
    cmd = _COMMAND.get()
    if cmd is not None:                       # written while a command runs: carry its id (see command_scope)
        fields.setdefault("command", cmd["id"])
        cmd["logged"] += 1
    record = {"ts": store.now_iso(),
              "actor": policy_mod.screen_log_identity(actor, layout),
              "event": policy_mod.screen_log_identity(event, layout),
              **policy_mod.redact_fields(fields, layout)}
    store.append_line(layout.log, json.dumps(record, ensure_ascii=False))     # the log's own line endings


# ------------------------------------------------------------------ the rule

def _evaluate(task: Task) -> tuple[TaskStatus, str]:
    """Next status implied by the delivery records of the CURRENT digest (pure function)."""
    d = task.delivery
    if not d.digest:
        return task.status, "no delivery recorded"
    if not d.verification or d.verification.digest != d.digest:
        return task.status, "awaiting verification for the current digest"
    if not d.verification.ok:
        return TaskStatus.FAILED, "verification failed: " + (d.verification.detail or "")
    live = [r for r in d.reviews if r.digest == d.digest and not r.superseded]
    latest = {}
    for r in live:
        latest[r.role] = r
    # Every required role must report before the task leaves WAITING_FOR_REVIEW: short-circuiting on the first
    # role in listed order used to move the task to IN_PROGRESS/BLOCKED the instant that role reported, even
    # though a later-listed role's review had not been dispatched yet — pending() only dispatches for
    # WAITING_FOR_REVIEW tasks, so the round could never complete.
    missing = [role for role in task.reviewer if role not in latest]
    if missing:
        return TaskStatus.WAITING_FOR_REVIEW, f"awaiting review from {', '.join(missing)}"
    blocked = [role for role in task.reviewer if latest[role].verdict == ReviewVerdict.BLOCKED]
    if blocked:
        return TaskStatus.BLOCKED, f"review from {blocked[0]} is BLOCKED"
    failing = [role for role in task.reviewer if latest[role].verdict != ReviewVerdict.ACCEPT]
    if failing:
        return TaskStatus.IN_PROGRESS, f"review from {failing[0]} is {latest[failing[0]].verdict.value}"
    if task.human_approval != HumanApproval.none:
        approvals = [a for a in d.approvals if a.digest == d.digest and not a.superseded]
        if not approvals:
            return TaskStatus.WAITING_FOR_HUMAN, "awaiting owner approval for the current digest"
        if approvals[-1].response != ApprovalResponse.ACCEPT:
            return TaskStatus.IN_PROGRESS, f"owner response is {approvals[-1].response.value}"
    return TaskStatus.COMPLETE, "verified, reviewed and approved as required"


def completion_check(task: Task) -> str | None:
    """None when the task may COMPLETE; otherwise why not."""
    target, note = _evaluate(task)
    return None if target == TaskStatus.COMPLETE else note


# ------------------------------------------------------------------ mutation helpers (all decide on the locked doc)

def _update(layout: Layout, task_id: str, fn: Callable[[dict], None]) -> Task:
    def wrapped(doc):
        fn(doc)
        Task.model_validate(doc)      # never persist an invalid task
        return doc
    doc = store.update(path_for(layout, task_id), wrapped, lock_path=layout.state / ".tasks.lock")
    t = Task.model_validate(doc)
    snap = _TURN_SNAPSHOT.get()
    if snap is not None:              # this turn's snapshot, if one is open: later steps must see this write (R-09)
        snap[t.id] = t
    return t


def _record_history(doc: dict, to: TaskStatus, by: str, note: str, forced: bool = False):
    entry = {"at": store.now_iso(), "by": by, "from": doc.get("status"), "to": to.value, "note": note}
    if forced:
        entry["forced"] = True
    doc.setdefault("history", []).append(entry)
    doc["status"] = to.value


class _NoChange(Exception):
    pass


# The loop's own automatic writers (F11 already keeps both out of a held task's move off BLOCKED in practice -
# the gate and `_ensure_in_progress` refuse the launch, `diagnosis.ingest` declines before this runs); named here
# so release stays a question of *which actors may decide to release a Hold*, not a prefix guess at "the owner"
# (F13) - anyone else moving the task off BLOCKED is a deliberate, non-automatic decision and releases it.
AUTOMATIC_ACTORS = frozenset({"loop", "escalation"})


def set_status(layout: Layout, task_id: str, status: TaskStatus | str, actor: str = "system", note: str = "",
               force: bool = False) -> Task:
    """Manual/loop transition following ALLOWED. COMPLETE is refused here (use advance()) unless the OWNER forces it
    with a note; a forced change is persisted on the task (`forced`), rendered, and logged."""
    status = TaskStatus(status)
    if force and (actor != "owner" or not note.strip()):
        raise TaskError("forcing a status requires actor=owner and a non-empty note")
    if status == TaskStatus.COMPLETE and not force:
        raise TaskError(f"{task_id}: COMPLETE is only reachable through delivery records (advance) or an owner force")
    before = {}

    def fn(doc):
        current = TaskStatus(doc.get("status") or TaskStatus.NOT_STARTED.value)
        before["status"] = current.value
        if current == status:
            raise _NoChange()
        if status not in ALLOWED[current] and not force:
            raise TaskError(f"{task_id}: illegal transition {current.value} → {status.value}")
        _record_history(doc, status, actor, note, forced=force)
        if doc.get("held") and actor not in AUTOMATIC_ACTORS:
            doc["held"] = None    # any actor but the loop's own (F9/F13) releases a Hold on a move off BLOCKED
        if force:
            doc["forced"] = {"by": actor, "at": store.now_iso(), "to": status.value, "note": note}
    try:
        t = _update(layout, task_id, fn)
    except _NoChange:
        return load(layout, task_id)
    log(layout, "task_status", actor, task=task_id, **{"from": before.get("status"), "to": status.value}, note=note, forced=force)
    if status == TaskStatus.CANCELLED or (status == TaskStatus.COMPLETE and force):
        # R-04: a move to CANCELLED, or an owner-forced COMPLETE, must retire the task's own jobs in the same
        # breath — a queued one never launches, a running one is terminated — or a job dispatched before this
        # call still writes into the tree and spends a session on work the owner just stopped.
        from . import jobs as jobs_mod
        closed = jobs_mod.retire(layout, task_id, reason=f"task moved to {status.value}", actor=actor)
        if closed:
            log(layout, "jobs_retired", actor, task=task_id, jobs=closed, to=status.value)
    return t


def hold(layout: Layout, task_id: str, actor: str, note: str = "") -> Task:
    """The owner's one explicit stop control (`ui/routes/tasks.py` `/hold`). Durable identity recorded on the
    task itself (`held`) rather than inferred from status, history order or an escalation event's `routed`
    flag - none of which can tell an owner's BLOCKED apart from the cap event's own BLOCKED (round-4 F9) - so
    the automatic path (`diagnosis.ingest`'s requeue, `loop._repair_capped_escalation`'s replay) can read it
    and refuse to undo it, even when the task is already BLOCKED, where `set_status()`'s same-status write
    would otherwise be a silent `_NoChange` with no history entry at all."""
    def fn(doc):
        current = TaskStatus(doc.get("status") or TaskStatus.NOT_STARTED.value)
        doc["held"] = {"by": actor, "at": store.now_iso(), "note": note}
        if current == TaskStatus.BLOCKED:
            doc.setdefault("history", []).append({"at": store.now_iso(), "by": actor, "from": current.value,
                                                   "to": current.value, "note": note or "held by owner"})
        else:
            if TaskStatus.BLOCKED not in ALLOWED[current]:
                raise TaskError(f"{task_id}: illegal transition {current.value} → {TaskStatus.BLOCKED.value}")
            _record_history(doc, TaskStatus.BLOCKED, actor, note or "held by owner")
    t = _update(layout, task_id, fn)
    log(layout, "task_held", actor, task=task_id, note=note)
    return t


PAGE_ACTIONS = ("hold", "cancel", "requeue")


def action_blocked_reason(action: str, status: TaskStatus) -> Optional[str]:
    """None when the named task-page action would succeed for a task in `status` - i.e. hold()/set_status()
    would not raise an illegal-transition error - else the reason to show, greyed, instead of letting the page
    offer an action its status refuses (R-18: five of eight failed owner commands in the records were exactly
    that). `action` is one of PAGE_ACTIONS; `requeue`'s target mirrors the real route's own choice."""
    if action == "hold":
        ok = status == TaskStatus.BLOCKED or TaskStatus.BLOCKED in ALLOWED[status]
    elif action == "cancel":
        ok = status == TaskStatus.CANCELLED or TaskStatus.CANCELLED in ALLOWED[status]
    elif action == "requeue":
        target = TaskStatus.NOT_STARTED if status == TaskStatus.CANCELLED else TaskStatus.READY
        ok = status == target or target in ALLOWED[status]
    else:
        raise ValueError(action)
    return None if ok else f"not available while the task is {status.value}"


def add_attempt(layout: Layout, task_id: str, resource: str, note: str = "", seed: int = 0, **extra) -> tuple[Task, str]:
    """Allocate the task's next job number from its own counter and record the attempt, in the same locked
    update, so the job record and the task's own attempt list can never disagree (R-03). `seed` covers a task
    with no counter yet (one written before this field existed): pass the highest attempt number already on
    disk so the number allocated here is never one already used. Returns the updated task and the id it chose;
    `note` gets that id appended.

    Superseded, for a fresh dispatch, by `allocate_job` (ATTEMPT_MODEL.md §2.3/§9, the writer switch) — this
    function and the id shape it writes (`<task>.a<n>`, one flat counter) are kept only because an un-migrated
    document and a handful of narrow, unrelated tests still read and write it; `loop.dispatch` no longer calls
    it."""
    allocated: dict[str, str] = {}

    def fn(doc):
        n = max(int(doc.get("job_seq") or 0), len(doc.get("attempts", [])), seed) + 1
        doc["job_seq"] = n
        attempt_id = f"{task_id}.a{n}"
        allocated["id"] = attempt_id
        doc.setdefault("attempts", []).append(Attempt(id=attempt_id, resource=resource, at=store.now_iso(),
                                                      note=f"{note} {attempt_id}".strip(), **extra)
                                              .model_dump(exclude_none=True))
    t = _update(layout, task_id, fn)
    return t, allocated["id"]


# ---- the attempt-identity allocator (§2.2, §2.3 of the attempt-identity design) — not yet called from the loop.
# A job's id is derived from four fields, never parsed back out of the string: task id, kind, seq (the ordinal of
# that kind inside its attempt) and the attempt number itself, or no attempt at all for a task-scoped job (today
# only a diagnosis raised before any delivery ever exists, §2.5).
_JOB_KIND_LETTERS = {"delivery": "s", "verify": "v", "review": "r", "diagnosis": "d"}


@dataclass(frozen=True)
class JobId:
    """A job's identity exactly as `allocate_job` minted it. `id` is derived for a file name, a URL, a log line
    and a human — never parsed back into these fields; every reader takes them from the job record instead."""
    task_id: str
    kind: str
    seq: int
    attempt: Optional[int] = None
    scope: Optional[str] = None
    cause: Optional[str] = None

    @property
    def id(self) -> str:
        k = _JOB_KIND_LETTERS[self.kind]
        if self.attempt is None:
            return f"{self.task_id}.{k}{self.seq}"
        return f"{self.task_id}.a{self.attempt}.{k}{self.seq}"


def allocate_job(layout: Layout, task_id: str, kind: str, *, attempt: int | None = None,
                 cause: str | None = None, new_attempt: bool = False, resource: str | None = None,
                 parent: str | None = None, note: str | None = None) -> JobId:
    """The one function that may invent a job id (§2.3): a seq drawn from the task's own `counters` block,
    allocated under the task lock in the same update that advances it. Counters are monotonic — nothing ever
    decrements them — so a job record lost between two allocations (a torn write, an antivirus lock, a manual
    cleanup, R-03) can never cause the next one to reuse an id. `attempt` names the delivery attempt this job
    belongs to; omitted (with `new_attempt=False`), the job is task-scoped (`scope="task"`), which today only a
    diagnosis raised before any delivery uses (§2.5). `new_attempt=True` (only ever a fresh delivery) opens the
    next attempt number instead (`counters.attempt + 1`), ignoring whatever `attempt` was passed.

    Called by `loop.dispatch` (step 9, the writer switch). In the SAME locked update, when this job belongs to
    an attempt, the task's own `attempts[]` gains the delivery-attempt entry (opened now, if this is the first
    job of a fresh `attempt` number) and this job's row inside its `jobs[]` (§2.7) — never a second, separate
    write a crash between the two could tear apart. A task-scoped job (diagnosis before any delivery) is never
    added to `attempts[]`: §2.5 shows it as a task-level row above Attempt 1, not inside one."""
    if kind not in _JOB_KIND_LETTERS:
        raise ValueError(f"unknown job kind: {kind!r}")
    allocated: dict[str, int] = {}

    def fn(doc):
        nonlocal attempt
        counters = doc.setdefault("counters", {})
        if new_attempt:
            attempt = int(counters.get("attempt") or 0) + 1
        if attempt is None:
            task_jobs = counters.setdefault("task_jobs", {})
            seq = int(task_jobs.get(kind, 0)) + 1
            task_jobs[kind] = seq
        else:
            counters["attempt"] = max(int(counters.get("attempt") or 0), int(attempt))
            per_attempt = counters.setdefault("jobs", {}).setdefault(attempt, {})
            seq = int(per_attempt.get(kind, 0)) + 1
            per_attempt[kind] = seq
        allocated["seq"] = seq
        if attempt is not None:
            job_id = JobId(task_id=task_id, kind=kind, seq=seq, attempt=attempt, cause=cause).id
            attempts = doc.setdefault("attempts", [])
            entry = next((a for a in attempts if a.get("n") == attempt), None)
            if entry is None:
                entry = {"n": attempt, "opened": store.now_iso(), "closed": None, "cause": cause,
                         "outcome": "open", "jobs": []}
                attempts.append(entry)
            entry.setdefault("jobs", []).append({"id": job_id, "kind": kind, "seq": seq, "resource": resource,
                                                 "parent": parent, "result": None, "note": note or ""})

    _update(layout, task_id, fn)
    return JobId(task_id=task_id, kind=kind, seq=allocated["seq"], attempt=attempt,
                scope=None if attempt is not None else "task", cause=cause)


def annotate_attempt(layout: Layout, task_id: str, attempt_id: str, **fields) -> Task:
    """Fill in fields (`run_dir`, once it is known) on a job already recorded by `add_attempt` (the old,
    one-entry-per-job shape: `a.id == attempt_id`) or by `allocate_job` (the new shape: `attempt_id` inside one
    attempt's `jobs[]`) — never reallocates a number or an id, either shape."""
    def fn(doc):
        for a in doc.get("attempts", []):
            if a.get("id") == attempt_id:
                a.update(fields)
                return
            for j in a.get("jobs") or []:
                if j.get("id") == attempt_id:
                    j.update(fields)
                    return
    return _update(layout, task_id, fn)


def current_attempt(layout: Layout, task_id: str) -> int | None:
    """The highest attempt number this task's own `counters` block has ever opened, or `None` when it has none
    yet — the task document's own record, never a job-id suffix parse. Used only as the bounded fallback for a
    diagnosis whose triggering event does not name a resolvable job (a stall key, or one from before this
    field existed): the live writer places such a diagnosis on the task's newest attempt rather than
    reconstructing "the newest attempt open at the time", which only the migration (§8.2) needs to do, from
    historical evidence this call has no reason to re-read."""
    try:
        n = load(layout, task_id).counters.get("attempt")
    except TaskError:
        return None
    return int(n) if n else None


def add_note(layout: Layout, task_id: str, note: str, actor: str = "owner", command_id: str | None = None,
             guidance: bool = False) -> Task:
    """Append a note; with `guidance` also hand the text to the next delivery brief (packets.guidance_block). Both go
    in ONE task update and each is idempotent per `command_id`, so a replayed command completes whatever an
    interruption left missing instead of skipping it (independent review of 1.6.0, finding 4)."""
    def fn(doc):
        notes = doc.setdefault("notes", [])
        if not command_id or not any(isinstance(n, dict) and n.get("command_id") == command_id for n in notes):
            entry = {"at": store.now_iso(), "by": actor, "note": note}
            if command_id:
                entry["command_id"] = command_id
            notes.append(entry)
        if guidance:
            items = doc.setdefault("guidance", [])
            key = f"note:{command_id}" if command_id else f"note:{store.now_iso()}"
            if not any(isinstance(g, dict) and g.get("item") == key for g in items):
                items.append({"at": store.now_iso(), "by": "owner", "item": key, "text": note[:4000], "scope": "delivery"})
    return _update(layout, task_id, fn)


def set_priority(layout: Layout, task_id: str, priority: int, actor: str = "owner") -> Task:
    def fn(doc):
        doc["priority"] = int(priority)
    t = _update(layout, task_id, fn)
    log(layout, "task_priority", actor, task=task_id, priority=priority)
    return t


# ------------------------------------------------------------------ delivery records and advance()

VOLATILE_PROJECTIONS = {"STATE.md", "TASKS.md", "ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md"}


# Authored records under state/: written by an agent or the owner as a deliverable and never rewritten by the loop,
# so they can be frozen in a delivery digest. Everything else under state/ is the store and changes every tick.
AUTHORED_STATE = ("state/decisions/",)


def volatile_state(rel: str) -> bool:
    """True for a store file under state/ — never a deliverable, never a frozen review input."""
    return rel.startswith("state/") and not rel.startswith(AUTHORED_STATE)


def record_delivery(layout: Layout, task_id: str, digest: str, actor: str = "system", result: str | None = None,
                    paths: Iterable[str] = ()) -> Task:
    """A new artifact digest: earlier verification/reviews/approvals are superseded (kept in history). A task that was
    waiting on review/approval of the old digest returns to IN_PROGRESS."""
    def fn(doc):
        d = doc.setdefault("delivery", {}) or {}
        doc["delivery"] = d
        if d.get("digest") and d.get("digest") != digest:
            for r in d.get("reviews") or []:
                r["superseded"] = True
            for a in d.get("approvals") or []:
                a["superseded"] = True
            d["verification"] = None
            hist = doc.get("history") or []
            blocked_by_review = (doc["status"] == TaskStatus.BLOCKED.value and hist
                                 and hist[-1].get("by") == "advance" and "BLOCKED" in str(hist[-1].get("note", "")))
            if doc["status"] in (TaskStatus.WAITING_FOR_REVIEW.value, TaskStatus.WAITING_FOR_HUMAN.value,
                                 TaskStatus.FAILED.value) or blocked_by_review:
                _record_history(doc, TaskStatus.IN_PROGRESS, actor, f"re-delivery {digest[:12]}")
        d["digest"] = digest
        if result is not None:
            doc["result"] = result
        if paths:
            # the delivery's own paths replace the previous delivery's (a review freezes on them; accumulated stale
            # entries — e.g. a state/ file listed once — kept invalidating reviews). state/ and projections never
            # count — the built-in views and every projection a plugin registered (verify.excluded_files), so
            # a rendered file is never frozen as a review input.
            doc["paths"] = sorted({str(x) for x in paths if not volatile_state(str(x))
                                   and str(x) not in volatile})
    from . import verify as verify_mod                   # local: verify imports this module
    volatile = VOLATILE_PROJECTIONS | verify_mod.excluded_files(layout)
    t = _update(layout, task_id, fn)
    log(layout, "delivery", actor, task=task_id, digest=digest)
    return t


def _require_digest(doc: dict, task_id: str, digest: str, what: str):
    d = doc.setdefault("delivery", {}) or {}
    doc["delivery"] = d
    if d.get("digest") != digest:
        raise TaskError(f"{task_id}: {what} digest {digest[:12]} does not match the current delivery")
    return d


def record_verification(layout: Layout, task_id: str, digest: str, ok: bool, report: str | None = None,
                        detail: str = "", actor: str = "verify") -> Task:
    def fn(doc):
        d = _require_digest(doc, task_id, digest, "verification")
        d["verification"] = VerificationRecord(digest=digest, ok=ok, at=store.now_iso(), report=report,
                                               detail=detail).model_dump()
    t = _update(layout, task_id, fn)
    log(layout, "verification", actor, task=task_id, ok=ok, digest=digest)
    return t


MAX_REPORT_BYTES = 4 * 1024 * 1024


def read_linkfree(layout: Layout, rel: str, limit: int) -> bytes | None:
    """Up to `limit` bytes of the project file `rel`, or None when it is missing, larger, outside the root or reached
    through a link (checked before it is opened). One read: a caller that hashes the result hashes what it uses."""
    from ._vendor.contract import contained, resolve as resolve_linkfree
    try:
        path = resolve_linkfree(layout.root / str(rel))
        if not contained(path, [layout.root], root=layout.root) or not path.is_file():
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except (OSError, ValueError):
        return None
    return data if len(data) <= limit else None


def report_file_sha256(layout: Layout, rel: str | None) -> str | None:
    data = read_linkfree(layout, rel, MAX_REPORT_BYTES) if rel else None
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_verified(layout: Layout, rel: str, sha256: str | None, limit: int) -> bytes | None:
    """The bytes of `rel`, read once through a link-free path, only when they hash to `sha256`."""
    data = read_linkfree(layout, rel, limit) if sha256 else None
    return data if data is not None and hashlib.sha256(data).hexdigest() == sha256 else None


def review_report(layout: Layout, review: ReviewRecord) -> tuple[dict | None, str]:
    """A recorded review's own report, exactly as recorded: (review object, state). `state` is "ok", "none" (the
    review names no report), "unverified" (no hash was recorded: a review from before 1.10.0) or "changed" (the file
    no longer hashes to `report_sha256`, is gone, is a link or does not parse). Never the file's current content
    otherwise. The one reader for everything that shows or quotes a reviewer's rationale (the approval view,
    escalation). A lane's envelope around the review object (`{"status", ..., "result": {...}}`) is unwrapped."""
    if not review.report:
        return None, "none"
    if not review.report_sha256:
        return None, "unverified"
    data = read_verified(layout, review.report, review.report_sha256, MAX_REPORT_BYTES)
    if data is None:
        return None, "changed"
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "changed"
    if isinstance(doc, dict) and "verdict" not in doc and isinstance(doc.get("result"), dict):
        doc = doc["result"]
    return (doc, "ok") if isinstance(doc, dict) else (None, "changed")


def review_snapshot(layout: Layout, run_dir, review: dict) -> tuple[str, str]:
    """Freeze the structured review a job returned (the object its verdict was read from) as `review_report.json` in
    its run dir: the bytes are serialized once, written, and hashed in memory, so the recorded verdict and the
    report that speaks for it come from the same input, whatever happens to the file afterwards. Returns
    (root-relative path, sha256)."""
    from ._vendor.contract import atomic_write_text
    # Compact ASCII JSON: every newline and non-ASCII character is escaped, so text-mode writing (newline translation
    # on Windows) cannot make the bytes on disk differ from the bytes hashed here.
    text = json.dumps(review, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    path = Path(run_dir) / "review_report.json"
    atomic_write_text(path, text)
    return path.relative_to(layout.root).as_posix(), hashlib.sha256(text.encode("ascii")).hexdigest()


HASH_AT_RECORD = object()        # record_review(report_sha256=...) default: no provenance supplied, hash the file now


def record_review(layout: Layout, task_id: str, digest: str, role: str, verdict: str, vendor: str | None = None,
                  report: str | None = None, actor: str = "review", report_sha256=HASH_AT_RECORD) -> Task:
    """`report_sha256`: the hash of the report as the reviewer produced it, when the caller holds those bytes (the
    loop's snapshot of the job's result, a CLI review hashed at submission). An explicit None means the caller had no
    verifiable bytes: it is kept as None (the report is never shown as the reviewer's) and the file is NOT hashed
    later in its place. Omitted, the file is hashed now, at record time — which says what the file held then, not
    what the reviewer wrote."""
    verdict_e = ReviewVerdict(verdict)              # strict: ACCEPT/REVISE/REJECT/BLOCKED only
    sha = report_file_sha256(layout, report) if report_sha256 is HASH_AT_RECORD else report_sha256

    def fn(doc):
        d = _require_digest(doc, task_id, digest, "review")
        d.setdefault("reviews", []).append(ReviewRecord(digest=digest, role=role, verdict=verdict_e, at=store.now_iso(),
                                                        vendor=vendor, report=report,
                                                        report_sha256=sha).model_dump(mode="json"))
    t = _update(layout, task_id, fn)
    log(layout, "review", actor, task=task_id, role=role, verdict=verdict_e.value, digest=digest)
    return t


def record_approval(layout: Layout, task_id: str, digest: str, response: str, by: str = "owner", note: str = "",
                    command_id: str | None = None) -> Task:
    response_e = ApprovalResponse(response)

    def fn(doc):
        d = _require_digest(doc, task_id, digest, "approval")
        d.setdefault("approvals", []).append(ApprovalRecord(digest=digest, by=by, response=response_e, at=store.now_iso(),
                                                            note=note, command_id=command_id).model_dump(mode="json"))
    t = _update(layout, task_id, fn)
    log(layout, "approval", by, task=task_id, response=response_e.value, digest=digest)
    return t


def advance(layout: Layout, task_id: str, actor: str = "advance", attempt_id: str | None = None) -> Task:
    """The only path to COMPLETE. Decides from the locked, freshly read document. `attempt_id`, when the caller
    knows which delivery attempt produced the digest now completing (loop.record_outcome does), is recorded on
    any issue this task's completion recovers (R-21, escalation.recover_issues_for_task)."""
    outcome = {}

    def fn(doc):
        task = Task.model_validate(doc)
        if task.status not in ADVANCEABLE:
            raise _NoChange()
        target, note = _evaluate(task)
        if target == task.status:
            raise _NoChange()
        outcome.update({"from": task.status.value, "to": target.value, "note": note})
        _record_history(doc, target, actor, note)
    try:
        t = _update(layout, task_id, fn)
    except _NoChange:
        return load(layout, task_id)
    log(layout, "task_status", actor, task=task_id, **outcome)
    if outcome.get("to") == TaskStatus.COMPLETE.value:
        from . import escalation as escalation_mod
        escalation_mod.recover_issues_for_task(layout, task_id, attempt_id, t.delivery.digest or "")
    return t


def reconcile_advance(layout: Layout, tasks: dict[str, Task] | None = None,
                       open_job_task_ids: Iterable[str] | None = None) -> list[str]:
    """Advance every task whose current records already satisfy the completion rule but whose status has not
    caught up (R-08): a crash, or any other missed call to advance() after a durable evidence write, leaves exactly
    this gap, and closing it must not depend on a replay ever arriving. Checked here from the in-memory snapshot
    (`_evaluate` is pure, no disk read) so a normal turn with nothing stuck pays for one pass over already-loaded
    tasks, not a second lock and read per task; `advance()` itself re-reads and re-decides, only for the few that
    actually need it. Returns the ids it advanced.

    Skips any task with an attempt still open (PREPARED/SUBMITTED/RUNNING, named by `open_job_task_ids`): its
    current digest's verification record belongs to the attempt that open job is replacing, so `_evaluate()` would
    read FAILED back onto a task that is, in fact, mid-revision or mid-retry (P1-1) -- nothing in `ADVANCEABLE`
    should move backwards onto evidence a running attempt has already superseded. A caller mid-turn (`loop.once()`)
    passes the turn's own already-scanned open-job ids; anyone else gets one fresh scan."""
    tasks = tasks if tasks is not None else load_all(layout)
    if open_job_task_ids is None:
        from . import jobs as jobs_mod
        open_job_task_ids = {r["task_id"] for r in jobs_mod.open_records(layout)}
    else:
        open_job_task_ids = set(open_job_task_ids)
    out = []
    for t in tasks.values():
        if t.status not in ADVANCEABLE or t.id in open_job_task_ids:
            continue
        target, _note = _evaluate(t)
        if target != t.status:
            advance(layout, t.id)
            out.append(t.id)
    return out
