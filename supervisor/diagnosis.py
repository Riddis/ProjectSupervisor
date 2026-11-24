"""The exception route: manager → architect → owner.

When supervisor.escalation stops retrying (same failure signature twice, or the revision budget spent) it used to open an
issue saying "owner decision needed" and stop. Now it also dispatches ONE bounded diagnosis job to the manager role
(`senior_pm` by default) on a read-only lane, with the failure evidence frozen in the brief. The diagnosis returns a
structured decision:

    requeue   — the task goes back to READY with written guidance that the next delivery brief carries; the retry
                budget is reset once (DIAGNOSIS_BUDGET diagnoses per task, then straight to the owner);
    defer     — the task waits for a repair named structurally, a `plan`:
                a document of the six typed operations `supervisor.plans` validates. A plan the validator refuses
                gets exactly one more session of this SAME diagnosis job to correct — `cause: continuation`, no new
                cause value — carrying only the frozen evidence, the refused plan verbatim and its refusal list.
                Still refused after that, it reaches the owner with both versions and both refusal lists from the
                one plan document, never offering `apply_plan`: a twice-refused plan is refused whole. A plan that
                does validate is proposed and released — a fresh retry budget, the task back to READY — and applies
                through the same journalled applier (`supervisor.plans`) an owner's own choice uses, on the loop's
                own next turn;
    architect — the question is design, not execution: one more diagnosis by the architect role (never a third level);
    owner     — a product choice or an action only the owner can perform: one inbox item, the only way this route
                reaches the owner's attention.

A failed or malformed diagnosis never loops: it falls through to `owner`. One diagnosis per (task, signature, level),
checked against the job records, so a replayed escalation cannot spend a second one. The job goes through
Supervisor.dispatch → launch → supervisor.gate like every other job (decision, capacity floor, agent limit)."""
from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import ValidationError

from . import inbox as inbox_mod, jobs as jobs_mod, plans as plans_mod, routing_config, store, tasks as tasks_mod
from ._vendor.contract import CapacityDeferred
from .adapters import REGISTRY
from .models.task import Task, TaskStatus
from .paths import Layout

KIND = "diagnosis"
ACTIONS = ("requeue", "defer", "architect", "owner")
LEVELS = ("manager", "architect")
DEFAULT_ROLES = {"manager": "senior_pm", "architect": "chief_architect"}
DIAGNOSIS_BUDGET = 2            # requeues granted by diagnosis per task; then the owner decides


RESULT_CONTRACT = (
    "## Result contract\nEnd your answer with exactly one fenced JSON block:\n"
    '```json\n{"kind":"diagnosis","schema_version":1,"action":"requeue|defer|architect|owner","summary":"one paragraph: '
    'what actually went wrong","guidance":"(requeue) concrete instructions for the next attempt","blocked_on":"(defer) '
    'what must happen first","plan":{"reason":"(defer) one paragraph: what this plan repairs","operations":'
    '[{"op":"create_task","task":{"id":"...","title":"...","objective":"...","owner_role":"...","cost_category":"...",'
    '"owned_paths":["..."],"artifacts":["..."],"tests":["..."]}},{"op":"add_dependency","task":"...","depends_on":'
    '["..."]},{"op":"remove_dependency","task":"...","depends_on":["..."]},{"op":"patch_contract","task":"...",'
    '"fields":{}},{"op":"set_job","task":"...","template":"...","params":{}},{"op":"resume","task":"...","as":'
    '"ready"}]},"question":"(owner) the one decision or action only the owner can take"}\n```\n'
    "`requeue` means you are confident a fresh attempt with your guidance will pass the SAME verification and reviews; "
    "do not requeue to try the same thing again. `defer` must name what releases this task as a `plan`: `reason` plus "
    "`operations`, drawn only from the six typed operations above, in this order and shape — `create_task` (a "
    "contract, the fields of `supervisor task create` take, never a `job`), `add_dependency`/`remove_dependency` "
    "(edges among the subject, a task this plan creates, or the subject's direct neighbours), `patch_contract` "
    "(named contract fields only, never loosened), `set_job` (a template id and typed params, never a literal "
    "command) and exactly one `resume` (always last, always on this task). The supervisor validates the plan; one "
    "refused this way gets exactly one more session of this same diagnosis to correct, then goes to the owner with "
    "both versions. `architect` is for a design question, not an execution problem. "
    "`owner` is for product choices, spending, external commitments, or physical actions on the workstation. A "
    "diagnosis cannot change a task's job directly: use `set_job` inside a `plan`, or, for a repair a plan cannot "
    'express (a process task\'s own command is wrong, not the agent\'s work), `action: "owner"` naming the '
    "`task.set_job` change the task needs in `question`.\n")


