"""Owner inbox store (state/inbox/*.yaml): items a human must answer before a task can proceed.
Two rules the loop depends on: no more than `MAX_OPEN_PER_ROLE` open items per role at once (the owner's attention is
the scarce resource), and a human gate never times out — items wait until answered, however long that takes.
Answers accept the structured keywords (ACCEPT/REJECT/MODIFY/REDO, optionally followed by an
argument, e.g. `MODIFY shorter summary`) or, unless an item disables it, free text — the owner's own words count as a
valid answer, not only the keywords. `HUMAN_INBOX.md` is rendered from this store (render.render_inbox), never parsed.

An item may also carry typed `choices`: each one a label and detail rendered by
`plans.render_choice` from its own effect — never prose a caller writes beside it — and an effect `create()` has
already checked this release can apply, with the operands it needs. `create()` derives the legacy keyword list
from them, `answer()` refuses an unoffered one or a stale `choices_digest` — on the keyword path as on the
button's, since both name a choice — and `apply_choice()` applies the chosen effect, checking the digest once
more where the effect actually lands: one or several plan operations as a single journalled plan, or one
owner-only effect, through the same applier `plans.py` uses for a plan. Free text on such an item is a comment
and never an effect. An item with no `choices` key is unaffected by any of this."""
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
          extra: dict | None = None, detail: str | None = None, dedup_key: str | None = None,
          choices: list[dict] | None = None) -> dict:
    """Write a new open item. Enforces the per-role cap and the prose budget (summary ≤ MAX_SUMMARY_CHARS; the
    optional `detail` — the full text, see fit() — ≤ MAX_DETAIL_CHARS); the cap check and id allocation happen
    under one lock so concurrent creates never collide or overshoot. `extra` adds fields without overriding any.

    `dedup_key`, paired with `kind`, is checked under that same lock, before the cap: a call whose kind+dedup_key
    already names an existing item (open or answered) returns that item unchanged instead of writing another or
    counting against the cap. This is the atomic form `plugin.open_owned_item` needs for its own idempotence —
    scanning for a match outside this lock and then re-entering it to create one is not an option, since
    `locks.FileLock` is not re-entrant (a second acquire by the same thread raises `LockHeld` immediately).

    `allowed_responses=[]`, given explicitly (not the default None), means the item takes no structured keyword at
    all — only free text (an alert naming problems nobody can fix with ACCEPT/REJECT/MODIFY/REDO, R-06/R-17). `None`
    still means "the four defaults"; only an explicit empty list opts out of them.

    `choices`: typed buttons, each `{id, effect, preconditions?}` — see
    `_validate_choice`, `answer()` and `apply_choice()`. Every one is checked here, before it is ever offered:
    its `effect` must be an operation (or a non-empty list of them) this release can actually apply — a
    plan-tier list run through `plans.validate` itself, an owner-only effect against the operands it needs — and
    its stored `label`/`detail` are always `plans.render_choice` of that effect, never whatever the caller wrote
    there (if anything); a caller-written `label`/`detail` is silently replaced, not trusted. `choices_digest` is
    then computed from the normalised list (`plans.choices_digest`) and, unless the caller also names its own
    `allowed_responses`, that list is derived from the choice ids (one keyword — the id, upper-cased — per
    choice) instead of the four defaults, so the keyword path and the typed-choice path name the same thing. An
    item built with no `choices` at all carries neither field and is exactly today's item."""
    if len(summary) > MAX_SUMMARY_CHARS:
        raise InboxError(f"summary is {len(summary)} characters; limit is {MAX_SUMMARY_CHARS}")
    if detail is not None and len(detail) > MAX_DETAIL_CHARS:
        raise InboxError(f"detail is {len(detail)} characters; limit is {MAX_DETAIL_CHARS}")
    normalised_choices = [_validate_choice(layout, task_id, c) for c in choices] if choices else None
    if normalised_choices and allowed_responses is None:
        allowed_responses = [c["id"].upper() for c in normalised_choices]
    with FileLock(layout.state / ".inbox.lock", owner="inbox", timeout=5.0):
        if dedup_key is not None:
            for item in all_items(layout):
                if item.get("kind") == kind and item.get("dedup_key") == dedup_key:
                    return item
        if len(open_items(layout, for_role)) >= MAX_OPEN_PER_ROLE:
            raise InboxFull(for_role)
        item_id = _next_id(layout)
        doc = {"id": item_id, "kind": kind, "for": for_role, "from": from_, "status": "open", "subject": subject,
               "summary": summary,
               "allowed_responses": list(DEFAULT_RESPONSES if allowed_responses is None else allowed_responses),
               "free_text": free_text, "task_id": task_id, "attempt_id": attempt_id}
        if approval_digest:
            doc["approval_digest"] = approval_digest
        if detail and detail.strip() != summary.strip():
            doc["detail"] = detail
        if normalised_choices:
            from . import plans as plans_mod
            doc["choices"] = normalised_choices
            doc["choices_digest"] = plans_mod.choices_digest(doc["choices"])
        payload = dict(extra or {})
        if dedup_key is not None:
            payload.setdefault("dedup_key", dedup_key)
        for k, v in payload.items():
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


