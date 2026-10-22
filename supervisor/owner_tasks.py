"""Owner tasks store (state/owner_tasks.yaml): things only the owner can do.
status: open | done | snoozed | deferred(trigger) — rendered to OWNER_TASKS.md by render.render_owner_tasks."""
from __future__ import annotations

from . import store
from .paths import Layout


class OwnerTaskError(ValueError):
    pass


def load(layout: Layout) -> list[dict]:
    if not layout.owner_tasks.exists():
        return []
    return store.read(layout.owner_tasks).get("owner_tasks") or []


def _next_id(layout: Layout) -> str:
    import re
    n = 0
    for row in load(layout):
        m = re.match(r"OT-(\d+)$", str(row.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"OT-{n + 1:03d}"


def create(layout: Layout, title: str, guidance: str, priority: str = "normal", actor: str = "owner") -> dict:
    """Add a row that is not in the seed (Control Center conversions: ). `priority` and `blocks` match
    the seeded rows' shape so the Owner tasks panel renders it the same way."""
    row = {}
    if not layout.owner_tasks.exists():
        store.write_new(layout.owner_tasks, {"owner_tasks": []})

    def fn(doc):
        rows = doc.setdefault("owner_tasks", [])
        row.update({"id": _next_id(layout), "title": title, "guidance": guidance, "status": "open",
                   "blocks": [], "priority": priority})
        rows.append(dict(row))
        return doc

    store.update(layout.owner_tasks, fn, lock_path=layout.state / ".owner_tasks.lock")
    from . import tasks as tasks_mod
    tasks_mod.log(layout, "owner_task_created", actor, owner_task=row["id"])
    return row


def set_status(layout: Layout, task_id: str, status: str, actor: str = "owner") -> dict:
    """Change an owner task's status. Marking it `done` also releases the project tasks in its `blocks` list: each one
    that is FAILED or BLOCKED and no longer waits on any other open owner task goes back to READY, so the owner's
    "done" click is the re-trigger. The released ids are returned under `released`."""
    found = {}
    all_rows: list[dict] = []

    def fn(doc):
        rows = doc.setdefault("owner_tasks", [])
        for row in rows:
            if row.get("id") == task_id:
                row["status"] = status
                found.update(row)
                all_rows[:] = [dict(r) for r in rows]
                return doc
        raise OwnerTaskError(f"no such owner task {task_id}")

    store.update(layout.owner_tasks, fn, lock_path=layout.state / ".owner_tasks.lock")
    from . import tasks as tasks_mod
    tasks_mod.log(layout, "owner_task_status", actor, owner_task=task_id, status=status)
    if status == "done":
        found["released"] = release_blocked(layout, all_rows, found, actor)
    return found


RELEASABLE = ("FAILED", "BLOCKED")


def release_blocked(layout: Layout, rows: list[dict], done_row: dict, actor: str = "owner") -> list[str]:
    """Re-queue the FAILED/BLOCKED project tasks that `done_row` blocked, unless another owner task that is not done
    still blocks them. Tasks in any other status are left alone: a READY/IN_PROGRESS task needs no release and a
    COMPLETE/CANCELLED one must not be reopened by an owner-task click."""
    from . import tasks as tasks_mod
    from .models.task import TaskStatus
    released: list[str] = []
    for tid in done_row.get("blocks") or []:
        still = [r["id"] for r in rows
                 if r.get("id") != done_row.get("id") and tid in (r.get("blocks") or []) and r.get("status") != "done"]
        if still:
            tasks_mod.log(layout, "owner_task_release_skipped", actor, owner_task=done_row.get("id"), task=tid,
                          still_blocked_by=still)
            continue
        try:
            task = tasks_mod.load(layout, tid)
        except tasks_mod.TaskError:
            continue
        if task.status.value not in RELEASABLE:
            continue
        try:
            tasks_mod.set_status(layout, tid, TaskStatus.READY, actor=actor,
                                 note=f"owner task {done_row.get('id')} done — re-queued")
        except tasks_mod.TaskError as exc:
            tasks_mod.log(layout, "owner_task_release_failed", actor, owner_task=done_row.get("id"), task=tid,
                          error=str(exc)[:200])
            continue
        released.append(tid)
    return released