def settings(layout: Layout) -> dict:
    out = {"enabled": True, "roles": dict(DEFAULT_ROLES)}
    esc = routing_config.load(layout).get("escalation") or {}
    if "diagnosis" in esc:
        out["enabled"] = bool(esc["diagnosis"])
    for level in LEVELS:
        if esc.get(f"{level}_role"):
            out["roles"][level] = str(esc[f"{level}_role"])
    return out


def _records(layout: Layout, task_id: str) -> list[dict]:
    return [r for r in jobs_mod.for_task(layout, task_id) if (r.get("spec") or {}).get("kind") == KIND]


def already(layout: Layout, task_id: str, signature: str, level: str, grant: int = 0) -> dict | None:
    """`grant` (`escalation.current_grant`) excludes a diagnosis dispatched under an earlier grant generation (R4):
    a repeat failure after a fresh grant must reach its OWN diagnosis, not the one the spent grant already answered.
    A declined requeue (`diag_action == "held"`, round-5 F12) is excluded the same way: the owner's Hold answered
    nothing for this (signature, level, grant), so the next identical escalation must reach its own diagnosis too,
    not the spent decline."""
    for r in _records(layout, task_id):
        if (r.get("diag_signature") == signature and r.get("diag_level") == level and r.get("phase") != "FAILED"
                and int(r.get("diag_grant") or 0) == grant and r.get("diag_action") != "held"):
            return r
    return None


def count_requeues(layout: Layout, task_id: str) -> int:
    return sum(1 for r in _records(layout, task_id) if r.get("diag_action") == "requeue")


def _plan_doc_from_proposal(task: Task, rec: dict, plan_id: str, proposal: Any) -> dict:
    """The envelope `ingest()` hands to `plans.propose()`/`plans.revise()` — `plan_schema`, `id`, `subject` and
    `expected_rev` are the supervisor's own, built from what this diagnosis already knows about the attempt it
    explains, never trusted from the model (R0); `reason` and `operations` are the diagnosis's own proposal,
    `obj["plan"]`, read as loosely as a malformed one needs to be — neither key present is an empty plan, refused
    on its own merits by `plans.validate()`, never an exception here."""
    proposal = proposal if isinstance(proposal, dict) else {}
    subject: dict = {"task": task.id}
    if rec.get("attempt") is not None:
        subject["attempt"] = rec["attempt"]
    if rec.get("parent"):
        subject["job"] = rec["parent"]
    ops = proposal.get("operations")
    return {"plan_schema": plans_mod.PLAN_SCHEMA, "id": plan_id, "subject": subject,
           "reason": str(proposal.get("reason") or "")[:1000], "expected_rev": {task.id: task.rev},
           "operations": ops if isinstance(ops, list) else []}


