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
# The summary is the card's headline and stays short; anything longer the writer had to say goes in `detail`, which
# the Control Center shows behind "Show full message". A hard cut at MAX_SUMMARY_CHARS used to drop whatever came
# last — for an escalation that was the question and the options.
MAX_DETAIL_CHARS = 8000
DEFAULT_RESPONSES = ["ACCEPT", "REJECT", "MODIFY", "REDO"]

# Owner approval of a delivery (tasks with human_approval=required). The loop opens one `approval` item per
# (task, delivery digest) once verification and every required review have passed; answering it is what writes the
# digest-bound ApprovalRecord that tasks._evaluate() demands.
# Kinds the framework itself creates or renders with a fixed meaning; a plugin claims others (registry.inbox_kind).
BUILTIN_KINDS = ("gate", "decision", "approval", "spend", "policy", "question")
APPROVAL_KIND = "approval"
APPROVAL_RESPONSES = ["APPROVE", "REJECT", "MODIFY"]
APPROVAL_ALIASES = {"APPROVE": "ACCEPT", "ACCEPT": "ACCEPT", "YES": "ACCEPT", "OK": "ACCEPT", "LGTM": "ACCEPT",
                    "REJECT": "REJECT", "NO": "REJECT", "MODIFY": "MODIFY", "REDO": "REDO"}


def fit(text: str) -> tuple[str, str | None]:
    """Split prose for an item into (summary, detail): text within the summary budget is the summary alone; longer
    text becomes a summary cut at a word boundary plus the full text (up to MAX_DETAIL_CHARS) as detail, so nothing
    the writer said is lost. Callers put what the owner must act on first — that is the part the summary keeps."""
    text = str(text or "").strip()
    if len(text) <= MAX_SUMMARY_CHARS:
        return text, None
    cut = text[:MAX_SUMMARY_CHARS - 1]
    head = cut.rsplit(" ", 1)[0] if " " in cut[MAX_SUMMARY_CHARS // 2:] else cut
    return head.rstrip(" ,;:") + "…", text[:MAX_DETAIL_CHARS]


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
          task_id: str | None = None, attempt_id: str | None = None, approval_digest: str | None = None,
          extra: dict | None = None, detail: str | None = None) -> dict:
    """Write a new open item. Enforces the per-role cap and the prose budget (summary ≤ MAX_SUMMARY_CHARS; the
    optional `detail` — the full text, see fit() — ≤ MAX_DETAIL_CHARS); the cap check and id allocation happen
    under one lock so concurrent creates never collide or overshoot. `extra` adds fields without overriding any."""
    if len(summary) > MAX_SUMMARY_CHARS:
        raise InboxError(f"summary is {len(summary)} characters; limit is {MAX_SUMMARY_CHARS}")
    if detail is not None and len(detail) > MAX_DETAIL_CHARS:
        raise InboxError(f"detail is {len(detail)} characters; limit is {MAX_DETAIL_CHARS}")
    with FileLock(layout.state / ".inbox.lock", owner="inbox", timeout=5.0):
        if len(open_items(layout, for_role)) >= MAX_OPEN_PER_ROLE:
            raise InboxFull(for_role)
        item_id = _next_id(layout)
        doc = {"id": item_id, "kind": kind, "for": for_role, "from": from_, "status": "open", "subject": subject,
               "summary": summary, "allowed_responses": list(allowed_responses or DEFAULT_RESPONSES),
               "free_text": free_text, "task_id": task_id, "attempt_id": attempt_id}
        if approval_digest:
            doc["approval_digest"] = approval_digest
        if detail and detail.strip() != summary.strip():
            doc["detail"] = detail
        for k, v in (extra or {}).items():
            doc.setdefault(k, v)
        store.write_new(path_for(layout, item_id), doc)
    tasks_mod.log(layout, "inbox_open", from_, item=item_id, for_role=for_role, task=task_id)
    return load(layout, item_id)


def kind_info(layout: Layout | None, kind: str | None):
    """The plugin's InboxKind claiming `kind`, or None for a built-in or unclaimed kind."""
    if layout is None or not kind:
        return None
    from . import plugins
    return plugins.load(layout).inbox_kinds.get(str(kind))


