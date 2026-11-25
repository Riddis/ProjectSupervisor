"""Retries, failure signatures and escalation. A substantive failure — failed verification or a
REVISE/REJECT review — gets an automatic revision attempt with a failure brief quoting the evidence verbatim; the
second automatic revision runs on the role's senior model. The same normalized failure signature twice, a REJECT,
or the cause's own budget (or the total across causes) exhausted, stops retrying and opens an issue instead of
spending another attempt on a problem a human needs to see (R-14: `substance`, `infra`, `stall`, `total`, each a
separate counter so infrastructure failures cannot spend the budget a content review needs).

The decision is keyed by the attempt id that failed (`event`) and written before the revision is dispatched or the
issue opened, because the job record is only closed afterwards: a kill in between makes settle() re-collect the same
result, and the replay must not spend a second revision or stack a second issue on the owner."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import jobs as jobs_mod
from . import packets as packets_mod
from . import policy, routing_config, scheduler, store
from . import tasks as tasks_mod
from ._vendor.contract import atomic_json, read_json
from .locks import FileLock
from .models.task import ALLOWED, Task, TaskStatus
from .paths import Layout

ISSUE_TITLES = {"worker_blocked": "worker reported BLOCKED", "worker_failed": "worker run failed repeatedly",
                "stalled": "stopped with nothing routing it", "continuations_exhausted": "continuation budget exhausted"}

# R-14: one counter used to charge infrastructure failures and content revisions alike, so four
# infrastructure failures could spend the budget a content review needed. Three counters, by cause, plus
# a `total` across all of them; the limits and the cause table are data (config/routing.yaml
# escalation.budget), never constants, so a project can retune its own work class without a release.
DEFAULT_BUDGET_LIMITS = {"substance": 2, "infra": 4, "stall": 2, "total": 8}
# source (or "source:reason") -> counter. A source with no entry here, and no "source:reason" entry for
# the reason it was called with, charges "substance" — the conservative default, since an unclassified
# failure is about the work until shown otherwise.
DEFAULT_CAUSE_TABLE = {
    "verification": "substance",
    "verification:infra": "infra",
    "review": "substance",
    "worker_blocked": "substance",
    "worker_failed": "substance",
    "stalled": "stall",
    "continuations_exhausted": "infra",
}


def _budget_config(layout: Layout) -> tuple[dict[str, int], dict[str, str]]:
    """(limits, causes) with config/routing.yaml's `escalation.budget` layered over the defaults above —
    missing file, missing section or an unreadable document all fall back to the defaults, never an error."""
    limits, causes = dict(DEFAULT_BUDGET_LIMITS), dict(DEFAULT_CAUSE_TABLE)
    data = routing_config.load(layout)
    budget = ((data.get("escalation") or {}).get("budget")) or {}
    limits.update({str(k): int(v) for k, v in (budget.get("limits") or {}).items()})
    causes.update({str(k): str(v) for k, v in (budget.get("causes") or {}).items()})
    return limits, causes


def classify(layout: Layout, source: str, reason: str | None = None) -> str:
    """Which counter `source` (optionally qualified by `reason`, e.g. a verification report's own reason
    code) charges: the "source:reason" entry first, then plain "source", then "substance"."""
    _, causes = _budget_config(layout)
    if reason and f"{source}:{reason}" in causes:
        return causes[f"{source}:{reason}"]
    return causes.get(source, "substance")
# retry=False decisions never get a second try at the same brief; the note explains why in the failure's own terms
# instead of a stock worker_blocked sentence (this is also where continuations-exhausted lands).
NO_RETRY_NOTES = {"worker_blocked": "the worker reported it cannot proceed; retrying the same brief would repeat it",
                  "continuations_exhausted": "the continuation budget is spent; resubmitting the same brief would need another"}


def last_decision_at(layout: Layout, task_id: str) -> str:
    """ISO time of the most recent escalation decision for this task ('' when none, or recorded before decisions
    carried a time)."""
    return max((str(d.get("at") or "") for d in _load(layout, task_id)["events"].values()), default="")


def _mark_routed(layout: Layout, task_id: str, event: str | None) -> None:
    """Durable proof that `event`'s escalate decision finished routing it (a dispatched diagnosis, or an owner
    item once the budget is spent) - the fact `_repair_capped_escalation` (loop.py, round-3 F6) needs to stop
    recovering it, instead of inferring completion from the task's current status, which a later, unrelated
    hold or cancellation can also change."""
    if not event:
        return
    state = _load(layout, task_id)
    if event in state["events"] and not state["events"][event].get("routed"):
        state["events"][event]["routed"] = True
        _save(layout, task_id, state)


def event_outcome(layout: Layout, task_id: str, event: str) -> str:
    """Where `event`'s own escalation decision stands, for recovery: `""` when it never reached
    `record_failure`'s durable decision write at all, `"pending"` when the decision is recorded but `_apply`'s
    routing has not finished, `"routed"` once it has (`_mark_routed`). This tells "this event's own routing
    landed" apart from "it has not" - never derived from the task's current status, since BLOCKED is this
    decision's own intermediate state (issue opened, diagnosis still owed) as much as it is a status set for
    any other reason. It says nothing about an owner's deliberate Hold, which a BLOCKED task carries for an
    unrelated reason of its own: that is a separate, durable record on the task itself (`held`),
    read directly by the recovery guard instead of being inferred from this event's outcome."""
    dec = _load(layout, task_id)["events"].get(event)
    if not dec:
        return ""
    return "routed" if dec.get("routed") else "pending"


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
    legacy = state.pop("revisions", None)   # pre-2.8.0's single shared counter, replaced by "counters" below
    state.setdefault("signatures", [])
    if "counters" not in state and legacy is not None:
        # a task mid-flight on the old field already spent part of the one shared budget; the old field
        # always meant `substance` (the only cause the pre-2.8.0 code ever charged), so the split carries
        # that spend over instead of handing the task a fresh budget it was never granted.
        n = int(legacy)
        state["counters"] = {"substance": n, "infra": 0, "stall": 0, "total": n}
    else:
        state.setdefault("counters", {"substance": 0, "infra": 0, "stall": 0, "total": 0})
    state.setdefault("events", {})          # attempt_id -> the decision already taken for that failure
    return state