def _correction_brief(layout: Layout, task: Task, role: str, source: str, detail: str, plan: dict) -> str:
    """§2.5's one correction turn: never `brief()` — no role card, no attempt history, no task notes — only the
    frozen original evidence, the refused plan verbatim, and its refusal list (code, operation index, field,
    message, rule). Nothing else is added and nothing is removed."""
    from . import policy
    refusals = plan.get("refusals") or []
    refusal_lines = "\n".join(
        f"- {r.get('code')} (operation {r.get('op_index')}, field {r.get('field')}): {r.get('message')} "
        f"[{r.get('rule')}]" for r in refusals) or "- (no refusal detail recorded)"
    plan_yaml = yaml.safe_dump({"reason": plan.get("reason"), "operations": plan.get("operations")}, sort_keys=False)
    text = (
        f"# Correction turn for {task.id}: {task.title}\n\nRole: {role}. Repository: {layout.root}\n\n"
        "Your plan was refused. Correct `operations` so it validates — fix exactly what the refusal list names, "
        'nothing else — and answer with the same result contract, `action: "defer"` and the corrected `plan`.\n\n'
        f"## The original evidence this diagnosis was raised on ({source})\n```\n{str(detail)[:4000]}\n```\n\n"
        f"## Your refused plan\n```yaml\n{plan_yaml}```\n\n"
        f"## Why it was refused\n{refusal_lines}\n\n" + RESULT_CONTRACT)
    return policy.assert_packet_clean(text, layout, f"diagnosis correction packet {task.id}")


def _dispatch_correction(supervisor, task: Task, rec: dict, role: str, plan: dict) -> dict | None:
    """One more session of the SAME diagnosis job (`cause: continuation`, `parent` on this session):
    dispatched directly, never through `dispatch()`'s own signature dedup — a correction is not a fresh
    diagnosis. A capacity window (or a failed login, `capacity.mark_login_failed`) is not terminal: `lane_for`'s
    own `CapacityDeferred` names `reset_epoch`, and the correction is dispatched anyway with `deferred_until` set
    to it — the same mechanism `loop.requeue()` already uses to continue every other job kind — so the PREPARED
    record simply waits for `launch()`'s own capacity re-check (loop.py) instead of being treated as unavailable.
    `None` only when the role has no adapter registered at all (a genuinely unavailable lane, never a schedule);
    the caller then escalates to the owner naming that, rather than waiting on a correction that can never run."""
    from . import review
    layout = supervisor.layout
    deferred_until = None
    try:
        adapter_name, overrides, _ = review.lane_for(layout, role)
    except CapacityDeferred as exc:
        tasks_mod.log(layout, "diagnosis_deferred", "escalation", task=task.id, role=role, reason="capacity")
        adapter_name, overrides, deferred_until = review._role_profile(layout, role).get("adapter", "claude_cli"), {}, exc.reset_epoch
    if adapter_name not in REGISTRY:
        return None
    prompt = _correction_brief(layout, task, role, rec.get("diag_source", ""), rec.get("diag_detail", ""), plan)
    view = _task_view(layout, task, role, adapter_name, prompt, overrides)
    extra = {k: v for k, v in rec.items() if k.startswith("diag_")}
    extra["diag_plan_id"] = rec["attempt_id"]
    dispatched = supervisor.dispatch(view, adapter_name, deferred_until=deferred_until,
                                     note=f"diagnosis:{role} (correction)", kind=KIND,
                                     extra=extra, cause="continuation", attempt=rec.get("attempt"),
                                     parent=rec["attempt_id"])
    tasks_mod.log(layout, "diagnosis_dispatched", "escalation", task=task.id, role=role,
                 level=rec.get("diag_level"), issue=rec.get("diag_issue"), attempt=(dispatched or {}).get("attempt_id"))
    return dispatched