def answer(layout: Layout, item_id: str, response: str = "", note: str = "", by: str = "owner",
          command_id: str | None = None, internal: bool = False, digest: str | None = None,
          choice: str | None = None, choices_digest: str | None = None) -> dict:
    """Validate and record an answer; idempotent by command_id (a retried command must not raise "already answered").

    `internal=True` is for the code of the plugin that owns the item's kind (registry.inbox_kind with
    `answerable=False`): it carries per-option verdicts only that plugin knows how to finalise, so the generic path
    — this function as called by the command handler and the Control Center form — refuses to close one out from
    under it.

    `digest`, when given, must be the item's own `approval_digest`: the approval card's hidden field carries back
    what it showed, so an item left open through a newer delivery is refused here, under the same lock, before the
    item is spent — not left to fail later inside record_approval() with the item already marked answered (R-05).

    `choice`, when given, names one of the item's own `choices[].id`: refused with
    RF-24 (`plans.choice_not_offered_refusal`) when the item carries no such choice, and with RF-25
    (`plans.choice_stale_refusal`) — "reload" — when `choices_digest` no longer equals the item's own
    `choices_digest`, the approval-digest rule generalised to a button rather than a delivery. `response` then
    defaults to the choice's own id, so the stored answer and its derived keyword (`allowed_responses`) agree
    with what gets applied below.

    A bare keyword that is exactly one of those derived ids (`inbox.answer REQUEUE` on an item whose choice id is
    `requeue`) names the same choice and is held to the same two refusals: an answer that names a choice carries
    the digest it was shown, whichever argument names it, so there is no path to an effect that skips the check.
    Prose that merely *begins* with such a keyword ("REQUEUE after the other one lands") names nothing: it is
    recorded as a comment, because free text is never an effect (section 4.1). What the applier reads is written
    on the answer itself (`choice`, `choices_digest`), and the effect is applied once this answer is durable
    (`apply_choice`), through the same journalled applier a plan uses."""
    if choice:
        response = response.strip() or choice
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
        if digest and doc.get("task_id"):
            try:
                current = tasks_mod.load(layout, doc["task_id"]).delivery.digest
            except tasks_mod.TaskError:
                current = None
            if current != digest:
                raise InboxError(f"{item_id}: the delivery changed since this item was shown "
                                 f"(shown {digest[:12]}, now {(current or 'none')[:12]}); reload and review it again")
        if not internal:
            refusal = owned_by_plugin(layout, doc)
            if refusal:
                raise InboxError(refusal)
        stored = doc.get("allowed_responses")
        allowed = DEFAULT_RESPONSES if stored is None else stored
        token = response.split(None, 1)[0].upper()
        structured = token in {a.upper() for a in allowed}
        carried = [str(c.get("id")) for c in (doc.get("choices") or []) if isinstance(c, dict)]
        named = (choice or "").strip() or None
        if named is None and carried and structured and response.upper() == token:
            # a keyword answer that is nothing but one of the derived keywords: the other way to name a choice
            named = next((c for c in carried if c.upper() == token), None)
        if named is None and carried and token in {c.upper() for c in carried}:
            # prose about a choice is prose, whatever word it opens with: a comment, never that choice's effect
            structured = False
        if named is not None:
            from . import plans as plans_mod
            refusal = plans_mod.choice_not_offered_refusal(carried, named)
            if refusal is None:
                refusal = plans_mod.choice_stale_refusal(str(doc.get("choices_digest") or ""), choices_digest or "")
            if refusal is not None:
                missing = (refusal["code"] == "RF-25" and not choices_digest)
                raise InboxError(f"{item_id}: {refusal['code']} {refusal['message']}"
                                 + (" (an answer that names a choice carries the choices_digest the item showed)"
                                    if missing else ""))
        if not structured and not doc.get("free_text", True):
            raise InboxError(f"{item_id}: {response!r} is not one of {allowed}")
        doc["status"] = "answered"
        doc["answered_at"] = store.now_iso()
        doc["answer"] = {"response": response, "kind": token if structured else "text", "note": note, "by": by,
                         "at": store.now_iso(), "command_id": command_id}
        if named is not None:
            # what `apply_choice` reads: the choice this answer named and the digest it was bound to, so the
            # effect is checked against the item again where it is applied, not only here
            doc["answer"]["choice"] = named
            doc["answer"]["choices_digest"] = choices_digest
        outcome["kind"] = token if structured else "text"
        return doc

    doc = store.update(path_for(layout, item_id), fn, lock_path=layout.state / ".inbox.lock")
    if not outcome.get("skipped"):
        tasks_mod.log(layout, "inbox_answer", by, item=item_id, response=outcome.get("kind"), task=doc.get("task_id"))
        apply_approval(layout, doc, by=by, command_id=command_id)
        apply_decision(layout, doc, by=by)
        if apply_choice(layout, doc, by=by) is not None:
            # apply_choice's own store.update wrote `choice_applied` after `doc` was read above; reload so the
            # caller (the command result, the Control Center) can see it rather than a stale copy without it.
            doc = load(layout, item_id)
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
                    # Called every time regardless of whether this call's own reset_retries() made a fresh grant
                    # (F2): grant_continuations() is generation-scoped, so a replay only finishes retiring an
                    # earlier generation's own markers and never touches one the current chain already created.
                    escalation_mod.grant_continuations(layout, task_id, reason=f"owner REQUEUE ({item['id']})")
                tasks_mod.set_status(layout, task_id, TaskStatus.READY, actor="owner",
                                     note=f"{item['id']}: owner re-queued the task")
            out["resumed"] = resume
        else:
            if note:
                tasks_mod.add_note(layout, task_id, f"{item['id']} ({out['response']}): {note}", actor="owner")
            out["skipped"] = "deferred"
            out["message"] = "recorded as a comment; the task is still stopped"
            # Neither DEFER nor free text resumes the task - it is still BLOCKED/FAILED. Opening a fresh decision
            # item here, naming what just happened, is the common case that keeps it in the owner's list with a
            # way to act (REQUEUE/CANCEL/DEFER again); an owner who keeps writing free text keeps getting a fresh
            # one, same as before this item was answered. But this create() can fail for want of room in the
            # owner's inbox, so the guarantee that the task stays watched must not live here alone: loop.py's
            # sweep_stalled never treats `skipped: "deferred"` (set just above, whether or not the reopen below
            # succeeds) as a resolved stop, so a stall still escalated on its own schedule even when the cap
            # leaves this reopen with nowhere to go (R-06).
            try:
                choices = item.get("choices")
                reopened = create(layout, for_role="owner", subject=item.get("subject") or f"Decision needed: {task_id}",
                                  summary=f"{item['id']} was answered ({out['response']}): recorded as a comment; "
                                          f"{task_id} has not moved and is still stopped. REQUEUE = re-run it; "
                                          "CANCEL = cancel it; DEFER = leave it stopped.",
                                  kind=DECISION_KIND, allowed_responses=(None if choices else list(DECISION_RESPONSES)),
                                  choices=choices, free_text=True, from_="loop", task_id=task_id,
                                  extra={"resume": item.get("resume") or "task", "blocked_at": item.get("blocked_at"),
                                         "reopened_from": item["id"]})
                out["reopened"] = reopened["id"]
            except InboxFull:
                tasks_mod.log(layout, "decision_reopen_full", by, item=item["id"], task=task_id)
    except tasks_mod.TaskError as exc:
        out["error"] = str(exc)[:200]

    def mark(doc):
        doc["applied"] = {"at": store.now_iso(), "by": by, **out}
        return doc
    store.update(path_for(layout, item["id"]), mark, lock_path=layout.state / ".inbox.lock")
    tasks_mod.log(layout, "decision_applied", by, item=item["id"], task=task_id, **out)
    return out


