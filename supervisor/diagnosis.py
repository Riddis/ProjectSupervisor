"""The exception route: manager → architect → owner.

When supervisor.escalation stops retrying (same failure signature twice, or the revision budget spent) it used to open an
issue saying "owner decision needed" and stop. Now it also dispatches ONE bounded diagnosis job to the manager role
(`senior_pm` by default) on a read-only lane, with the failure evidence frozen in the brief. The diagnosis returns a
structured decision:

    requeue   — the task goes back to READY with written guidance that the next delivery brief carries; the retry
                budget is reset once (DIAGNOSIS_BUDGET diagnoses per task, then straight to the owner);
    defer     — the task stays BLOCKED with what must happen first written on it and on the issue;
    architect — the question is design, not execution: one more diagnosis by the architect role (never a third level);
    owner     — a product choice or an action only the owner can perform: one inbox item, the only way this route
                reaches the owner's attention.

A failed or malformed diagnosis never loops: it falls through to `owner`. One diagnosis per (task, signature, level),
checked against the job records, so a replayed escalation cannot spend a second one. The job goes through
Supervisor.dispatch → launch → supervisor.gate like every other job (decision, capacity floor, agent limit)."""
from __future__ import annotations

import yaml

from . import inbox as inbox_mod, jobs as jobs_mod, store, tasks as tasks_mod
from ._vendor.contract import CapacityDeferred
from .adapters import REGISTRY
from .models.task import Task, TaskStatus
from .paths import Layout

KIND = "diagnosis"
ACTIONS = ("requeue", "defer", "architect", "owner")
LEVELS = ("manager", "architect")
DEFAULT_ROLES = {"manager": "senior_pm", "architect": "chief_architect"}
DIAGNOSIS_BUDGET = 2            # requeues granted by diagnosis per task; after that the owner decides

RESULT_CONTRACT = (
    "## Result contract\nEnd your answer with exactly one fenced JSON block:\n"
    '```json\n{"kind":"diagnosis","schema_version":1,"action":"requeue|defer|architect|owner","summary":"one paragraph: '
    'what actually went wrong","guidance":"(requeue) concrete instructions for the next attempt","blocked_on":"(defer) what '
    'must happen first","question":"(owner) the one decision or action only the owner can take"}\n```\n'
    "`requeue` means you are confident a fresh attempt with your guidance will pass the SAME verification and reviews; "
    "do not requeue to try the same thing again. `architect` is for a design question, not an execution problem. "
    "`owner` is for product choices, spending, external commitments, or physical actions on the workstation.\n")


def settings(layout: Layout) -> dict:
    cfg = layout.config / "routing.yaml"
    out = {"enabled": True, "roles": dict(DEFAULT_ROLES)}
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        esc = data.get("escalation") or {}
        if "diagnosis" in esc:
            out["enabled"] = bool(esc["diagnosis"])
        for level in LEVELS:
            if esc.get(f"{level}_role"):
                out["roles"][level] = str(esc[f"{level}_role"])
    return out


def _records(layout: Layout, task_id: str) -> list[dict]:
    return [r for r in jobs_mod.for_task(layout, task_id) if (r.get("spec") or {}).get("kind") == KIND]


def already(layout: Layout, task_id: str, signature: str, level: str) -> dict | None:
    for r in _records(layout, task_id):
        if r.get("diag_signature") == signature and r.get("diag_level") == level and r.get("phase") != "FAILED":
            return r
    return None


def count_requeues(layout: Layout, task_id: str) -> int:
    return sum(1 for r in _records(layout, task_id) if r.get("diag_action") == "requeue")