def _defer_plan(supervisor, task: Task, rec: dict, role: str, obj: dict) -> tuple[dict | None, str, bool]:
    """(plan, owner_why, pending) for a defer's `plan`. `pending` means a correction session was just dispatched
    (or was already queued before a crash): nothing is settled yet, and the caller does only the common job-record
    bookkeeping under `action: "defer"` and returns. Otherwise `plan` is the current document (valid or refused)
    and `owner_why`, non-empty, means it must go to the owner — either this IS the one correction session and it
    is still refused, or no adapter is registered for the role at all (a capacity window alone never reaches
    here: `_dispatch_correction` waits it out with `deferred_until` instead)."""
    layout = supervisor.layout
    plan_id = rec.get("diag_plan_id") or rec["attempt_id"]
    is_correction = bool(rec.get("diag_plan_id"))
    doc = _plan_doc_from_proposal(task, rec, plan_id, obj.get("plan"))
    existing = plans_mod.load_plan(layout, plan_id)
    if existing is not None and existing.get("status") != "refused":
        return existing, "", False                             # a replayed ingest: already proposed, nothing to redo
    if existing is None:
        try:
            plan = plans_mod.propose(layout, doc, by=role, proposer_role=role)
        except Exception as exc:                                # noqa: BLE001 - I3 §1.2: never silently. Whatever
            # the proposer raises (a validator bug, a store write failure, anything not already a refusal) must
            # not leave this diagnosis job DONE (it already is, by the time this runs) with no plan, no owner
            # item and no note: see "what this task did not cover" for the one named exception this also covers
            # without yet fixing (P2-3's ValueError on a symlinked path slot).
            why = f"the plan could not be proposed ({type(exc).__name__}: {str(exc)[:200]})"
            tasks_mod.log(layout, "diagnosis_plan_error", "escalation", task=task.id, role=role, error=why[:300])
            return None, why, False
    elif is_correction and not existing.get("revisions"):
        try:
            plan = plans_mod.revise(layout, plan_id, doc, by=role)
        except Exception as exc:                                # noqa: BLE001 - see above, the correction half
            why = f"the corrected plan could not be proposed ({type(exc).__name__}: {str(exc)[:200]})"
            tasks_mod.log(layout, "diagnosis_plan_error", "escalation", task=task.id, role=role, error=why[:300])
            return None, why, False
    else:
        plan = existing                                          # a replayed correction: already revised once
    if plan.get("status") != "refused":
        return plan, "", False
    if is_correction:
        return plan, "the corrected plan was refused again", False
    if any(r.get("parent") == rec["attempt_id"] for r in jobs_mod.for_task(layout, task.id)):
        return plan, "", True                                     # the correction was already queued before a crash
    if _dispatch_correction(supervisor, task, rec, role, plan) is None:
        return plan, f"no adapter is registered for the {role} role; the correction turn could not be dispatched", False
    return plan, "", True


def _route_plan_to_owner(layout: Layout, task: Task, rec: dict, role: str, plan: dict, why: str) -> dict:
    """§2.5's escalation of a plan diagnosis could not settle: one inbox item, the owner-only choices that are
    the owner's in their own right (`cancel_task`, `hold`) — never `apply_plan`, since what reaches here is
    refused whole either way, nothing to apply in part. Two shapes, told apart by `plan["revisions"]`: a
    correction actually ran and was refused again — both versions and both refusal lists from the one plan
    document; or no correction ever ran (`_dispatch_correction` found no adapter registered for the role at
    all — a capacity window alone never reaches here, `_dispatch_correction` waits that out instead) — one
    version, one refusal list, and `why` names why no correction was attempted. Idempotent like `plans`'s own
    owner routes (`_open_owner_choice`): `dedup_key` so a replay never opens a second item, `owner_item` written
    back onto the plan so its page can link the item the owner is holding."""
    def codes(refs):
        return ", ".join(r.get("code", "?") for r in (refs or [])) or "none"
    revisions = plan.get("revisions") or []
    plan_id = plan.get("id")
    if revisions:
        prior = revisions[-1]
        subject = f"Plan refused twice: {task.id} — {task.title}"
        full = (f"Plan for {task.id} was refused twice by the {role} diagnosis ({why}). {plan.get('reason') or ''}\n\n"
               f"First version — refused: {codes(prior.get('refusals'))}\n"
               + yaml.safe_dump({"operations": prior.get("operations")}, sort_keys=False) +
               f"\nCorrected version — refused: {codes(plan.get('refusals'))}\n"
               + yaml.safe_dump({"operations": plan.get("operations")}, sort_keys=False))
        dedup_key = f"plan-refused-twice:{plan_id}"
    else:
        subject = f"Plan refused: {task.id} — {task.title}"
        full = (f"Plan for {task.id} was refused once by the {role} diagnosis; no correction turn ran ({why}). "
               f"{plan.get('reason') or ''}\n\nRefused: {codes(plan.get('refusals'))}\n"
               + yaml.safe_dump({"operations": plan.get("operations")}, sort_keys=False))
        dedup_key = f"plan-refused-once:{plan_id}"
    summary, detail = inbox_mod.fit(full)
    choices = [{"id": "cancel_task", "effect": {"op": "cancel_task", "task": task.id}},
              {"id": "hold", "effect": {"op": "hold", "task": task.id}}]
    try:
        item = inbox_mod.create(layout, for_role="owner", subject=subject, summary=summary, detail=detail,
                                kind=inbox_mod.DECISION_KIND, choices=choices, free_text=True, from_="escalation",
                                task_id=task.id, dedup_key=dedup_key, extra={"resume": "task", "plan": plan_id})
    except inbox_mod.InboxFull:
        tasks_mod.add_note(layout, task.id, f"owner decision needed (plan refused: {why}); inbox full — "
                           f"see plan {plan_id}", actor="escalation")
        return {"action": "owner", "inbox": None, "plan": plan_id}

    def fn(d):
        d["owner_item"] = item["id"]
        return d
    try:
        store.update(plans_mod.path_for(layout, plan_id), fn)
    except Exception:                                  # noqa: BLE001 - the item exists (dedup_key) either way
        pass
    return {"action": "owner", "inbox": item["id"], "plan": plan_id}