def _save(layout: Layout, task_id: str, state: dict) -> None:
    atomic_json(_state_path(layout, task_id), state)


def note_process_outcome(layout: Layout, task_id: str, attempt_id: str, retryable: bool) -> None:
    """A deterministic lane's (process/python/plugin) FAILED result, classified by `loop.record_outcome` while
    the worker's own report is still in hand — the stall sweep decides WHEN to act (its grace period is the
    back-off), this decides what it does once it does (R-12): a timeout, a lost process or an exit code the job's
    own `retry_on` names is worth one retry; any other reported exit would only repeat it, so the sweep is told to
    go straight to the exception route instead. Keyed by the delivery attempt, so a later attempt of the same
    task gets its own, independent classification."""
    state = _load(layout, task_id)
    state.setdefault("process_outcomes", {})[attempt_id] = bool(retryable)
    _save(layout, task_id, state)


def process_outcome_retryable(layout: Layout, task_id: str, attempt_id: str) -> bool:
    """Whether `attempt_id`'s deterministic failure (`note_process_outcome`) was transient. True — retry as
    before — for an attempt no one ever classified this way: a model-lane failure (handled on its own path), or
    one from before this fix shipped."""
    return bool(_load(layout, task_id).get("process_outcomes", {}).get(attempt_id, True))