# ------------------------------------------------------------------ typed choices
#
# A choice's `effect` is one plan-tier operation, a list of them (the "two-step instruction becomes one choice"
# rule — `set_job` then `resume`, applied as a single journalled plan), or one owner-only effect
# (`plans.OWNER_ONLY_EFFECTS`) on its own. The two tiers are never mixed in one choice: `_validate_choice` is
# where that is actually enforced, not merely asserted — a plan-tier list goes through `plans.validate` itself,
# which refuses RF-26 for any operation named in `plans.OWNER_ONLY_EFFECTS`, so a list mixing the tiers is
# refused the same way a machine-authored plan carrying one would be. An owner-only effect is not itself
# journalled through the plan applier — it is the one call the framework already makes for an owner's command
# (hold, a cancel, the existing plan applier called directly for apply_plan/cancel_plan).

# The owner-only effects this release's synchronous answer path can actually apply, each with the operand(s) its
# effect must carry. `diagnose_now` needs more than `layout` alone — a diagnosis is dispatched through a
# `loop.Supervisor` (`diagnosis.dispatch`'s own signature) — so `_apply_owner_effect` builds one of its own
# (wiring diagnose_now is why_not_runnable's own step); `Supervisor.__init__`
# starts no loop and no job, so building one here for a single dispatch call is exactly as cheap as any other
# owner-only effect below. A choice missing a required operand is refused at `create()` rather than offered as a
# button that always raises when clicked.
_OWNER_EFFECT_REQUIRED = {"apply_plan": ("plan",), "cancel_plan": ("plan",), "cancel_task": ("task",),
                          "hold": ("task",), "release_hold": ("task",), "diagnose_now": ("task",)}