def _attempt_summary(ad: dict) -> str:
    """One line of the diagnosis brief's recent-attempts list, read from the stored dict (`Attempt.model_dump()`)
    — never `Attempt.id` as a declared field, which no longer exists. The migrated shape
    (`jobs[]` set) names its jobs; an un-migrated entry names itself by whatever `id` key it was written under."""
    jobs = ad.get("jobs") or []
    if jobs:
        names = ", ".join(f"{j.get('id')} ({j.get('kind')})" for j in jobs)
        return f"- attempt {ad.get('n')}: {names} → {ad.get('outcome') or 'open'}"
    return f"- {ad.get('id', '')} ({ad.get('resource', '')}): {ad.get('note', '')}"


def brief(layout: Layout, task: Task, role: str, level: str, source: str, detail: str, issue_id: str | None) -> str:
    from . import packets
    card = packets.role_card(layout, role)
    attempts = "\n".join(_attempt_summary(a.model_dump()) for a in task.attempts[-6:]) or "- none recorded"
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


def _attempt_and_parent_for_event(layout, task_id: str, event: str | None) -> tuple[int | None, str | None]:
    """(attempt, parent) for a fresh diagnosis raised by `event` (§2.5/§8.2): `event` is usually the failing
    job's own id, resolved through its durable record for the `attempt` it belongs to (or `None` — task-scoped
    — when that job was itself task-scoped); a composite key (`requeue()`'s own `"<id>:continuations"`) is
    stripped to the job id first. An `event` that still does not resolve (a `stall:<timestamp>` key, or none at
    all) falls back to the task's own newest attempt (`tasks.current_attempt`) with no parent named — simpler
    than the migration's own "newest attempt open at the time" (§8.2), which has historical evidence to read
    that a live dispatch does not."""
    if not event:
        return tasks_mod.current_attempt(layout, task_id), None
    job_id = event.split(":", 1)[0]
    rec = jobs_mod.resolve(layout, job_id)
    if rec is not None:
        return rec.get("attempt"), job_id
    return tasks_mod.current_attempt(layout, task_id), None