def ownership_unknown(layout: Layout | None, kind: str | None) -> str | None:
    """Why the owner of `kind` cannot be established, or None. A kind that is neither built in nor claimed while a
    required plugin failed to load may well belong to that plugin — its `inbox_kind()` registration never ran —
    so the generic path must fail closed: answering the item would skip the per-option validation the plugin
    enforces, and rendering it would show rows the plugin keeps blind. Built-in kinds are unaffected (their
    recovery actions stay available)."""
    if layout is None or not kind or str(kind) in BUILTIN_KINDS:
        return None
    from . import plugins
    reg = plugins.load(layout)
    if str(kind) in reg.inbox_kinds or not reg.blocking:
        return None
    return (f"kind {kind!r} cannot be answered or shown in full: a required plugin failed to load "
            f"({'; '.join(reg.blocking)}) and may own this kind; fix the plugin (`supervisor doctor`)")


def owned_by_plugin(layout: Layout | None, doc: dict) -> str | None:
    """Why the generic answer path may not close this item, or None: a plugin claimed its kind with
    `answerable=False`, so only that plugin's own code (`answer(..., internal=True)`) finalises it — or the kind's
    owner cannot be established because a required plugin failed to load (`ownership_unknown`)."""
    unknown = ownership_unknown(layout, doc.get("kind"))
    if unknown:
        return f"{doc.get('id')}: {unknown}"
    info = kind_info(layout, doc.get("kind"))
    if info is None or info.answerable:
        return None
    page = info.page_for(str(doc.get("id") or ""))
    return (f"{doc.get('id')}: a {info.kind} item is finalised by its plugin"
            + (f" (open {page})" if page else "") + ", not answered directly")


def answer(layout: Layout, item_id: str, response: str, note: str = "", by: str = "owner",
          command_id: str | None = None, internal: bool = False) -> dict:
    """Validate and record an answer; idempotent by command_id (a retried command must not raise "already answered").

    `internal=True` is for the code of the plugin that owns the item's kind (registry.inbox_kind with
    `answerable=False`): it carries per-option verdicts only that plugin knows how to finalise, so the generic path
    — this function as called by the command handler and the Control Center form — refuses to close one out from
    under it."""
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
        if not internal:
            refusal = owned_by_plugin(layout, doc)
            if refusal:
                raise InboxError(refusal)
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
        apply_decision(layout, doc, by=by)
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


# ------------------------------------------------------------------ owner decisions on stopped tasks

DECISION_KIND = "decision"
DECISION_RESPONSES = ["REQUEUE", "CANCEL", "DEFER"]
OWNER_DECISION_MARK = "owner decision needed"


def is_decision(item: dict) -> bool:
    """A REQUEUE/CANCEL/DEFER item about one task: the loop's `decision` items (Supervisor.request_owner_decisions)
    and the owner route of a diagnosis (diagnosis.route_to_owner). Framework versions before 1.4.0 wrote the latter
    as kind `question`; those are recognised by origin and responses so an answer to one still takes effect."""
    if not item.get("task_id"):
        return False
    if item.get("kind") == DECISION_KIND:
        return True
    return (item.get("kind") == "question" and item.get("from") == "escalation"
            and set(item.get("allowed_responses") or []) == set(DECISION_RESPONSES))


def add_guidance(layout: Layout, task_id: str, item_id: str, text: str, resume: str = "task",
                 digest: str | None = None) -> None:
    """Owner guidance for the next attempt, in the task's `guidance` list (the field packets.py reads). A review
    resume scopes it to the review of `digest`; anything else to the next delivery. Idempotent per item."""
    entry = {"at": store.now_iso(), "by": "owner", "item": item_id, "text": text[:4000],
             "scope": "review" if resume == "review" else "delivery"}
    if digest:
        entry["digest"] = digest

    def fn(doc):
        items = doc.setdefault("guidance", [])
        if not any(g.get("item") == item_id for g in items if isinstance(g, dict)):
            items.append(entry)
    tasks_mod._update(layout, task_id, fn)