_CHOICE_FIELDS = frozenset({"id", "effect", "label", "detail", "preconditions"})


def _effect_ops(choice: dict) -> list[dict]:
    effect = choice.get("effect")
    if isinstance(effect, list):
        return [e for e in effect if isinstance(e, dict)]
    if isinstance(effect, dict):
        return [effect]
    return []


def _validate_choice(layout: Layout, task_id: str | None, choice: dict) -> dict:
    """One `choices[]` entry, checked before it is ever offered (only ones the
    underlying command would accept are offered) and returned normalised: `label`/`detail` are always
    `plans.render_choice` of the effect — whatever the caller wrote there, if anything, is replaced, never
    trusted (section 4.2) — so a stored label can never promise more than its own effect performs.

    An owner-only effect (a bare `effect` dict naming one of `plans.OWNER_ONLY_EFFECTS`) is checked against
    `_OWNER_EFFECT_REQUIRED`: one missing an operand it needs is refused here. Anything else — one plan operation
    or a list of them — is checked by building the plan `apply_choice` would propose and handing it to
    `plans.validate` whole, so a plan-tier choice is refused on exactly the grounds a plan would be, RF-26 (an
    owner-only effect inside it) among them."""
    from . import plans as plans_mod
    if not isinstance(choice, dict):
        raise InboxError("a choice must be an object")
    cid = str(choice.get("id") or "").strip()
    if not cid:
        raise InboxError("a choice must carry a non-empty id")
    extra = set(choice) - _CHOICE_FIELDS
    if extra:
        raise InboxError(f"choice {cid!r}: unexpected field(s) {sorted(extra)}")
    effect = choice.get("effect")
    if isinstance(effect, dict):
        ops = [effect]
    elif isinstance(effect, list) and effect:
        ops = list(effect)
    else:
        raise InboxError(f"choice {cid!r}: effect must be an operation object or a non-empty list of operations")
    if any(not isinstance(op, dict) or not op.get("op") for op in ops):
        raise InboxError(f"choice {cid!r}: every operation in effect must be an object naming a non-empty op")
    if isinstance(effect, dict) and effect["op"] in plans_mod.OWNER_ONLY_EFFECTS:
        name = effect["op"]
        required = _OWNER_EFFECT_REQUIRED.get(name)
        if required is None:
            raise InboxError(f"choice {cid!r}: {name!r} cannot be applied without the running loop "
                             "(not yet wired this release)")
        missing = [k for k in required if not str(effect.get(k) or "").strip()]
        if missing:
            raise InboxError(f"choice {cid!r}: {name!r} needs {missing}")
        op_extra = set(effect) - {"op", *required}
        if op_extra:
            raise InboxError(f"choice {cid!r}: {name!r} carries unexpected field(s) {sorted(op_extra)}")
    else:
        if not task_id:
            raise InboxError(f"choice {cid!r}: a plan-tier effect needs the item's own task_id")
        try:
            task = tasks_mod.load(layout, task_id)
        except tasks_mod.TaskError as exc:
            raise InboxError(f"choice {cid!r}: {exc}") from exc
        preview = {"plan_schema": plans_mod.PLAN_SCHEMA, "id": f"choice-preview-{cid}", "subject": {"task": task_id},
                  "reason": f"preview of choice {cid!r}", "expected_rev": {task_id: task.rev}, "operations": ops}
        _, refusals = plans_mod.validate(preview, layout)
        if refusals:
            codes = ", ".join(sorted({r["code"] for r in refusals}))
            raise InboxError(f"choice {cid!r}: refused ({codes}): {refusals[0]['message']}")
    label, render_detail = plans_mod.render_choice(effect)
    if label is None:
        raise InboxError(f"choice {cid!r}: its effect cannot be rendered as a label")
    out = {"id": cid, "effect": effect, "label": label}
    if render_detail:
        out["detail"] = render_detail
    if "preconditions" in choice:
        out["preconditions"] = choice["preconditions"]
    return out