def brief(layout: Layout, task: Task, role: str, level: str, source: str, detail: str, issue_id: str | None) -> str:
    from . import packets
    card = packets.role_card(layout, role)
    attempts = "\n".join(f"- {a.id} ({a.resource}): {a.note}" for a in task.attempts[-6:]) or "- none recorded"
    notes = "\n".join(f"- {n.get('by')}: {n.get('note')}" for n in ((task.model_extra or {}).get("notes") or [])[-6:]) or "- none"
    levels = ("requeue, defer, architect or owner" if level == "manager" else "requeue, defer or owner")
    text = ((card + "\n\n") if card else "") + (
        f"# Diagnosis for {task.id}: {task.title}\n\nRole: {role} ({level} level). Repository: {layout.root}\n\n"
        f"Automatic retries for this task have stopped. Decide what happens next — {levels}. Read the repository and "
        f"the run directories under runs/{task.id}/; do not modify anything.\n\n"
        + packets.contract_block(task) +
        "\n## Expected artifacts\n" + "\n".join(f"- {a}" for a in task.artifacts) +
        f"\n\n## Why retries stopped ({source})\n```\n{detail[:4000]}\n```\n\n## Attempts\n{attempts}\n\n## Notes on the task\n{notes}\n"
        + (f"\n## Issue\n{issue_id}\n" if issue_id else "") + "\n" + RESULT_CONTRACT)
    from . import policy
    return policy.assert_packet_clean(text, layout, f"diagnosis packet {task.id}")


def _task_view(layout: Layout, task: Task, role: str, adapter_name: str, prompt: str, overrides: dict) -> Task:
    from . import review
    doc = task.model_dump(mode="json")
    doc["owner_role"] = role
    doc["job"] = {"adapter": adapter_name, "role": role, "prompt": prompt, "result_kind": KIND,
                  **review.review_lane(layout, role), **overrides}
    return Task.model_validate(doc)


def dispatch(supervisor, task: Task, source: str, detail: str, signature: str, level: str = "manager",
             issue_id: str | None = None) -> dict | None:
    """One diagnosis job for (task, signature, level), or None when one exists, the route is disabled, the role's lane
    is unavailable, or its capacity is out (the escalation then stands as an issue + BLOCKED task, as before)."""
    from . import review
    layout = supervisor.layout
    cfg = settings(layout)
    if not cfg["enabled"] or level not in LEVELS:
        return None
    if already(layout, task.id, signature, level):
        return None
    role = cfg["roles"][level]
    try:
        adapter_name, overrides, same_vendor = review.lane_for(layout, role)
    except CapacityDeferred:
        tasks_mod.log(layout, "diagnosis_deferred", "escalation", task=task.id, role=role, reason="capacity")
        return None
    if adapter_name not in REGISTRY:
        return None
    view = _task_view(layout, task, role, adapter_name, brief(layout, task, role, level, source, detail, issue_id), overrides)
    rec = supervisor.dispatch(view, adapter_name, note=f"diagnosis:{role}", kind=KIND,
                              extra={"diag_signature": signature, "diag_level": level, "diag_issue": issue_id,
                                     "diag_source": source, "diag_detail": detail[:4000], "diag_role": role})
    tasks_mod.log(layout, "diagnosis_dispatched", "escalation", task=task.id, role=role, level=level, issue=issue_id,
                  attempt=(rec or {}).get("attempt_id"))
    return rec


def rebuild(layout: Layout, rec: dict) -> Task | None:
    """Task view for a capacity continuation of a diagnosis job."""
    task = tasks_mod.load(layout, rec["task_id"])
    role, level = rec.get("diag_role"), rec.get("diag_level")
    if not role or task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        return None
    adapter_name = (rec.get("spec") or {}).get("adapter", "claude_cli")
    return _task_view(layout, task, role, adapter_name,
                      brief(layout, task, role, level, rec.get("diag_source", ""), rec.get("diag_detail", ""), rec.get("diag_issue")), {})


def _annotate_issue(layout: Layout, issue_id: str | None, **fields) -> None:
    if not issue_id:
        return
    p = layout.issues / f"{issue_id}.yaml"
    if not p.exists():
        return
    def fn(doc):
        doc.setdefault("diagnosis", []).append({"at": store.now_iso(), **fields})
        return doc
    try:
        store.update(p, fn, lock_path=layout.state / ".issues.lock")
    except (OSError, ValueError):
        pass