def _next_issue_id(layout: Layout) -> str:
    n = 0
    for p in layout.issues.glob("I-*.yaml"):
        m = re.match(r"I-(\d+)$", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    return f"I-{n + 1:03d}"


def open_issue(layout: Layout, title: str, severity: str, owner_role: str, detail: str,
              resolution_task: str | None = None, signature: str | None = None, grant: int = 0) -> dict:
    """`grant` (`current_grant`) tags the issue with the continuation/retry grant generation it was opened under
    (R4): `find_issue` only reuses an issue from the SAME generation, so a fresh grant's own exhaustion never
    reuses one a spent grant already opened."""
    layout.issues.mkdir(parents=True, exist_ok=True)
    with FileLock(layout.state / ".issues.lock", owner="escalation", timeout=5.0):
        iid = _next_issue_id(layout)
        doc = {"id": iid, "title": title, "status": "open", "severity": severity, "owner_role": owner_role,
               "detail": detail, "resolution_task": resolution_task, "signature": signature, "grant": grant,
               "opened": store.now_iso()}
        store.write_new(layout.issues / f"{iid}.yaml", doc)
    tasks_mod.log(layout, "issue_opened", "escalation", id=iid, title=title)
    return doc


_ISSUE_ID_RE = re.compile(r"^I-\d+$")                 # the exact shape _next_issue_id generates, nothing wider


def resolve_issue(layout: Layout, issue_id: str, resolution: str, resolving_task: str | None, actor: str) -> dict:
    """The only way state/issues/ is closed: the `issue.resolve` command (supervisor.commands), queued like every
    other command. Idempotent: an already-resolved issue is a no-op, not a second write. Runs from `open` or from
    `recovered` (R-21: a task completing marks an issue `recovered`, never `resolved` — that stays this function's
    own, explicit decision) alike; only a terminal `resolved` short-circuits.

    `issue_id` is attacker-reachable text (R3): rejecting the format up front (`I-<digits>`, nothing else) already
    refuses every separator, `..` traversal and absolute id, because none of those match it. The containment check
    is kept anyway as a second, independent layer, and the loaded record's own `id`/`severity`/`status` fields are
    checked before any write so a contained but wrong-shaped record (not an issue at all) cannot be mistaken for
    one — all three checks run before the file is even opened for update."""
    if not isinstance(issue_id, str) or not _ISSUE_ID_RE.match(issue_id):
        raise ValueError(f"invalid issue id {issue_id!r}")
    p = (layout.issues / f"{issue_id}.yaml").resolve()
    if p.parent != layout.issues.resolve():
        raise ValueError(f"invalid issue id {issue_id!r}")
    if not p.exists():
        raise ValueError(f"no such issue {issue_id!r}")
    doc = store.read(p)
    if doc.get("id") != issue_id or "severity" not in doc or doc.get("status") not in ("open", "recovered", "resolved"):
        raise ValueError(f"not an issue record: {issue_id!r}")
    if doc.get("status") == "resolved":
        return {"skipped": "already resolved", "id": issue_id}
    def fn(doc):
        doc.update(status="resolved", resolution=resolution, resolved_by=actor, resolved_by_task=resolving_task,
                   resolved_at=store.now_iso())
        return doc
    store.update(p, fn, lock_path=layout.state / ".issues.lock")
    tasks_mod.log(layout, "issue_resolved", actor, id=issue_id, resolution=resolution[:200], task=resolving_task)
    return {"id": issue_id, "status": "resolved"}


def recover_issues_for_task(layout: Layout, task_id: str, attempt_id: str | None, digest: str) -> None:
    """A task reaching COMPLETE (`tasks.advance`) recovers every OPEN issue that names it as `resolution_task`
    (R-21): marked `recovered`, with the attempt and digest that did it, instead of sitting open forever or being
    silently closed as `resolved` — that stays a separate, explicit decision (`issue.resolve`), since the owner
    may still want to see what caused it. A `recovered` issue is excluded from every "open" count the same way a
    `resolved` one already is (render.state_lines, render.render_issues)."""
    for p in sorted(layout.issues.glob("I-*.yaml")):
        try:
            doc = store.read(p)
        except (OSError, ValueError):
            continue
        if doc.get("resolution_task") != task_id or doc.get("status") != "open":
            continue
        def fn(d, attempt_id=attempt_id, digest=digest):
            d.update(status="recovered", recovered_attempt=attempt_id, recovered_digest=digest,
                     recovered_at=store.now_iso())
            return d
        store.update(p, fn, lock_path=layout.state / ".issues.lock")
        tasks_mod.log(layout, "issue_recovered", "escalation", id=doc.get("id"), task=task_id, attempt=attempt_id)


def find_issue(layout: Layout, task_id: str, signature: str, grant: int = 0) -> dict | None:
    """An issue already opened for this task and failure signature under the SAME grant generation (`current_grant`,
    R4) — so replaying an interrupted escalation (or the same signature arriving twice within one grant) cannot
    stack duplicate issues on the owner, but the normalized signature of a repeat "continuations_exhausted" failure
    (identical before and after a fresh grant, strictly by text) does not reuse an issue the SPENT grant already
    opened — that would dedupe every later exhaustion away and the sequence could never reach its own diagnosis or
    the owner."""
    for p in sorted(layout.issues.glob("I-*.yaml")):
        try:
            doc = store.read(p)
        except (OSError, ValueError):
            continue
        if (doc.get("resolution_task") == task_id and doc.get("signature") == signature
                and doc.get("status") == "open" and int(doc.get("grant") or 0) == grant):
            return doc
    return None


def refuse_packet(supervisor, task: Task, where: str, exc: policy.PolicyViolation) -> dict:
    """A `PolicyViolation` raised while building ONE task's own packet (a revision, review or diagnosis brief, or
    a continuation's rebuild of one) is this task's own failure, not the loop's: caught here, at the dispatch
    boundary that raised it, so the packet is never written or dispatched and the exception never reaches
    `settle()`/`dispatch_reviews()` - nothing about this one task's refusal stops the loop from serving every
    other one this turn or the next.

    A `scope="machine"` violation (the credential store could not be enumerated; a required screen plugin would
    not load - see `policy.PolicyViolation`) is re-raised unchanged instead. It is not this task's refusal: it
    refuses every packet on the machine, so containing it here would BLOCK each task in turn, spend the owner's
    inbox on identical items and leave the misconfiguration standing. Still fail-closed - nothing dispatches
    either way - but the loop's own error path, not the task list, is where a machine-wide stop belongs.

    The task leaves the path it was on, for the owner, rather than being retried: a revision, review or
    diagnosis session would rebuild the identical packet from the identical source text and hit the identical
    refusal, so retrying is not a recovery here, it is the replay that used to re-raise every loop turn until the
    loop-error breaker tripped. BLOCKED, whenever the task's current status allows that transition at all
    (`models.task.ALLOWED`) - the same "automatic retries stopped" status every other escalate branch uses, and
    the transition that actually contains this: the status is what the schedulers read, so a refused REVIEW
    packet left in WAITING_FOR_REVIEW is re-offered by `review.pending()` on the very next turn and refuses
    again, which is the repeat this function exists to end. A task already BLOCKED, COMPLETE or CANCELLED keeps
    its status and gets the note alone.

    The owner is told once per stopped task: with an owner `decision` item already open for this task there is
    nothing a second card adds (the answer to the first one - REQUEUE/CANCEL/DEFER - is the answer to both), and
    `inbox.MAX_OPEN_PER_ROLE` is a shared budget, so one task's refusals must not spend the slots every other
    task's own decisions and approvals need.

    The note, the log and the owner item carry `exc`'s own message only - `PolicyViolation` never carries the
    matched text (policy.py's own floor rule), only the pattern name or label that tripped - never `source` or
    `detail`, which is the text that tripped the scanner and must not be the text that un-trips it by reaching a
    stored note, an issue or an inbox item verbatim."""
    if getattr(exc, "scope", "task") != "task":
        raise exc
    from . import diagnosis as diagnosis_mod
    from . import inbox as inbox_mod
    layout = supervisor.layout
    msg = str(exc)[:500]
    tasks_mod.log(layout, "packet_refused", "escalation", task=task.id, where=where, error=msg)
    status = tasks_mod.load(layout, task.id).status
    if TaskStatus.BLOCKED in ALLOWED.get(status, set()):
        tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=f"{where} refused: {msg}")
    else:
        tasks_mod.add_note(layout, task.id, f"{where} refused: {msg}", actor="escalation")
    open_decision = next((i for i in inbox_mod.open_items(layout, "owner")
                          if i.get("task_id") == task.id and i.get("kind") == inbox_mod.DECISION_KIND), None)
    if open_decision:
        return {"action": "refused", "inbox": open_decision["id"]}
    out = diagnosis_mod.route_to_owner(layout, task, {"diag_role": "escalation"}, msg, "",
                                       f"the {where} could not be built without refusing: {msg}")
    out["action"] = "refused"
    return out


