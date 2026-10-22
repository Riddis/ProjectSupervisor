"""Owner inbox store (state/inbox/*.yaml): items a human must answer before a task can proceed.
Two rules the loop depends on: no more than `MAX_OPEN_PER_ROLE` open items per role at once (the owner's attention is
the scarce resource), and a human gate never times out — items wait until answered, however long that takes.
Answers accept the structured keywords (ACCEPT/REJECT/MODIFY/REDO, optionally followed by an
argument, e.g. `MODIFY shorter summary`) or, unless an item disables it, free text — the owner's own words count as a
valid answer, not only the keywords. `HUMAN_INBOX.md` is rendered from this store (render.render_inbox), never parsed."""
from __future__ import annotations

import re
from pathlib import Path

from . import store, tasks as tasks_mod
from .locks import FileLock
from .paths import Layout

MAX_OPEN_PER_ROLE = 3
MAX_SUMMARY_CHARS = 600
DEFAULT_RESPONSES = ["ACCEPT", "REJECT", "MODIFY", "REDO"]

# Owner approval of a delivery (tasks with human_approval=required). The loop opens one `approval` item per
# (task, delivery digest) once verification and every required review have passed; answering it is what writes the
# digest-bound ApprovalRecord that tasks._evaluate() demands.
APPROVAL_KIND = "approval"
APPROVAL_RESPONSES = ["APPROVE", "REJECT", "MODIFY"]
APPROVAL_ALIASES = {"APPROVE": "ACCEPT", "ACCEPT": "ACCEPT", "YES": "ACCEPT", "OK": "ACCEPT", "LGTM": "ACCEPT",
                    "REJECT": "REJECT", "NO": "REJECT", "MODIFY": "MODIFY", "REDO": "REDO"}


class InboxError(ValueError):
    pass


class InboxFull(InboxError):
    """Raised by create() instead of writing a 4th open item for a role. The human adapter turns this into a
    capacity deferral (no attempt consumed, no failure) rather than a task failure."""

    def __init__(self, role: str):
        super().__init__(f"inbox full for role {role!r} ({MAX_OPEN_PER_ROLE} open items already)")
        self.role = role


def path_for(layout: Layout, item_id: str) -> Path:
    return layout.inbox / f"{item_id}.yaml"


def load(layout: Layout, item_id: str) -> dict:
    p = path_for(layout, item_id)
    if not p.exists():
        raise InboxError(f"no such inbox item {item_id}")
    return store.read(p)


def all_items(layout: Layout) -> list[dict]:
    return [store.read(p) for p in sorted(layout.inbox.glob("*.yaml"))]


def open_items(layout: Layout, for_role: str | None = None) -> list[dict]:
    items = [i for i in all_items(layout) if i.get("status") == "open"]
    return [i for i in items if for_role is None or i.get("for") == for_role]


def find_by_attempt(layout: Layout, attempt_id: str) -> dict | None:
    """The item (if any) already written for this dispatch attempt — lets an adapter's recover() find work a
    previous, crashed loop already launched instead of creating a duplicate."""
    for i in all_items(layout):
        if i.get("attempt_id") == attempt_id:
            return i
    return None