def apply_decision(layout: Layout, item: dict, by: str = "owner") -> dict | None:
    """Act on an answered decision item (is_decision) for a task that is BLOCKED ("owner decision needed") or FAILED
    (a diagnosis handed it to the owner). REQUEUE resets the budget that stopped the task and puts it back where it
    stopped (`resume`: review | delivery | task), CANCEL cancels it, DEFER / free text only annotate it. REQUEUE also
    resets the task's escalation budget (spent revisions, failure signatures). Text after the keyword ("REQUEUE use
    the cached outputs") becomes owner guidance that the next delivery brief (or, for a review resume, the next
    review brief) carries, plus a task note for the record. Idempotent: the outcome is written to the item as
    `applied` and an applied item is never acted on twice."""
    if item.get("status") != "answered" or not is_decision(item) or item.get("applied"):
        return None
    from . import jobs as jobs_mod
    from .models.task import TaskStatus
    task_id = item.get("task_id")
    answer = item.get("answer") or {}
    token = str(answer.get("response") or "").split(None, 1)[0].upper()
    # the Control Center sends a button plus typed text as one response ("REQUEUE <text>"): the words after the
    # keyword are the guidance
    raw = str(answer.get("response") or "").strip()
    arg = raw.split(None, 1)[1].strip() if answer.get("kind") != "text" and len(raw.split(None, 1)) > 1 else ""
    note = (answer.get("note") or "").strip() or arg or (raw if answer.get("kind") == "text" else "")
    out: dict = {"response": token if token in DECISION_RESPONSES else "text"}
    try:
        task = tasks_mod.load(layout, task_id)
        if task.status not in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            out["skipped"] = f"task is {task.status.value}"
        elif token == "CANCEL":
            tasks_mod.set_status(layout, task_id, TaskStatus.CANCELLED, actor="owner", note=f"{item['id']}: cancelled by owner")
        elif token == "REQUEUE":
            resume = item.get("resume") or "task"
            if note:
                # the note is the audit trail; `guidance` is what the next packet actually carries (packets.py):
                # the delivery brief for a task/delivery resume, the review brief of this digest for a review resume
                add_guidance(layout, task_id, item["id"], note, resume=resume,
                             digest=task.delivery.digest if resume == "review" else None)
                tasks_mod.add_note(layout, task_id, f"{item['id']} owner guidance: {note}", actor="owner")
            # REQUEUE grants a fresh start: the spent revisions and failure signatures go too, or the next failure
            # escalates at once instead of getting the normal revision budget. Keyed by the item, so a re-apply
            # after a crash between this write and `applied` never grants a second reset.
            from . import escalation as escalation_mod
            escalation_mod.reset_retries(layout, task_id, reason=f"owner REQUEUE ({item['id']})", key=item["id"])
            if resume == "review":
                digest = task.delivery.digest
                for r in jobs_mod.for_task(layout, task_id):
                    if ((r.get("spec") or {}).get("kind") == "review" and r.get("review_digest") == digest
                            and any(r.get(k) for k in ("failed_review", "blocked_review", "invalidated"))):
                        jobs_mod.set_phase(layout, r["attempt_id"], r["phase"], failed_review=False, blocked_review=False,
                                           invalidated=False, budget_reset_by=item["id"])
                tasks_mod.set_status(layout, task_id, TaskStatus.WAITING_FOR_REVIEW, actor="owner", force=True,
                                     note=f"{item['id']}: owner re-queued the review (review budget reset)")
            else:
                if resume == "delivery":
                    for r in jobs_mod.for_task(layout, task_id):
                        reason = str(r.get("deferred_reason", ""))
                        if reason.startswith("continuation"):
                            jobs_mod.set_phase(layout, r["attempt_id"], r["phase"],
                                               deferred_reason=f"reset by owner ({item['id']}): {reason}")
                tasks_mod.set_status(layout, task_id, TaskStatus.READY, actor="owner",
                                     note=f"{item['id']}: owner re-queued the task")
            out["resumed"] = resume
        else:
            if note:
                tasks_mod.add_note(layout, task_id, f"{item['id']} ({out['response']}): {note}", actor="owner")
            out["skipped"] = "deferred"
    except tasks_mod.TaskError as exc:
        out["error"] = str(exc)[:200]

    def mark(doc):
        doc["applied"] = {"at": store.now_iso(), "by": by, **out}
        return doc
    store.update(path_for(layout, item["id"]), mark, lock_path=layout.state / ".inbox.lock")
    tasks_mod.log(layout, "decision_applied", by, item=item["id"], task=task_id, **out)
    return out