def _apply_diagnose_now(layout: Layout, task_id: str) -> dict:
    """`diagnose_now`: dispatch a diagnosis for `task_id` right now, through a `loop.Supervisor` built for this one
    call (`Supervisor.__init__` starts no loop and no job, so this costs no more than any other owner-only effect
    — no more than any other effect). The source and the signature are `why_not_runnable`'s own reason, read
    fresh rather than carried on the effect (RF-04's own rule, generalised: an effect names an operand, never a
    value a diagnosis would turn around and read back as if it had written it)."""
    from . import diagnosis as diagnosis_mod
    from . import escalation as escalation_mod
    from . import tasks as tasks_mod2
    from . import why_not_runnable as why_not_runnable_mod
    from .loop import Supervisor
    task = tasks_mod2.load(layout, task_id)
    why = why_not_runnable_mod.why_not_runnable(task, tasks_mod2.load_all(layout), layout)
    detail = why_not_runnable_mod.render_reason(why)
    sig = escalation_mod.signature(f"why_not_runnable:{why['code']}:{detail}")
    rec = diagnosis_mod.dispatch(Supervisor(layout), task, "owner", detail, sig, level="manager")
    if rec is None:
        raise InboxError(f"diagnosis could not be dispatched for {task_id} (disabled, or no lane available)")
    return {"attempt_id": rec.get("attempt_id")}