def dispatch_revision(supervisor, task: Task, source: str, detail: str, senior: bool, event: str | None = None) -> dict:
    """Queue the next delivery attempt with a revision brief; `_ensure_in_progress` (inside launch()) handles the
    FAILED/IN_PROGRESS transition, so no status change is needed here. The record goes through supervisor.dispatch →
    launch(), i.e. the same gate (decision, capacity, limits, ownership) as a scheduled job: under PAUSE/DRAIN it
    simply waits as PREPARED.

    The lane and model come from `scheduler.resolve_lane` (R-16): a role routed to another lane used to lose
    that routing here, because the lookup ran with no layout and silently fell back to the cost-lane default,
    so only the role's first, scheduled delivery ever reached its own lane. The resolver also names why, so a
    senior-model revision and the plain return to the base model both leave a reason on the job record instead
    of a model change nobody can trace."""
    layout = supervisor.layout
    open_attempts = [r["attempt_id"] for r in jobs_mod.open_records(layout)
                     if r["task_id"] == task.id and (r.get("spec") or {}).get("kind", "delivery") == "delivery"
                     and r["attempt_id"] != event]          # the failing delivery itself is still open at this point
    if open_attempts:                                 # one revision at a time: two REVISE verdicts on one digest must
        tasks_mod.log(layout, "revision_skipped", "escalation", task=task.id, reason=f"attempt(s) already open: {open_attempts}",
                      source=source)                  # not spawn two editors of the same files
        return {"skipped": "attempt already open", "open": open_attempts}
    role = scheduler.job_spec(task).get("role") or task.owner_role
    resolved = scheduler.resolve_lane(layout, task, role=role, revision=True, senior=senior)
    adapter_name = resolved.adapter
    job = dict(scheduler.job_spec(task))
    overrides = {k: v for k, v in (("model", resolved.model), ("effort", resolved.effort)) if v}
    if adapter_name in scheduler.MODEL_LANES:
        prior_brief = job.get("prompt") or packets_mod.delivery_brief(layout, task)
        job = {**job, **overrides, "prompt": packets_mod.revision_brief(task, prior_brief, source, detail)}
    doc = task.model_dump(mode="json")
    doc["job"] = job
    revision_task = Task.model_validate(doc)
    note = f"revision after {source} failure" + (" (senior model)" if senior else "")
    extra = {"same_vendor": resolved.same_vendor} if resolved.same_vendor else None
    rec = supervisor.dispatch(revision_task, adapter_name, note=note, reason=resolved.reason, extra=extra, cause="revision")
    dep = tasks_mod.unmet_dependency(layout, task)
    if dep and rec.get("attempt_id"):
        # G6, 2.4.0: the record is queued either way (crash-safe, same spirit as a continuation's own note) - the
        # gate refuses it at launch time; this note says why, at queue time, so the owner is not left guessing.
        tasks_mod.add_note(layout, task.id, f"{rec['attempt_id']}: revision queued; waiting on {dep}", actor="escalation")
    return rec