def dispatch(supervisor, task: Task, source: str, detail: str, signature: str, level: str = "manager",
             issue_id: str | None = None, grant: int = 0, event: str | None = None) -> dict | None:
    """The diagnosis job for (task, signature, level) under this grant generation (`grant`, R4): the EXISTING
    record when one already matches (a replayed hand-off reuses a transition that is already durable — round-3
    review F7 — never a fresh dispatch, so a second architect for the same signature and generation is still
    never created), a freshly dispatched one otherwise, or None when the route is disabled, the role's lane is
    unavailable, or its capacity is out (the escalation then stands as an issue + BLOCKED task, as before). None
    therefore only ever means "no diagnosis is running for this" — a caller that reads it as "unavailable" is
    never looking at one that is in fact already under way.

    `event` (the escalation event that raised this, `decision.get("event")`) names the job this diagnosis
    explains (§2.6's `parent`) and the attempt it belongs to — or neither, when the task has never had a
    delivery yet (§2.5: task-scoped)."""
    from . import review
    layout = supervisor.layout
    cfg = settings(layout)
    if not cfg["enabled"] or level not in LEVELS:
        return None
    existing = already(layout, task.id, signature, level, grant=grant)
    if existing:
        return existing
    role = cfg["roles"][level]
    try:
        adapter_name, overrides, same_vendor = review.lane_for(layout, role)
    except CapacityDeferred:
        tasks_mod.log(layout, "diagnosis_deferred", "escalation", task=task.id, role=role, reason="capacity")
        return None
    if adapter_name not in REGISTRY:
        return None
    view = _task_view(layout, task, role, adapter_name, brief(layout, task, role, level, source, detail, issue_id), overrides)
    diag_attempt, diag_parent = _attempt_and_parent_for_event(layout, task.id, event)
    rec = supervisor.dispatch(view, adapter_name, note=f"diagnosis:{role}", kind=KIND,
                              extra={"diag_signature": signature, "diag_level": level, "diag_issue": issue_id,
                                     "diag_source": source, "diag_detail": detail[:4000], "diag_role": role,
                                     "diag_grant": grant},
                              cause="diagnose", attempt=diag_attempt, parent=diag_parent)
    tasks_mod.log(layout, "diagnosis_dispatched", "escalation", task=task.id, role=role, level=level, issue=issue_id,
                  attempt=(rec or {}).get("attempt_id"))
    return rec


def rebuild(layout: Layout, rec: dict) -> Task | None:
    """Task view for a capacity continuation of a diagnosis job — a plan-correction session's own continuation
    (`diag_plan_id` set) rebuilds `_correction_brief()` from the plan still on disk, never the regular `brief()`;
    a plan that has meanwhile gone (F7, the same unreadable-task window) ends the continuation with no successor,
    exactly as a task that moved past diagnosis already does below."""
    try:
        task = tasks_mod.load(layout, rec["task_id"])
    except tasks_mod.TaskError:
        # F7: an unreadable task file ends a continuation or a reroute the same way a task that has moved past
        # diagnosis does (below) - both requeue() and _reroute_rebuild() already close the refused/continued
        # record with no successor when this returns None.
        return None
    role, level = rec.get("diag_role"), rec.get("diag_level")
    if not role or task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        return None
    adapter_name = (rec.get("spec") or {}).get("adapter", "claude_cli")
    plan_id = rec.get("diag_plan_id")
    if plan_id:
        plan = plans_mod.load_plan(layout, plan_id)
        if plan is None:
            return None
        prompt = _correction_brief(layout, task, role, rec.get("diag_source", ""), rec.get("diag_detail", ""), plan)
    else:
        prompt = brief(layout, task, role, level, rec.get("diag_source", ""), rec.get("diag_detail", ""), rec.get("diag_issue"))
    return _task_view(layout, task, role, adapter_name, prompt, {})


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


_FAKE_OPTIONS_RE = re.compile(r"(?i)\ballowed responses?\s*:[^.\n]*\.?|\ba?nswer (?:with|using)\b[^.\n]*\.?")


def _without_fake_options(question: str) -> str:
    """A diagnosis's own question is model prose, not a contract, and it sometimes invents its own letter or word
    choices ("Allowed responses: A, B") that only it would honour. This item accepts REQUEUE/CANCEL/DEFER (or a
    free-text comment) and nothing else, so printing the diagnosis's invented options invites an answer the item
    cannot act on (R-06: the owner answered exactly what was asked and the task still parked silently)."""
    return _FAKE_OPTIONS_RE.sub("", question or "").strip()


