"""Task store: load/validate/import, eligibility, the single transition function `advance()`, manual transitions.

Rules:
- every status change happens here, inside the store lock, deciding from the freshly read document (no stale reads);
- `_evaluate()` is the one rule: verification ∧ every required review ACCEPT on the current digest ∧ (no approval
  needed ∨ owner ACCEPT for this digest) → COMPLETE. `advance()` applies it; `completion_check()` reports it;
- a new delivery digest supersedes older verification/review/approval records;
- `set_status()` never reaches COMPLETE except when the owner forces it with a note (persisted, rendered, logged).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable

import yaml
from pydantic import ValidationError

from . import gates as gates_mod
from . import policy as policy_mod
from . import store
from .models.task import (ALLOWED, ApprovalRecord, ApprovalResponse, Attempt, HumanApproval, ReviewRecord,
                          ReviewVerdict, Task, TaskStatus, VerificationRecord)
from .paths import Layout

ADVANCEABLE = {TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_REVIEW, TaskStatus.WAITING_FOR_HUMAN, TaskStatus.FAILED}
CONTRACT_FIELDS = ["phase", "title", "objective", "owner_role", "depends_on", "actions", "artifacts", "tests",
                   "checks", "owned_paths", "reviewer", "review_note", "acceptance", "escalation", "human_approval",
                   "approval_note", "cost_category"]


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
        return Task.model_validate(store.read(p))
    except ValidationError as exc:
        raise TaskError(f"{task_id}: {exc}") from exc


def load_all(layout: Layout) -> dict[str, Task]:
    out = {}
    for p in sorted(layout.tasks.glob("*.yaml")):
        t = Task.model_validate(store.read(p))
        out[t.id] = t
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
    ("cycle_N/", or with `root` an existing directory) and characters no file name can hold ("<id>").

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
            elif _ILLEGAL.search(a):
                why = "has a placeholder or a character no file name can hold (<...>, \", |) — use a glob such as runs/*/summary.json"
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


def artifact_warnings(tasks: Iterable[Task], root: Path | None = None) -> list[str]:
    """Artifact entries that look like prose or a placeholder but are legal file names (artifact_problems warnings)."""
    return artifact_problems(tasks, root)[1]


def eligible(layout: Layout, tasks: dict[str, Task] | None = None) -> list[Task]:
    tasks = tasks or load_all(layout)
    gate_data = gates_mod.load(layout)
    done = {t.id for t in tasks.values() if t.status == TaskStatus.COMPLETE}
    out = [t for t in tasks.values()
           if t.status in (TaskStatus.NOT_STARTED, TaskStatus.READY)
           and all(d in done for d in t.depends_on)
           and gates_mod.phase_open(layout, t.phase, gate_data)]
    return sorted(out, key=lambda t: (-t.priority, t.phase, t.id))


def create(layout: Layout, doc: dict, actor: str = "system") -> dict:
    """Add a task that is not in the seed (probes, owner-added work, Control Center conversions). Validated like
    the seed; an existing id is a no-op, so a retried command (or a second click) is never a duplicate."""
    doc = dict(doc)
    path = path_for(layout, doc["id"])
    if path.exists():
        return {"skipped": "exists", "id": doc["id"]}
    doc.setdefault("status", "NOT_STARTED")
    doc.update({"attempts": [], "result": None, "paths": [], "decisions": []})
    Task.model_validate(doc)                              # fail before touching the graph or the disk
    combined = load_all(layout)
    combined[doc["id"]] = Task.model_validate(doc)
    art_errors, warnings = artifact_problems([combined[doc["id"]]], root=layout.root)
    errors = validate_graph(combined) + ownership_errors([combined[doc["id"]]], layout) + art_errors
    if errors:
        raise TaskError("; ".join(errors))
    store.write_new(path, doc)
    log(layout, "task_created", actor, task=doc["id"], **({"warnings": warnings} if warnings else {}))
    out = {"id": doc["id"], "status": doc["status"]}
    if warnings:
        out["warnings"] = warnings
    return out


def import_seed(layout: Layout, seed_path: Path, update_ids: Iterable[str] = (), create: bool = True,
                actor: str = "cli") -> dict:
    """Create tasks that do not exist yet (unless `create` is False); refresh only the named tasks' contract fields.
    Idempotent. Runs in-process for the CLI when no supervisor is live, and as the `task.import` command otherwise."""
    seed = yaml.safe_load(Path(seed_path).read_text(encoding="utf-8"))["tasks"]
    created, updated, untouched = [], [], []
    update_ids = set(update_ids)
    combined = load_all(layout)
    existing = set(combined)
    for t in seed:
        combined[t["id"]] = Task.model_validate(t)   # seed must be a valid contract
    art_errors, warnings = artifact_problems([combined[t["id"]] for t in seed], root=layout.root)
    errors = (validate_graph(combined) + ownership_errors([combined[t["id"]] for t in seed if t["id"] not in existing], layout)
              + art_errors)
    if errors:
        raise TaskError("seed invalid: " + "; ".join(errors))   # nothing written
    for t in seed:
        path = path_for(layout, t["id"])
        if not path.exists():
            if not create:
                continue
            doc = dict(t)
            doc.setdefault("status", TaskStatus.NOT_STARTED.value)      # the store keeps the runtime fields explicit
            doc.update({"attempts": [], "result": None, "paths": [], "decisions": []})
            store.write_new(path, doc)
            created.append(t["id"])
        elif t["id"] in update_ids:
            def fn(doc, t=t):
                for k in CONTRACT_FIELDS:
                    if k in t:
                        doc[k] = t[k]
                    elif k not in ("owned_paths", "checks"):   # ownership/checks set in the store outlive a seed that lacks them
                        doc.pop(k, None)
                Task.model_validate(doc)
                return doc
            store.update(path, fn, lock_path=layout.state / ".tasks.lock")
            updated.append(t["id"])
        else:
            untouched.append(t["id"])
    log(layout, "task_import", actor, created=created, updated=updated, **({"warnings": warnings} if warnings else {}))
    out = {"created": created, "updated": updated, "untouched": untouched}
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

def log(layout: Layout, event: str, actor: str = "system", **fields):
    """The single append path to state/log.jsonl. Every field is redacted here: the log is read by the
    Control Center, quoted into revision packets and committed in projections, so a credential that reaches it
    has effectively left `secrets/`. Redacted, not refused — losing an audit record would be the worse failure."""
    layout.log.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": store.now_iso(), "actor": actor, "event": event, **policy_mod.redact_fields(fields, layout)}
    with layout.log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    return Task.model_validate(doc)


def _record_history(doc: dict, to: TaskStatus, by: str, note: str, forced: bool = False):
    entry = {"at": store.now_iso(), "by": by, "from": doc.get("status"), "to": to.value, "note": note}
    if forced:
        entry["forced"] = True
    doc.setdefault("history", []).append(entry)
    doc["status"] = to.value


class _NoChange(Exception):
    pass


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
        if force:
            doc["forced"] = {"by": actor, "at": store.now_iso(), "to": status.value, "note": note}
    try:
        t = _update(layout, task_id, fn)
    except _NoChange:
        return load(layout, task_id)
    log(layout, "task_status", actor, task=task_id, **{"from": before.get("status"), "to": status.value}, note=note, forced=force)
    return t


def add_attempt(layout: Layout, task_id: str, resource: str, note: str = "", **extra) -> Task:
    """A dispatch attempt (resource/model/session/run_dir) — used by the loop and retry budgets."""
    def fn(doc):
        n = len(doc.get("attempts", [])) + 1
        doc.setdefault("attempts", []).append(Attempt(id=f"{task_id}.a{n}", resource=resource, at=store.now_iso(),
                                                      note=note, **extra).model_dump(exclude_none=True))
    return _update(layout, task_id, fn)


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
            # entries — e.g. a state/ file listed once — kept invalidating reviews). state/ and projections never count.
            doc["paths"] = sorted({str(x) for x in paths if not str(x).startswith("state/")
                                   and str(x) not in VOLATILE_PROJECTIONS})
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


def record_review(layout: Layout, task_id: str, digest: str, role: str, verdict: str, vendor: str | None = None,
                  report: str | None = None, actor: str = "review") -> Task:
    verdict_e = ReviewVerdict(verdict)              # strict: ACCEPT/REVISE/REJECT/BLOCKED only

    def fn(doc):
        d = _require_digest(doc, task_id, digest, "review")
        d.setdefault("reviews", []).append(ReviewRecord(digest=digest, role=role, verdict=verdict_e, at=store.now_iso(),
                                                        vendor=vendor, report=report).model_dump(mode="json"))
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


def advance(layout: Layout, task_id: str, actor: str = "advance") -> Task:
    """The only path to COMPLETE. Decides from the locked, freshly read document."""
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
    return t