def _apply_owner_effect(layout: Layout, op: dict, by: str, item: dict) -> dict:
    """The six owner-only effects `_OWNER_EFFECT_REQUIRED` lets `_validate_choice` offer: `apply_plan`/
    `cancel_plan` call the plan applier directly (the same one a plan-tier choice's own operations go through,
    §4.1's "same applier"), `cancel_task`/`hold`/`release_hold` call the one existing function each already is,
    and `diagnose_now` is `_apply_diagnose_now` above."""
    from . import plans as plans_mod
    name = op.get("op")
    note = f"{item['id']}: owner choice"
    if name == "apply_plan":
        return plans_mod.apply(layout, op["plan"])
    if name == "cancel_plan":
        return plans_mod.cancel_plan(layout, op["plan"], by=by)
    if name == "cancel_task":
        t = tasks_mod.set_status(layout, op["task"], "CANCELLED", actor=by, note=note)
        return {"status": t.status.value}
    if name == "hold":
        t = tasks_mod.hold(layout, op["task"], by, note=note)
        return {"status": t.status.value}
    if name == "diagnose_now":
        return _apply_diagnose_now(layout, op["task"])
    if name == "release_hold":
        t = tasks_mod.release_hold(layout, op["task"], by, note=note)
        return {"status": t.status.value}
    raise InboxError(f"{name!r} cannot be applied without the running loop (not yet wired this release)")


def _apply_plan_tier_effect(layout: Layout, item: dict, choice: dict, ops: list[dict], by: str) -> dict:
    """Both operations of a two-step choice (or the one operation of a single-step choice) applied as one plan,
    through the same `plans.propose`/`plans.apply` the diagnosis lane uses — never a second, ad hoc writer."""
    from . import plans as plans_mod
    task_id = item.get("task_id")
    task = tasks_mod.load(layout, task_id)
    plan_id = f"choice-{item['id']}-{choice['id']}"
    doc = {"plan_schema": plans_mod.PLAN_SCHEMA, "id": plan_id, "subject": {"task": task_id},
          "reason": f"owner choice {choice['id']!r} on inbox item {item['id']}",
          "expected_rev": {task_id: task.rev}, "operations": ops}
    plans_mod.propose(layout, doc, by=by)
    applied = plans_mod.apply(layout, plan_id)
    if applied.get("status") != "applied":
        raise InboxError(f"choice {choice['id']!r} did not apply cleanly ({applied.get('status')}: "
                         f"{[r.get('code') for r in applied.get('refusals', [])] or applied.get('last_error')})")
    return {"plan": plan_id, "status": applied.get("status")}


def resolve_choice(item: dict, choice_id: str) -> dict | None:
    return next((c for c in (item.get("choices") or []) if isinstance(c, dict) and c.get("id") == choice_id), None)


def _reopen_after_choice_failure(layout: Layout, item: dict, out: dict) -> tuple[str | None, str | None]:
    """A choice whose effect failed to apply must leave the owner with another actionable item, not a silently
    answered one — the exact symptom R-17 names ("answering correctly changed nothing") survives on the failure
    path otherwise. Reopened with the same choices, the same rule `apply_decision` already keeps for a deferred
    decision (free text, DEFER).

    Returns `(reopened id, error)`: `(None, None)` when the item carries no task to watch, and `(None, reason)`
    when the reopen itself was refused — the owner's inbox has no room, or the carried choices no longer validate
    against the task's new state after a half-applied effect, which `create()` checks. Nothing is raised: the
    caller records the reason on `choice_applied`, so a failure of the failure path is still visible."""
    if not item.get("task_id") or not item.get("choices"):
        return None, None
    try:
        reopened = create(layout, for_role=item.get("for") or "owner",
                          subject=item.get("subject") or f"Choice failed on {item['id']}",
                          summary=f"{item['id']}'s choice {out.get('choice')!r} did not apply "
                                  f"({str(out.get('error') or '')[:200]}); pick again, or answer in free text.",
                          kind=item.get("kind") or DECISION_KIND, choices=item.get("choices"), free_text=True,
                          from_="loop", task_id=item.get("task_id"), extra={"reopened_from": item["id"]})
        return reopened["id"], None
    except InboxFull:
        tasks_mod.log(layout, "choice_reopen_full", "loop", item=item["id"], task=item.get("task_id"))
        return None, "the owner's inbox has no room for a fresh item"
    except Exception as exc:                              # noqa: BLE001 - recorded on the marker, never raised
        tasks_mod.log(layout, "choice_reopen_failed", "loop", item=item["id"], task=item.get("task_id"),
                      error=str(exc)[:200])
        return None, str(exc)[:200]