def _apply(supervisor, task: Task, source: str, detail: str, decision: dict) -> dict:
    """Carry out a decision. Idempotent on purpose: the revision dispatch is guarded by the caller and the issue is
    found-or-created by (task, signature), so replaying an interrupted escalation changes nothing.

    The escalate branch's three side effects (the issue, the IN_PROGRESS->BLOCKED transition, the routing that
    follows) are each finished on their own terms, not only when the issue is freshly opened (round-3 F6): a
    crash between the issue write and the status write leaves the issue found-not-created on replay, and the
    task must still leave IN_PROGRESS then, not stay stranded behind an issue that already exists. `_mark_routed`
    closes the loop for `_repair_capped_escalation`, whose recovery guard must tell "this event's own routing
    already landed" apart from "it has not" - that distinction cannot be read off the task's current status
    alone (BLOCKED is this event's own intermediate state as much as it is a stop for any other reason). A
    deliberate later cancellation is told apart because CANCELLED is outside the guard's allowed statuses; a
    deliberate later Hold is told apart by its own durable record on the task (`held`),
    checked directly rather than inferred from this event's `routed` flag, which carries no such information."""
    layout = supervisor.layout
    sig = decision["signature"]
    if decision["action"] == "retry":
        try:
            dispatch_revision(supervisor, task, source, detail, bool(decision.get("senior")), event=decision.get("event"))
        except policy.PolicyViolation as exc:
            # The revision packet (prior delivery brief + this failure's own `detail`, quoted verbatim by
            # packets.revision_brief) refused: never raise out of here into settle()/record_failure() - see
            # refuse_packet's own docstring for why this ends BLOCKED-for-owner rather than retried.
            out = refuse_packet(supervisor, task, "revision packet", exc)
            _mark_routed(layout, task.id, decision.get("event"))
            return out
        return {"action": "retry", "revision": decision["revision"], "senior": decision["senior"], "signature": sig}
    grant = current_grant(layout, task.id)
    issue = find_issue(layout, task.id, sig, grant=grant)
    created = issue is None
    note = decision.get("note", "")
    if created:
        issue = open_issue(layout, title=f"{task.id}: {ISSUE_TITLES.get(source, f'repeated {source} failure')}", severity="high",
                          owner_role=task.owner_role, signature=sig,
                          detail=f"{detail}\n\n{note}. Automatic retries stopped; diagnosis requested.",
                          resolution_task=task.id, grant=grant)
    if task.status == TaskStatus.IN_PROGRESS:
        tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=f"{note} ({issue['id']})")
    elif created:
        tasks_mod.add_note(layout, task.id, f"{note} ({issue['id']}); automatic retries stopped", actor="escalation")
    # The exception route: one bounded diagnosis by the manager role, instead of handing every stopped
    # retry chain to the owner. Idempotent per (task, signature, level); the diagnosis itself decides whether the
    # owner needs to hear about it. After DIAGNOSIS_BUDGET requeues the owner decides directly.
    from . import diagnosis as diagnosis_mod
    out = {"action": "escalate", "issue": issue["id"], "signature": sig}
    # R-14, trigger 2: a review escalation that happens because `substance` is spent is not a worker or a
    # process to diagnose — review rejections and their fixes belong to the review lane, and a diagnosis
    # here would only decide what the review already said. Straight to the owner, as an issue already
    # visible on the Issues page, never through a diagnosis session.
    if source == "review" and decision.get("trigger") == "substance_exhausted":
        # no diagnosis runs on this route (that is the whole point), so the owner text must not claim
        # one did and failed — "review", not "diagnosis", names what actually could not resolve it.
        rec = {"diag_role": "review", "diag_level": "manager"}
        out["owner"] = diagnosis_mod.route_to_owner(layout, task, rec, decision.get("note", ""), "",
                                                     "the review budget is spent; another round would only repeat "
                                                     "what the review already found")
    elif diagnosis_mod.count_requeues(layout, task.id) >= diagnosis_mod.DIAGNOSIS_BUDGET:
        rec = {"diag_role": "diagnosis", "diag_level": "manager"}
        out["owner"] = diagnosis_mod.route_to_owner(layout, task, rec, decision.get("note", ""), "",
                                                     f"diagnosis budget of {diagnosis_mod.DIAGNOSIS_BUDGET} requeues spent")
    else:
        rec = diagnosis_mod.dispatch(supervisor, task, source, detail, sig, level="manager", issue_id=issue["id"], grant=grant,
                                     event=decision.get("event"))
        if rec:
            out["diagnosis"] = rec.get("attempt_id")
    _mark_routed(layout, task.id, decision.get("event"))
    return out