def _next_id(layout: Layout) -> str:
    n = 0
    for p in layout.inbox.glob("H-*.yaml"):
        m = re.match(r"H-(\d+)$", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    return f"H-{n + 1:03d}"


def create(layout: Layout, *, for_role: str, subject: str, summary: str, kind: str = "question",
          allowed_responses: list[str] | None = None, free_text: bool = True, from_: str = "system",
          task_id: str | None = None, attempt_id: str | None = None, approval_digest: str | None = None) -> dict:
    """Write a new open item. Enforces the per-role cap and the prose budget; the cap check and id allocation happen
    under one lock so concurrent creates never collide or overshoot."""
    if len(summary) > MAX_SUMMARY_CHARS:
        raise InboxError(f"summary is {len(summary)} characters; limit is {MAX_SUMMARY_CHARS}")
    with FileLock(layout.state / ".inbox.lock", owner="inbox", timeout=5.0):
        if len(open_items(layout, for_role)) >= MAX_OPEN_PER_ROLE:
            raise InboxFull(for_role)
        item_id = _next_id(layout)
        doc = {"id": item_id, "kind": kind, "for": for_role, "from": from_, "status": "open", "subject": subject,
               "summary": summary, "allowed_responses": list(allowed_responses or DEFAULT_RESPONSES),
               "free_text": free_text, "task_id": task_id, "attempt_id": attempt_id}
        if approval_digest:
            doc["approval_digest"] = approval_digest
        store.write_new(path_for(layout, item_id), doc)
    tasks_mod.log(layout, "inbox_open", from_, item=item_id, for_role=for_role, task=task_id)
    return load(layout, item_id)


def answer(layout: Layout, item_id: str, response: str, note: str = "", by: str = "owner",
          command_id: str | None = None) -> dict:
    """Validate and record an answer; idempotent by command_id (a retried command must not raise "already answered")."""
    response = (response or "").strip()
    if not response:
        raise InboxError(f"{item_id}: empty response")
    if not path_for(layout, item_id).exists():
        raise InboxError(f"no such inbox item {item_id}")
    outcome: dict = {}

    def fn(doc):
        if doc.get("status") != "open":
            if command_id and (doc.get("answer") or {}).get("command_id") == command_id:
                outcome["skipped"] = True
                return doc
            raise InboxError(f"{item_id}: already {doc.get('status')}")
        allowed = doc.get("allowed_responses") or DEFAULT_RESPONSES
        token = response.split(None, 1)[0].upper()
        structured = token in {a.upper() for a in allowed}
        if not structured and not doc.get("free_text", True):
            raise InboxError(f"{item_id}: {response!r} is not one of {allowed}")
        doc["status"] = "answered"
        doc["answered_at"] = store.now_iso()
        doc["answer"] = {"response": response, "kind": token if structured else "text", "note": note, "by": by,
                         "at": store.now_iso(), "command_id": command_id}
        outcome["kind"] = token if structured else "text"
        return doc

    doc = store.update(path_for(layout, item_id), fn, lock_path=layout.state / ".inbox.lock")
    if not outcome.get("skipped"):
        tasks_mod.log(layout, "inbox_answer", by, item=item_id, response=outcome.get("kind"), task=doc.get("task_id"))
        apply_approval(layout, doc, by=by, command_id=command_id)
    return doc


def approval_target(layout: Layout, item: dict) -> str | None:
    """The delivery digest an answered item approves, or None when the item is not an approval.

    Two shapes qualify: an `approval` item the loop opened (carries `approval_digest`), and an agent-written item
    for a task that is WAITING_FOR_HUMAN on its current digest with no attempt behind it (attempt_id None — items
    with an attempt belong to the human adapter, whose collect() resumes the task itself). The second shape covers an
    agent that asked the owner the question the loop would otherwise have asked."""
    task_id = item.get("task_id")
    if not task_id or item.get("attempt_id"):
        return None
    if item.get("approval_digest"):
        return str(item["approval_digest"])
    try:
        task = tasks_mod.load(layout, task_id)
    except tasks_mod.TaskError:
        return None
    from .models.task import HumanApproval, TaskStatus
    if task.status != TaskStatus.WAITING_FOR_HUMAN or task.human_approval == HumanApproval.none:
        return None
    return task.delivery.digest or None


def apply_approval(layout: Layout, item: dict, by: str = "owner", command_id: str | None = None) -> dict | None:
    """Turn an answered approval item into a digest-bound ApprovalRecord and re-run tasks.advance(). Idempotent:
    an approval already recorded for this item (by == item id) is not written twice. Returns the task's new status
    as {"status": ...} or None when the item is not an approval / was already applied / is stale."""
    if item.get("status") != "answered":
        return None
    digest = approval_target(layout, item)
    if not digest:
        return None
    answer = item.get("answer") or {}
    token = str(answer.get("response") or "").split(None, 1)[0].upper()
    response = APPROVAL_ALIASES.get(token)
    if response is None:
        if item.get("kind") != APPROVAL_KIND:
            return None                                   # free text on a plain question: not an approval verdict
        response = "MODIFY"                               # free text on an approval item = "change it", never a silent ACCEPT
    task_id = item["task_id"]
    note = (answer.get("note") or "").strip() or (answer.get("response") if response == "MODIFY" else "")
    try:
        task = tasks_mod.load(layout, task_id)
        if any(a.by == item["id"] and a.digest == digest for a in task.delivery.approvals):
            return {"status": task.status.value, "skipped": "already applied"}
        tasks_mod.record_approval(layout, task_id, digest, response, by=item["id"], note=note or "",
                                  command_id=command_id or answer.get("command_id"))
        task = tasks_mod.advance(layout, task_id, actor="advance")
    except tasks_mod.TaskError as exc:                   # digest superseded by a newer delivery, or no such task
        tasks_mod.log(layout, "approval_stale", by, item=item["id"], task=task_id, digest=digest, error=str(exc)[:200])
        return None
    return {"status": task.status.value}


def snooze(layout: Layout, item_id: str, by: str = "owner", note: str = "") -> dict:
    """Hide an open item from the owner's default view without answering it (Control Center inbox).
    The underlying task, if any, stays WAITING_FOR_HUMAN — snoozing defers attention, it is not an answer."""
    def fn(doc):
        if doc.get("status") != "open":
            raise InboxError(f"{item_id}: cannot snooze a {doc.get('status')} item")
        doc["status"] = "snoozed"
        doc["snoozed_at"] = store.now_iso()
        doc["snoozed_by"] = by
        if note:
            doc["snooze_note"] = note
        return doc

    doc = store.update(path_for(layout, item_id), fn, lock_path=layout.state / ".inbox.lock")
    tasks_mod.log(layout, "inbox_snooze", by, item=item_id, task=doc.get("task_id"))
    return doc


def reopen(layout: Layout, item_id: str, by: str = "owner") -> dict:
    """Put a snoozed item back in the default view (the inverse of snooze(); answered items are terminal)."""
    def fn(doc):
        if doc.get("status") != "snoozed":
            raise InboxError(f"{item_id}: cannot reopen a {doc.get('status')} item")
        doc["status"] = "open"
        return doc

    doc = store.update(path_for(layout, item_id), fn, lock_path=layout.state / ".inbox.lock")
    tasks_mod.log(layout, "inbox_reopen", by, item=item_id, task=doc.get("task_id"))
    return doc