def apply_choice(layout: Layout, item: dict, by: str = "owner") -> dict | None:
    """Apply the effect of the choice an answered item's answer names, once.

    What is applied comes from the answer's own `choice`/`choices_digest`, which `answer()` writes only for an
    answer that named a choice and carried the digest it was shown: `None` here when the item carries no
    `choices`, or the answer named none of them (free text, a keyword matching nothing) — the comment-only path
    is unaffected. The digest is then checked again, right where the effect is about to be applied, because a
    stored answer can be applied later than it was recorded (a retried command, a recovered record) and the
    item's choices may have moved since: a mismatch is RF-25, recorded and never applied.

    Idempotent: an item already carrying `choice_applied` is never acted on twice, the same guarantee
    `apply_decision`'s own `applied` marker gives the decision path. A failure is recorded on `choice_applied`
    (`ok: False`, `error`) before anything else is attempted, and then reopens a fresh item
    (`_reopen_after_choice_failure`) so the failure is visible to the owner, not only to the log."""
    if item.get("status") != "answered" or item.get("choice_applied"):
        return None
    choices = item.get("choices")
    if not choices:
        return None
    answer = item.get("answer") or {}
    named = str(answer.get("choice") or "")
    if not named:
        return None
    from . import plans as plans_mod
    carried = [str(c.get("id")) for c in choices if isinstance(c, dict)]
    refusal = (plans_mod.choice_not_offered_refusal(carried, named)
               or plans_mod.choice_stale_refusal(str(item.get("choices_digest") or ""),
                                                 str(answer.get("choices_digest") or "")))
    choice = resolve_choice(item, named)
    if refusal is not None or choice is None:
        out = {"ok": False, "choice": named,
               "error": (f"{refusal['code']} {refusal['message']}" if refusal is not None
                         else f"{named!r} is not one of this item's choices")}
    else:
        ops = _effect_ops(choice)
        try:
            if len(ops) == 1 and ops[0].get("op") in plans_mod.OWNER_ONLY_EFFECTS:
                result = _apply_owner_effect(layout, ops[0], by, item)
            else:
                result = _apply_plan_tier_effect(layout, item, choice, ops, by)
            out = {"ok": True, "choice": named, "result": result}
        except Exception as exc:                          # noqa: BLE001 - recorded on the item, never raised past
            out = {"ok": False, "choice": named, "error": str(exc)[:300]}

    def mark(doc):
        doc["choice_applied"] = {"at": store.now_iso(), "by": by, **out}
        return doc

    # The marker is written before the reopen is attempted, and again once the reopen's outcome is known: the
    # reopen can itself be refused (create() re-validates the carried choices against the task's new state), and
    # an answered item must never end up durable with neither an effect nor a record of why.
    store.update(path_for(layout, item["id"]), mark, lock_path=layout.state / ".inbox.lock")
    if not out["ok"]:
        reopened, reopen_error = _reopen_after_choice_failure(layout, item, out)
        if reopened:
            out["reopened"] = reopened
        if reopen_error:
            out["reopen_error"] = reopen_error
        if reopened or reopen_error:
            store.update(path_for(layout, item["id"]), mark, lock_path=layout.state / ".inbox.lock")
    tasks_mod.log(layout, "inbox_choice_applied", by, item=item["id"], **out)
    return out