def reset_retries(layout: Layout, task_id: str, reason: str = "", key: str | None = None,
                  reset: list[str] | None = None) -> bool:
    """A diagnosis or an owner REQUEUE granted a fresh start: forget the failure signatures and the spent
    counters named in `reset` (default: all of `substance`, `infra`, `stall`, `total` — a fresh start
    unless the caller means something narrower). The decisions already taken stay, so replays remain
    idempotent; count the grant. With `key` (the inbox item of an owner decision) the grant is made once:
    a replay of the same decision returns False and changes nothing."""
    state = _load(layout, task_id)
    grants = state.setdefault("reset_log", [])
    if key and any(e.get("key") == key for e in grants if isinstance(e, dict)):
        return False
    counters = list(reset) if reset else list(DEFAULT_BUDGET_LIMITS)
    state["signatures"] = []
    for name in counters:
        state["counters"][name] = 0
    state["resets"] = state.get("resets", 0) + 1
    grants.append({"at": store.now_iso(), "reason": reason, "reset": counters, **({"key": key} if key else {})})
    _save(layout, task_id, state)
    return True


def current_grant(layout: Layout, task_id: str) -> int:
    """The task's current continuation/retry grant generation: 0 until the first reset_retries() grant, then the
    number of genuine grants made so far (`state["resets"]`, itself already replay-safe). An issue or diagnosis
    tagged with an earlier generation belongs to a spent grant and must never dedupe a later one (R4: `find_issue`,
    `diagnosis.already`)."""
    return int(_load(layout, task_id).get("resets") or 0)