def route_to_owner(layout: Layout, task: Task, rec: dict, summary: str, question: str, why: str) -> dict:
    """Hand a stopped task to the owner as a `decision` item (inbox.apply_decision acts on the answer). The question
    and the options lead the text: the inbox card shows the first MAX_SUMMARY_CHARS and keeps the full diagnosis
    behind "Show full message" (before 1.4.0 the diagnosis came first and a long one cut the question off)."""
    role = rec.get("diag_role", "diagnosis")
    head = (f"{question.strip() if question and question.strip() else 'How do you want to proceed?'} "
            f"REQUEUE = re-run the task (text you add is passed on as guidance); CANCEL = cancel it; "
            f"DEFER = leave it stopped. ")
    text, detail = inbox_mod.fit(
        head + f"Why: {task.id} stopped after repeated failures and the {role} could not resolve it ({why}). {summary}")
    try:
        item = inbox_mod.create(layout, for_role="owner", subject=f"Decision needed: {task.id} — {task.title}", summary=text,
                                detail=detail, kind=inbox_mod.DECISION_KIND,
                                allowed_responses=list(inbox_mod.DECISION_RESPONSES), free_text=True,
                                from_="escalation", task_id=task.id, extra={"resume": "task"})
        return {"action": "owner", "inbox": item["id"]}
    except inbox_mod.InboxFull:
        tasks_mod.add_note(layout, task.id, f"owner decision needed ({why}); inbox full — see issue", actor="escalation")
        return {"action": "owner", "inbox": None}


def ingest(supervisor, rec: dict, result) -> dict:
    """Apply a diagnosis result. Writes the decision on the job record (`diag_action`) first, so a crash between the
    record and the task store replays the same branch and never grants a second requeue."""
    from . import escalation
    layout = supervisor.layout
    task = tasks_mod.load(layout, rec["task_id"])
    obj = (result.raw or {}) if isinstance(result.raw, dict) else {}
    action = str(obj.get("action") or "").lower()
    summary = str(obj.get("summary") or "")[:4000]     # the owner route keeps all of it (inbox `detail`)
    level, role, issue_id = rec.get("diag_level", "manager"), rec.get("diag_role", "?"), rec.get("diag_issue")
    if result.status == "FAILED" or action not in ACTIONS or (level == "architect" and action == "architect"):
        why = result.error or (f"action {action!r} not allowed at {level} level" if action else "no structured diagnosis")
        tasks_mod.log(layout, "diagnosis_failed", "loop", task=task.id, role=role, level=level, error=str(why)[:300])
        action, summary = "owner", summary or f"The {role} diagnosis did not produce a usable decision ({why})."
    if task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        tasks_mod.log(layout, "diagnosis_ignored", "loop", task=task.id, reason=f"task is {task.status.value}")
        return {"action": "ignored"}
    jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action=action)
    _annotate_issue(layout, issue_id, role=role, level=level, action=action, summary=summary)
    tasks_mod.log(layout, "diagnosis", "loop", task=task.id, role=role, level=level, action=action, summary=summary[:300])

    if action == "requeue":
        if count_requeues(layout, task.id) > DIAGNOSIS_BUDGET:
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""),
                                   f"diagnosis budget of {DIAGNOSIS_BUDGET} requeues spent")
        guidance = str(obj.get("guidance") or summary)[:4000]

        def fn(doc):
            doc.setdefault("guidance", []).append({"at": store.now_iso(), "by": role, "text": guidance})
        tasks_mod._update(layout, task.id, fn)
        escalation.reset_retries(layout, task.id, reason=f"requeued by {role} diagnosis")
        if task.status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            tasks_mod.set_status(layout, task.id, TaskStatus.READY, actor="escalation",
                                 note=f"{role} diagnosis: requeue — {summary[:200]}")
        else:
            tasks_mod.add_note(layout, task.id, f"{role} diagnosis: requeue — {summary[:200]}", actor="escalation")
        return {"action": "requeue", "guidance": guidance}
    if action == "defer":
        blocked_on = str(obj.get("blocked_on") or summary)[:1000]
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: deferred — blocked on: {blocked_on[:300]}", actor="escalation")
        if task.status == TaskStatus.IN_PROGRESS:
            tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=f"deferred: {blocked_on[:200]}")
        return {"action": "defer", "blocked_on": blocked_on}
    if action == "architect":
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: design question → architect. {summary[:200]}", actor="escalation")
        nxt = dispatch(supervisor, task, rec.get("diag_source", "diagnosis"), rec.get("diag_detail", "") or summary,
                       rec.get("diag_signature", ""), level="architect", issue_id=issue_id)
        if nxt is None:                                        # no architect lane right now: the owner hears about it
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "architect lane unavailable")
        return {"action": "architect", "attempt": nxt.get("attempt_id")}
    tasks_mod.add_note(layout, task.id, f"{role} diagnosis: owner decision — {summary[:200]}", actor="escalation")
    return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "needs the owner")