def route_to_owner(layout: Layout, task: Task, rec: dict, summary: str, question: str, why: str) -> dict:
    """Hand a stopped task to the owner as a `decision` item (inbox.apply_decision acts on the answer). The question
    and the options lead the text: the inbox card shows the first MAX_SUMMARY_CHARS and keeps the full diagnosis
    behind "Show full message" (before 1.4.0 the diagnosis came first and a long one cut the question off)."""
    role = rec.get("diag_role", "diagnosis")
    question = _without_fake_options(question)
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


def ingest(supervisor, rec: dict, result) -> dict | None:
    """Apply a diagnosis result. Writes the decision on the job record (`diag_action`) first, so a crash between the
    record and the task store replays the same branch and never grants a second requeue. Returns None only when
    the task file turned unreadable (F7): `loop.ingest()` skips its own `_ensure_in_progress()` guard for this
    kind (both `launch()` and `ingest()`), so this load is the only place that catch would otherwise have run;
    the caller must close the record on None rather than fall through to its own, since this already did."""
    from . import escalation
    layout = supervisor.layout
    try:
        task = tasks_mod.load(layout, rec["task_id"])
    except tasks_mod.TaskError:
        jobs_mod.set_phase(layout, rec["attempt_id"], "DONE", result=result.status, finished=store.now_iso(),
                           error=result.error)
        tasks_mod.log(layout, "job_done", "loop", task=rec["task_id"], attempt=rec["attempt_id"], result=result.status,
                      error=result.error, task_unreadable=True)
        return None
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
    blocked_on = str(obj.get("blocked_on") or summary)[:1000]
    plan, owner_why, closed = None, "", False
    if action == "defer" and not task.held:
        # plans.validate() refuses a subject with any open job (RF-20) — and this diagnosis job is itself the
        # open one until it closes. Close it now: the same transition loop.ingest()'s own tail call makes once
        # this function returns, made here first instead, so the plan this same call proposes never refuses on
        # finding the diagnosis that is proposing it still running.
        jobs_mod.set_phase(layout, rec["attempt_id"], "DONE", result=result.status, finished=store.now_iso())
        closed = True
        plan, owner_why, pending = _defer_plan(supervisor, task, rec, role, obj)
        if pending:
            # The correction turn is dispatched (or was already, before a crash): this session's own record closes
            # under "defer", not a final action yet — the continuation decides that, in its own call to ingest().
            jobs_mod.set_phase(layout, rec["attempt_id"], "DONE", diag_action="defer",
                               diag_plan=plan.get("id"), continued=True)
            _annotate_issue(layout, issue_id, role=role, level=level, action="defer", summary=summary)
            tasks_mod.log(layout, "diagnosis", "loop", task=task.id, role=role, level=level, action="defer",
                          summary=summary[:300])
            return {"action": "defer", "plan": plan.get("id"), "continuing": True}
        if owner_why:
            tasks_mod.log(layout, "diagnosis_defer_refused", "loop", task=task.id, role=role, level=level,
                          reason=owner_why[:300])
            action = "owner"
    jobs_mod.set_phase(layout, rec["attempt_id"], "DONE" if closed else rec["phase"], diag_action=action,
                       **({"diag_plan": plan.get("id")} if plan else {}))
    _annotate_issue(layout, issue_id, role=role, level=level, action=action, summary=summary)
    tasks_mod.log(layout, "diagnosis", "loop", task=task.id, role=role, level=level, action=action, summary=summary[:300])

    if action == "requeue":
        if count_requeues(layout, task.id) > DIAGNOSIS_BUDGET:
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""),
                                   f"diagnosis budget of {DIAGNOSIS_BUDGET} requeues spent")
        if task.held:
            # The owner's one explicit stop control, durable on the task itself (round-4 F9): a requeue this
            # diagnosis proposes is declined, not granted and left to be undone later, so nothing here resets
            # the retry signatures or grants a fresh continuation budget - the task stays held, with a note,
            # until the owner's own action (tasks.set_status with an owner actor) releases it. The record's own
            # diag_action is corrected to "held" (round-5 F12): a decline is not the "requeue" the manager
            # proposed, so it must not be counted by count_requeues or matched by already() as one.
            jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action="held")
            held_note = f" ({task.held['note']})" if task.held.get("note") else ""
            tasks_mod.add_note(layout, task.id,
                               f"{role} diagnosis: requeue declined — task is held by the owner{held_note}; "
                               "release the Hold to resume", actor="escalation")
            return {"action": "held", "held_by": task.held.get("by")}
        guidance = str(obj.get("guidance") or summary)[:4000]

        def fn(doc):
            doc.setdefault("guidance", []).append({"at": store.now_iso(), "by": role, "text": guidance})
        tasks_mod._update(layout, task.id, fn)
        # Explicit, replay-safe grant (R4): keyed by this diagnosis job's own attempt id, so a crash between this
        # write and the record's own DONE write (ingest() is re-run on the same rec/result) spends the GRANT only
        # once. grant_continuations() itself is called every time regardless (F2): it is generation-scoped, not
        # gated on `granted`, so a crash between the reset and the retirement, or partway through the retirement
        # loop, is finished on the next replay instead of leaving old markers counted forever against the new grant.
        escalation.reset_retries(layout, task.id, reason=f"requeued by {role} diagnosis", key=rec["attempt_id"])
        escalation.grant_continuations(layout, task.id, reason=f"requeued by {role} diagnosis")
        if task.status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            tasks_mod.set_status(layout, task.id, TaskStatus.READY, actor="escalation",
                                 note=f"{role} diagnosis: requeue — {summary[:200]}")
        else:
            tasks_mod.add_note(layout, task.id, f"{role} diagnosis: requeue — {summary[:200]}", actor="escalation")
        return {"action": "requeue", "guidance": guidance}
    if action == "defer":
        if task.held:
            # The owner's Hold stands, as for a requeue: the proposal is declined and written on the task, nothing
            # is created or released, and the record says "held" so it is not counted as a defer.
            jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action="held")
            tasks_mod.add_note(layout, task.id, f"{role} diagnosis: defer declined — task is held by the owner; it "
                               f"proposed waiting for: {blocked_on[:300]}; release the Hold to resume", actor="escalation")
            return {"action": "held", "held_by": task.held.get("by")}
        escalation.reset_retries(layout, task.id, reason=f"deferred by {role} diagnosis", key=plan["id"])
        escalation.grant_continuations(layout, task.id, reason=f"deferred by {role} diagnosis")
        tasks_mod.log(layout, "diagnosis_release", "escalation", task=task.id, role=role, plan=plan["id"])
        return {"action": "defer", "plan": plan["id"]}
    if action == "architect":
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: design question → architect. {summary[:200]}", actor="escalation")
        # Carry this diagnosis's OWN grant generation (F4): `already()`/`dispatch` dedupe an architect request by
        # (signature, level, grant), and omitting it here defaulted to 0, which let a generation-1 manager's
        # architect request match the spent generation-0 architect record and wrongly report the lane unavailable.
        nxt = dispatch(supervisor, task, rec.get("diag_source", "diagnosis"), rec.get("diag_detail", "") or summary,
                       rec.get("diag_signature", ""), level="architect", issue_id=issue_id, grant=rec.get("diag_grant") or 0)
        if nxt is None:                                        # no architect lane right now: the owner hears about it
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "architect lane unavailable")
        return {"action": "architect", "attempt": nxt.get("attempt_id")}
    if plan is not None and owner_why:
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: deferred, but {owner_why[:300]} — owner decision",
                           actor="escalation")
        return _route_plan_to_owner(layout, task, rec, role, plan, owner_why)
    if owner_why:
        # `_defer_plan` caught a proposer exception: there is no plan document at all (the crash happened before
        # `propose()`/`revise()` could write one), so `_route_plan_to_owner`'s two-version render has nothing to
        # show. The generic owner route still names the reason — never just "needs the owner" — so the operator
        # sees what crashed, not only that something did.
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: deferred, but {owner_why[:300]} — owner decision",
                           actor="escalation")
        return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), owner_why)
    tasks_mod.add_note(layout, task.id, f"{role} diagnosis: owner decision — {summary[:200]}", actor="escalation")
    return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "needs the owner")