def grant_continuations(layout: Layout, task_id: str, reason: str) -> None:
    """A fresh continuation grant retires every continuation marker that belongs to an earlier grant
    generation than this task's current one, the same way an owner REQUEUE resume has always retired them
    (supervisor.inbox). `current_grant` (`state["resets"]`) is the retirement boundary and it is durable in the
    same write `reset_retries` already made, so this function reads it fresh every call instead of trusting a
    caller's "was this a genuinely new grant" flag: calling it unconditionally, every time `reset_retries` runs
    (fresh grant or replay alike), finishes whatever an earlier crash left only partly retired without ever
    touching a marker the CURRENT generation already created (F2) — each marker is tagged with its own
    `continuation_grant` at dispatch (supervisor.loop.Supervisor.requeue/repair_continuations), so "belongs to
    an earlier generation" never depends on when this function happens to run."""
    grant = current_grant(layout, task_id)
    for r in jobs_mod.for_task(layout, task_id):
        if r.get("cause") == "continuation" and int(r.get("continuation_grant") or 0) < grant:
            prior = str(r.get("deferred_reason", ""))
            jobs_mod.set_phase(layout, r["attempt_id"], r["phase"], deferred_reason=f"reset ({reason}): {prior}")


def _newer_delivery_exists(layout: Layout, task_id: str, event: str) -> bool:
    """Whether a delivery session of a later attempt than `event`'s own already exists — the revision was
    dispatched before a crash replayed this decision. The `attempt` field of `event`'s own job record and of
    each candidate, fields only (§6/§8.6: no id-suffix fallback); a record with no `attempt` at all (`event`
    itself unresolvable, or a delivery dispatched before this field existed) reads as attempt 0, never a
    comparison between two `None`s."""
    event_attempt = jobs_mod.attempt_of(jobs_mod.resolve(layout, event) or {"attempt_id": event}) or 0
    return any((jobs_mod.attempt_of(r) or 0) > event_attempt for r in jobs_mod.for_task(layout, task_id)
              if jobs_mod.kind_of(r) == "delivery")


