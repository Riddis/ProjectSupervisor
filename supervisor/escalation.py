"""Retries, failure signatures and escalation. A substantive failure — failed verification or a
REVISE/REJECT review — gets an automatic revision attempt with a failure brief quoting the evidence verbatim; the
second automatic revision runs on the role's senior model. The same normalized failure signature twice, or the
two-revision budget exhausted, stops retrying and opens an issue instead of spending another attempt on a problem
a human needs to see.

The decision is keyed by the attempt id that failed (`event`) and written before the revision is dispatched or the
issue opened, because the job record is only closed afterwards: a kill in between makes settle() re-collect the same
result, and the replay must not spend a second revision or stack a second issue on the owner."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import jobs as jobs_mod
from . import packets as packets_mod
from . import scheduler
from . import store
from . import tasks as tasks_mod
from ._vendor.contract import atomic_json, read_json
from .locks import FileLock
from .models.task import Task, TaskStatus
from .paths import Layout

REVISION_BUDGET = 2


def normalize(text: str) -> str:
    """Strip hashes/digests and numbers so two reports of the same underlying problem collapse to one signature."""
    norm = re.sub(r"[0-9a-f]{8,}", "<hash>", text or "", flags=re.IGNORECASE)
    norm = re.sub(r"\d+", "<n>", norm)
    return re.sub(r"\s+", " ", norm).strip().lower()


def signature(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def _state_path(layout: Layout, task_id: str) -> Path:
    return layout.runtime / "escalation" / f"{task_id}.json"


def _load(layout: Layout, task_id: str) -> dict:
    p = _state_path(layout, task_id)
    state = read_json(p) if p.exists() else {}
    state.setdefault("revisions", 0)
    state.setdefault("signatures", [])
    state.setdefault("events", {})          # attempt_id -> the decision already taken for that failure
    return state


def _save(layout: Layout, task_id: str, state: dict) -> None:
    atomic_json(_state_path(layout, task_id), state)


def _next_issue_id(layout: Layout) -> str:
    n = 0
    for p in layout.issues.glob("I-*.yaml"):
        m = re.match(r"I-(\d+)$", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    return f"I-{n + 1:03d}"


def open_issue(layout: Layout, title: str, severity: str, owner_role: str, detail: str,
              resolution_task: str | None = None, signature: str | None = None) -> dict:
    layout.issues.mkdir(parents=True, exist_ok=True)
    with FileLock(layout.state / ".issues.lock", owner="escalation", timeout=5.0):
        iid = _next_issue_id(layout)
        doc = {"id": iid, "title": title, "status": "open", "severity": severity, "owner_role": owner_role,
               "detail": detail, "resolution_task": resolution_task, "signature": signature,
               "opened": store.now_iso()}
        store.write_new(layout.issues / f"{iid}.yaml", doc)
    tasks_mod.log(layout, "issue_opened", "escalation", id=iid, title=title)
    return doc


def find_issue(layout: Layout, task_id: str, signature: str) -> dict | None:
    """An issue already opened for this task and failure signature — so replaying an interrupted escalation (or the
    same signature arriving twice) cannot stack duplicate issues on the owner."""
    for p in sorted(layout.issues.glob("I-*.yaml")):
        try:
            doc = store.read(p)
        except (OSError, ValueError):
            continue
        if doc.get("resolution_task") == task_id and doc.get("signature") == signature and doc.get("status") == "open":
            return doc
    return None


def dispatch_revision(supervisor, task: Task, source: str, detail: str, senior: bool, event: str | None = None) -> dict:
    """Queue the next delivery attempt with a revision brief; `_ensure_in_progress` (inside launch()) handles the
    FAILED/IN_PROGRESS transition, so no status change is needed here. The record goes through supervisor.dispatch →
    launch(), i.e. the same gate (decision, capacity, limits, ownership) as a scheduled job: under PAUSE/DRAIN it
    simply waits as PREPARED."""
    layout = supervisor.layout
    open_attempts = [r["attempt_id"] for r in jobs_mod.open_records(layout)
                     if r["task_id"] == task.id and (r.get("spec") or {}).get("kind", "delivery") == "delivery"
                     and r["attempt_id"] != event]          # the failing delivery itself is still open at this point
    if open_attempts:                                 # one revision at a time: two REVISE verdicts on one digest must
        tasks_mod.log(layout, "revision_skipped", "escalation", task=task.id, reason=f"attempt(s) already open: {open_attempts}",
                      source=source)                  # not spawn two editors of the same files
        return {"skipped": "attempt already open", "open": open_attempts}
    adapter_name = scheduler.adapter_name(task)
    job = dict(scheduler.job_spec(task))
    role = job.get("role") or task.owner_role
    overrides = {}
    if senior:
        from .adapters.claude_cli import role_profile
        senior_model = role_profile(layout, role).get("senior_model")
        if senior_model:
            overrides["model"] = senior_model
    if adapter_name in ("claude_cli", "codex_cli", "claude_api"):
        prior_brief = job.get("prompt") or packets_mod.delivery_brief(layout, task)
        job = {**job, **overrides, "prompt": packets_mod.revision_brief(task, prior_brief, source, detail)}
    doc = task.model_dump(mode="json")
    doc["job"] = job
    revision_task = Task.model_validate(doc)
    note = f"revision after {source} failure" + (" (senior model)" if senior else "")
    return supervisor.dispatch(revision_task, adapter_name, note=note)


def _attempt_no(attempt_id: str) -> int:
    try:
        return int(str(attempt_id).rsplit(".a", 1)[1])
    except (IndexError, ValueError):
        return 0


def _apply(supervisor, task: Task, source: str, detail: str, decision: dict) -> dict:
    """Carry out a decision. Idempotent on purpose: the revision dispatch is guarded by the caller and the issue is
    found-or-created by (task, signature), so replaying an interrupted escalation changes nothing."""
    layout = supervisor.layout
    sig = decision["signature"]
    if decision["action"] == "retry":
        dispatch_revision(supervisor, task, source, detail, bool(decision.get("senior")), event=decision.get("event"))
        return {"action": "retry", "revision": decision["revision"], "senior": decision["senior"], "signature": sig}
    issue = find_issue(layout, task.id, sig)
    if issue is None:
        note = decision.get("note", "")
        issue = open_issue(layout, title=f"{task.id}: repeated {source} failure", severity="high",
                          owner_role=task.owner_role, signature=sig,
                          detail=f"{detail}\n\n{note}. Automatic retries stopped; diagnosis requested.",
                          resolution_task=task.id)
        if task.status == TaskStatus.IN_PROGRESS:
            tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=f"{note} ({issue['id']})")
        else:
            tasks_mod.add_note(layout, task.id, f"{note} ({issue['id']}); automatic retries stopped", actor="escalation")
    # The exception route: one bounded diagnosis by the manager role, instead of handing every stopped
    # retry chain to the owner. Idempotent per (task, signature, level); the diagnosis itself decides whether the
    # owner needs to hear about it. After DIAGNOSIS_BUDGET requeues the owner decides directly.
    from . import diagnosis as diagnosis_mod
    out = {"action": "escalate", "issue": issue["id"], "signature": sig}
    if diagnosis_mod.count_requeues(layout, task.id) >= diagnosis_mod.DIAGNOSIS_BUDGET:
        rec = {"diag_role": "diagnosis", "diag_level": "manager"}
        out["owner"] = diagnosis_mod.route_to_owner(layout, task, rec, decision.get("note", ""), "",
                                                     f"diagnosis budget of {diagnosis_mod.DIAGNOSIS_BUDGET} requeues spent")
        return out
    rec = diagnosis_mod.dispatch(supervisor, task, source, detail, sig, level="manager", issue_id=issue["id"])
    if rec:
        out["diagnosis"] = rec.get("attempt_id")
    return out


def reset_retries(layout: Layout, task_id: str, reason: str = "") -> None:
    """A diagnosis granted a fresh start: forget the failure signatures and the spent revisions (the decisions already
    taken stay, so replays remain idempotent) and count the grant."""
    state = _load(layout, task_id)
    state["signatures"] = []
    state["revisions"] = 0
    state["resets"] = state.get("resets", 0) + 1
    state.setdefault("reset_log", []).append({"at": store.now_iso(), "reason": reason})
    _save(layout, task_id, state)


def _replay(supervisor, task: Task, state: dict, event: str, source: str, detail: str) -> dict:
    """This failure was already decided (the job record is only closed after record_failure, so an interruption
    between the two replays it). Never spend a second revision or open a second issue; only finish the side effect
    the first pass may not have reached."""
    prior = dict(state["events"][event])
    if prior.get("action") == "retry" and any(_attempt_no(r["attempt_id"]) > _attempt_no(event)
                                              for r in jobs_mod.for_task(supervisor.layout, task.id)
                                              if (r.get("spec") or {}).get("kind", "delivery") == "delivery"):
        return {k: v for k, v in prior.items() if k != "note"}      # the revision was dispatched before the crash
    tasks_mod.log(supervisor.layout, "escalation_replayed", "escalation", task=task.id, attempt=event,
                  action=prior.get("action"))
    return _apply(supervisor, task, source, detail, prior)


def record_failure(supervisor, task_id: str, source: str, detail: str, event: str | None = None) -> dict:
    """A substantive failure (verification, or a REVISE/REJECT review). `event` is the attempt id that failed and
    makes the decision idempotent across an interruption. Returns {"action": "retry"|"escalate", ...}."""
    layout = supervisor.layout
    task = tasks_mod.load(layout, task_id)
    state = _load(layout, task_id)
    if event and event in state["events"]:
        return _replay(supervisor, task, state, event, source, detail)
    sig = signature(f"{source}:{detail}")
    repeat = sig in state["signatures"]
    state["signatures"].append(sig)
    state["revisions"] += 1
    if repeat or state["revisions"] > REVISION_BUDGET:
        decision = {"action": "escalate", "signature": sig,
                    "note": (f"{source} failed again with the same signature" if repeat else
                             f"{source} failed after {state['revisions']} revisions (budget {REVISION_BUDGET})")}
    else:
        decision = {"action": "retry", "revision": state["revisions"], "signature": sig,
                    "senior": state["revisions"] >= REVISION_BUDGET}      # the second automatic revision
    if event:
        decision["event"] = event
        state["events"][event] = decision         # durable BEFORE the side effect, so a crash replays the decision
    _save(layout, task_id, state)
    return _apply(supervisor, task, source, detail, decision)