def _replay(supervisor, task: Task, state: dict, event: str, source: str, detail: str) -> dict:
    """This failure was already decided (the job record is only closed after record_failure, so an interruption
    between the two replays it). Never spend a second revision or open a second issue; only finish the side effect
    the first pass may not have reached."""
    prior = dict(state["events"][event])
    if prior.get("action") == "retry" and _newer_delivery_exists(supervisor.layout, task.id, event):
        return {k: v for k, v in prior.items() if k != "note"}      # the revision was dispatched before the crash
    tasks_mod.log(supervisor.layout, "escalation_replayed", "escalation", task=task.id, attempt=event,
                  action=prior.get("action"))
    return _apply(supervisor, task, source, detail, prior)


def record_failure(supervisor, task_id: str, source: str, detail: str, event: str | None = None,
                   retry: bool = True, reason: str | None = None, immediate: bool = False) -> dict:
    """A failure that must not dead-end: failed verification, a REVISE/REJECT review, a worker that ended BLOCKED or
    FAILED instead of COMPLETE, or a task the stall sweep found stopped with nothing routing it. `event` is the
    attempt id (or stall key) that failed and makes the decision idempotent across an interruption. `retry=False`
    skips the blind revision and goes straight to the exception route — used when the worker itself reported
    BLOCKED: re-sending the same brief into the same environment reproduces the same block.

    R-14: `reason` is the extra classifier a caller may already have in hand (today, a verification
    report's own reason code distinguishing a verifier-condition failure from one on the work) that
    `classify()` uses, alongside `source`, to pick which of the three counters this failure charges.
    `immediate` is trigger 4 of the review-rounds rule: a REJECT escalates at once,
    regardless of any counter — a rejection is a verdict on the approach, not a list of repairs.

    Returns {"action": "retry"|"escalate", ...}."""
    layout = supervisor.layout
    task = tasks_mod.load(layout, task_id)
    state = _load(layout, task_id)
    if event and event in state["events"]:
        return _replay(supervisor, task, state, event, source, detail)
    sig = signature(f"{source}:{detail}")
    repeat = sig in state["signatures"]
    state["signatures"].append(sig)
    cause = classify(layout, source, reason)
    counters = state["counters"]
    counters[cause] = counters.get(cause, 0) + 1
    counters["total"] = counters.get("total", 0) + 1
    limits, _ = _budget_config(layout)
    cause_limit = limits.get(cause, DEFAULT_BUDGET_LIMITS.get(cause, 2))
    total_limit = limits.get("total", DEFAULT_BUDGET_LIMITS["total"])
    if not retry:
        decision = {"action": "escalate", "signature": sig, "cause": cause, "trigger": "no_retry",
                    "note": f"{source}: {NO_RETRY_NOTES.get(source, 'retrying the same brief would repeat it')}"}
    elif immediate:
        decision = {"action": "escalate", "signature": sig, "cause": cause, "trigger": "reject",
                    "note": f"{source}: rejected outright; a rejection is a verdict on the approach, "
                            f"not a list of repairs"}
    elif repeat:
        decision = {"action": "escalate", "signature": sig, "cause": cause, "trigger": "repeat_signature",
                    "note": f"{source} failed again with the same signature"}
    elif counters[cause] > cause_limit:
        decision = {"action": "escalate", "signature": sig, "cause": cause,
                    "trigger": "substance_exhausted" if cause == "substance" else "cause_exhausted",
                    "note": f"{source} failed after {counters[cause]} {cause} failures (budget {cause_limit})"}
    elif counters["total"] > total_limit:
        decision = {"action": "escalate", "signature": sig, "cause": cause, "trigger": "total_exhausted",
                    "note": f"{source} failed after {counters['total']} total failures (budget {total_limit})"}
    else:
        decision = {"action": "retry", "revision": counters[cause], "signature": sig, "cause": cause,
                    "senior": counters[cause] >= cause_limit}      # the second automatic revision of this cause
    decision["at"] = store.now_iso()                  # lets the stall sweep tell "escalated after this stop" apart
    if event:
        decision["event"] = event
        state["events"][event] = decision         # durable BEFORE the side effect, so a crash replays the decision
    _save(layout, task_id, state)
    return _apply(supervisor, task, source, detail, decision)
