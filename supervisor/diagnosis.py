"""The exception route: manager → architect → owner.

When supervisor.escalation stops retrying (same failure signature twice, or the revision budget spent) it used to open an
issue saying "owner decision needed" and stop. Now it also dispatches ONE bounded diagnosis job to the manager role
(`senior_pm` by default) on a read-only lane, with the failure evidence frozen in the brief. The diagnosis returns a
structured decision:

    requeue   — the task goes back to READY with written guidance that the next delivery brief carries; the retry
                budget is reset once (DIAGNOSIS_BUDGET diagnoses per task, then straight to the owner);
    defer     — the task waits for other work, named structurally: a `new_task` contract the supervisor creates
                (validated like `supervisor task create`) and/or `wait_for` ids of existing unfinished tasks. They
                join the task's `depends_on` and the task goes back to READY, so it runs once they complete. A defer
                that names nothing, or a proposal that is refused, goes to the owner instead: a task parked on prose
                alone is never released by anything (the stall this replaced);
    architect — the question is design, or the fix is a change to the task's own contract: one more diagnosis by the
                architect role (never a third level). The manager's `summary` and `guidance` (what is wrong with
                the contract, and why) are handed to the architect's brief;
    revise    — architect level only: the architect changes the task's contract itself. `changes` is a structured
                patch ({path, value} pairs such as `job.argv.3`) and `reason` says why. revision_plan() applies it to
                a copy of the stored task and checks the result the way `task create` / `task import --dry-run`
                check a contract (tasks.check_contract; its contract warnings are refusals, as for a defer's
                `new_task`); _revise() writes it through commands.apply_job (the job) and tasks.revise_contract
                (the other fields, plus a `revisions` record), notes old value, new value and reason on the task
                (`by: architect`), resets the retry budget once and sets the task READY;
    owner     — a product choice or an action only the owner can perform: one inbox item, the only way this route
                reaches the owner's attention.

A revise is the owner's call instead, through one decision item, when it is refused or invalid, when it touches a
field outside REVISABLE_FIELDS (owner_role, reviewer, human_approval, phase, cost_category, title, objective, ...) or
the job's lane (JOB_LANE_KEYS: what it runs on and costs), when it drops or alters an existing entry of a
verification field (ADDITIVE_FIELDS: artifacts, tests, checks, acceptance and depends_on may only grow), when this
task's contract was already revised for the same failure signature (so the revision did not fix it), after
DIAGNOSIS_BUDGET revisions of the task, or when `routing.yaml escalation.revise` is false. The seed file is never
edited: `supervisor check` warns about every open task whose stored contract differs from the seed
(tasks.seed_drift), naming the revision that caused it.

A failed or malformed diagnosis never loops: it falls through to `owner`. One diagnosis per (task, signature, level),
checked against the job records, so a replayed escalation cannot spend a second one. The job goes through
Supervisor.dispatch → launch → supervisor.gate like every other job (decision, capacity floor, agent limit)."""
from __future__ import annotations

import copy
import json

import yaml
from pydantic import ValidationError

from . import inbox as inbox_mod, jobs as jobs_mod, store, tasks as tasks_mod
from ._vendor.contract import CapacityDeferred
from .adapters import REGISTRY
from .models.task import Task, TaskStatus
from .paths import Layout

KIND = "diagnosis"
ACTIONS = ("requeue", "defer", "architect", "revise", "owner")
LEVEL_ACTIONS = {"manager": ("requeue", "defer", "architect", "owner"),
                 "architect": ("requeue", "defer", "revise", "owner")}
LEVELS = ("manager", "architect")
DEFAULT_ROLES = {"manager": "senior_pm", "architect": "chief_architect"}
DIAGNOSIS_BUDGET = 2            # requeues, defers and revisions (each counted separately) per task; then the owner
# What a revise may change. Everything else in the contract is the owner's: who does and reviews the work
# (owner_role, reviewer, review_note), whether the owner approves it (human_approval, approval_note), when it runs
# (phase), what it costs (cost_category) and what it is (title, objective, escalation).
REVISABLE_FIELDS = ("job", "actions", "owned_paths", "artifacts", "tests", "checks", "acceptance", "depends_on")
# verification (and the order the plan set) may only grow: every existing entry stays as it is; acceptance may be
# extended, not rewritten. Dropping or altering one would let the task pass a check it was failing.
ADDITIVE_FIELDS = ("artifacts", "tests", "checks", "acceptance", "depends_on")
JOB_LANE_KEYS = ("adapter", "model", "role")   # which lane runs the job and what it costs: the owner's (spending)
MAX_CHANGES = 10


_COMMON_FIELDS = ('"summary":"one paragraph: what actually went wrong","guidance":"(requeue) concrete instructions '
                  'for the next attempt","blocked_on":"(defer) what must happen first","new_task":{"id":"(defer) an '
                  'id no task uses yet","title":"...","objective":"...","owner_role":"...","owned_paths":["..."],'
                  '"artifacts":["..."],"checks":["..."],"reviewer":["..."]},"wait_for":["(defer) ids of existing '
                  'unfinished tasks"],')
_REQUEUE_DEFER = (
    "`requeue` means you are confident a fresh attempt with your guidance will pass the SAME verification and reviews; "
    "do not requeue to try the same thing again. Guidance reaches an agent's next brief only; it changes nothing "
    "about a deterministic job (process/python), which runs its stored command again. `defer` must name what "
    "releases this task: `new_task` is the "
    "contract of work that does not exist yet (the fields of `supervisor task create`: id, title, objective, "
    "owner_role, owned_paths, artifacts, actions, tests, checks, reviewer, acceptance, depends_on; phase and priority "
    "default to this task's) - the supervisor creates it, makes this task depend on it, and runs this task again "
    "once it completes; `wait_for` lists existing unfinished task ids this task must wait for. Give either or both. "
    "A `defer` with neither, or with one the supervisor cannot accept, goes to the owner, because nothing else would "
    "ever release the task. A `new_task` carries no `job`. ")
_OWNER = "`owner` is for product choices, spending, external commitments, or physical actions on the workstation. "


def result_contract(level: str = "manager", revise: bool = True) -> str:
    """The result contract a diagnosis brief ends with. With `revise` (routing.yaml escalation.revise, default on)
    the manager sends a contract fix to the architect and the architect may revise; without it a wrong contract is
    the owner's, as before."""
    actions = "|".join(a for a in LEVEL_ACTIONS[level] if revise or a != "revise")
    if level == "manager":
        extra_fields = '"question":"(owner) the one decision or action only the owner can take"'
        if revise:
            route = ("`architect` is for a design question, and for any fix that is a change to this task's own "
                     "contract rather than to the agent's work: its `job` (for example a wrong argument in a process "
                     "command), checks, artifacts, owned_paths or depends_on. You cannot change the contract; the "
                     "architect can. Say in `guidance` what is wrong with the contract, why, and the change you would "
                     "make - the architect's brief carries it. ")
        else:
            route = ("`architect` is for a design question, not an execution problem. A diagnosis cannot change a "
                     "task's job: if a process task's own command is wrong (not the agent's work), use "
                     '`action: "owner"` and name the `task.set_job` change the task needs in `question`. ')
        rules = _REQUEUE_DEFER + route + _OWNER
    else:
        extra_fields = ('"changes":[{"path":"(revise) a dotted path into the stored task, e.g. job.argv.3",'
                        '"value":"the new value"}],"reason":"(revise) what was wrong with the contract and why the '
                        'new value is right","question":"(owner) the one decision or action only the owner can take"'
                        if revise else '"question":"(owner) the one decision or action only the owner can take"')
        rules = _REQUEUE_DEFER + _OWNER
        if revise:
            rules += (
                "`revise` changes this task's own contract and runs it again. Each entry of `changes` addresses the "
                "stored task shown under \"Stored contract\": dict keys and list indexes joined by dots "
                "(`job.argv.3`, `checks.1`); its `value` replaces what is there, and an index one past the end of a "
                "list appends. Change only what the fix needs. You may change the job (not its "
                f"{', '.join(JOB_LANE_KEYS)}), actions and owned_paths, and add to - never drop or alter an entry "
                f"of - {', '.join(ADDITIVE_FIELDS)}. Anything else (owner_role, reviewer, human_approval, phase, "
                "cost_category, title, objective), a change that loosens verification, or a revision the "
                "supervisor's task checks refuse goes to the owner. One revision per failure: if the revised task "
                "fails the same way again, the owner decides. ")
        else:
            rules += ("A diagnosis cannot change a task's job: if a process task's own command is wrong, use "
                      '`action: "owner"` and name the `task.set_job` change the task needs in `question`. ')
    return ("## Result contract\nEnd your answer with exactly one fenced JSON block:\n"
            f'```json\n{{"kind":"diagnosis","schema_version":1,"action":"{actions}",' + _COMMON_FIELDS
            + extra_fields + "}\n```\n" + rules + "\n")


RESULT_CONTRACT = result_contract("manager")
ARCHITECT_RESULT_CONTRACT = result_contract("architect")
# what a diagnosis may put in `new_task`: a seed entry's contract, never runtime fields or a `job` (D-018)
NEW_TASK_FIELDS = frozenset(["id", "priority", *tasks_mod.CONTRACT_FIELDS])


def settings(layout: Layout) -> dict:
    cfg = layout.config / "routing.yaml"
    out = {"enabled": True, "revise": True, "roles": dict(DEFAULT_ROLES)}
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        esc = data.get("escalation") or {}
        if "diagnosis" in esc:
            out["enabled"] = bool(esc["diagnosis"])
        if "revise" in esc:
            out["revise"] = bool(esc["revise"])
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


def release_plan(layout: Layout, task: Task, rec: dict, obj: dict) -> tuple[dict | None, str]:
    """(plan, "") for a defer that names what releases the task, or (None, why) for one the supervisor cannot act
    on. The plan is what ingest() records on the job before it writes anything, so a replay repeats it instead of
    re-validating against a store it already changed: `new_task` (the stored document `tasks.create` writes, or
    None), `depends_on` (the ids added to the task) and `warnings`.

    A `new_task` passes the checks `supervisor task create` applies, against the graph as it will be (the deferred
    task already depending on it), and two more: it may carry only contract fields (no `job`, no runtime fields), and
    its contract warnings (a role routing does not configure, a check verification would refuse, an unregistered
    lane) are refusals, since a machine-proposed task that would wait forever only moves the stall. Reads only."""
    if sum(1 for r in _records(layout, task.id) if r.get("diag_release") and r["attempt_id"] != rec["attempt_id"]) >= DIAGNOSIS_BUDGET:
        return None, f"diagnosis budget of {DIAGNOSIS_BUDGET} defers spent"
    proposal, wait_for = obj.get("new_task"), obj.get("wait_for") or []
    if not proposal and not wait_for:
        return None, "the defer names no task to wait for (no new_task, no wait_for)"
    if not isinstance(wait_for, list) or not all(isinstance(w, str) and w.strip() for w in wait_for):
        return None, "wait_for must be a list of task ids"
    combined = tasks_mod.load_all(layout)
    for w in wait_for:
        if w == task.id or w not in combined or combined[w].status == TaskStatus.CANCELLED:
            return None, f"wait_for names {w!r}, which is {'this task' if w == task.id else 'no live task'}"
    deps, doc, warnings = list(wait_for), None, []
    if proposal:
        if not isinstance(proposal, dict):
            return None, "new_task must be an object (a task contract)"
        extra = sorted(set(proposal) - NEW_TASK_FIELDS)
        if extra:
            return None, f"new_task may carry only contract fields, not {', '.join(extra)}"
        new_id = str(proposal.get("id") or "").strip()
        if not new_id or new_id in combined:
            return None, (f"new_task id {new_id!r} is already a task (name it in wait_for)" if new_id
                          else "new_task has no id")
        deps.append(new_id)
        doc = {"phase": task.phase, "priority": task.priority, **proposal, "id": new_id}
    elif all(combined[w].status == TaskStatus.COMPLETE for w in wait_for):
        return None, "every task in wait_for is already complete, so nothing would hold this task back"
    combined[task.id] = combined[task.id].model_copy(
        update={"depends_on": list(dict.fromkeys(list(combined[task.id].depends_on) + deps))})
    if doc is None:
        errors = tasks_mod.validate_graph(combined)
    else:
        try:
            doc, errors, warnings = tasks_mod.check_new(layout, doc, combined)
        except ValidationError as exc:
            return None, f"new_task is not a valid task contract: {str(exc)[:400]}"
        refusals = tasks_mod.contract_warnings([Task.model_validate(doc)], layout)
        errors += refusals
        warnings = [w for w in warnings if w not in refusals]
    if errors:
        return None, "; ".join(errors)[:1000]
    return {"new_task": doc, "depends_on": deps, "warnings": warnings}, ""


def _release(supervisor, task: Task, rec: dict, role: str, summary: str, blocked_on: str, plan: dict) -> dict:
    """Carry out a release_plan(): create the new task (an existing id is a no-op, so a replay creates nothing
    twice), add the dependencies, grant a fresh retry budget (keyed by this job, so once) and put the task back
    to READY. The scheduler holds it until every dependency is COMPLETE."""
    from . import escalation
    layout, deps, doc = supervisor.layout, list(plan["depends_on"]), plan.get("new_task")
    if doc:
        try:
            tasks_mod.create(layout, doc, actor="escalation")
        except (tasks_mod.TaskError, ValidationError) as exc:      # the store moved since the plan was made
            jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action="owner")
            return route_to_owner(layout, task, rec, summary, _defer_question(task, role, blocked_on),
                                  f"the proposed task {doc['id']} could not be created: {str(exc)[:300]}")
        tasks_mod.add_note(layout, doc["id"], f"created by the {role} diagnosis of {task.id}: {blocked_on[:300]}",
                           actor="escalation", command_id=f"{rec['attempt_id']}:created")

    def fn(d):
        d["depends_on"] = list(dict.fromkeys(list(d.get("depends_on") or []) + deps))
    tasks_mod._update(layout, task.id, fn)
    tasks_mod.log(layout, "diagnosis_release", "escalation", task=task.id, role=role, depends_on=deps,
                  created=(doc or {}).get("id"), **({"warnings": plan["warnings"]} if plan.get("warnings") else {}))
    escalation.reset_retries(layout, task.id, reason=f"deferred by {role} diagnosis", key=rec["attempt_id"])
    escalation.grant_continuations(layout, task.id, reason=f"deferred by {role} diagnosis")
    note = f"{role} diagnosis: deferred until {', '.join(deps)} complete — {blocked_on[:200]}"
    status = tasks_mod.load(layout, task.id).status
    if status == TaskStatus.IN_PROGRESS:
        tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=note)
        status = TaskStatus.BLOCKED
    if status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
        tasks_mod.set_status(layout, task.id, TaskStatus.READY, actor="escalation", note=note)
    else:
        tasks_mod.add_note(layout, task.id, note, actor="escalation", command_id=f"{rec['attempt_id']}:deferred")
    return {"action": "defer", "blocked_on": blocked_on, "depends_on": deps, "created": (doc or {}).get("id")}


def _set_path(doc: dict, path: str, value):
    """Set `path` (dict keys and list indexes joined by dots) in `doc` to `value`; return what was there (None for a
    new dict key or an appended list item). Every step but the last must exist; a list index may be one past the
    end (append). ValueError for anything else."""
    parts = path.split(".")
    if not all(parts):
        raise ValueError("has an empty segment")
    cur = doc
    for i, seg in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(cur, dict):
            if last:
                old = copy.deepcopy(cur.get(seg))
                cur[seg] = value
                return old
            if seg not in cur:
                raise ValueError(f"names {'.'.join(parts[:i + 1])!r}, which the task does not have")
            cur = cur[seg]
        elif isinstance(cur, list):
            if not seg.isdigit() or int(seg) > len(cur) or (int(seg) == len(cur) and not last):
                raise ValueError(f"indexes {'.'.join(parts[:i])!r} (a list of {len(cur)}) with {seg!r}")
            n = int(seg)
            if last:
                if n == len(cur):
                    cur.append(value)
                    return None
                old = copy.deepcopy(cur[n])
                cur[n] = value
                return old
            cur = cur[n]
        else:
            raise ValueError(f"goes below {'.'.join(parts[:i])!r}, which is a {type(cur).__name__}")
    raise ValueError("is empty")


def _dropped(field: str, old: Task, new: Task) -> list:
    """Entries of verification field `field` the revision drops or alters (ADDITIVE_FIELDS)."""
    if field == "acceptance":
        return [] if not old.acceptance or new.acceptance.startswith(old.acceptance) else [old.acceptance]
    if field == "checks":
        before, after = [c.model_dump() for c in old.checks], [c.model_dump() for c in new.checks]
    else:
        before, after = list(getattr(old, field)), list(getattr(new, field))
    return [e for e in before if e not in after]


def revision_plan(layout: Layout, task: Task, rec: dict, obj: dict) -> tuple[dict | None, str]:
    """(plan, "") for an architect `revise` the supervisor can apply, or (None, why) for one that goes to the owner.
    The plan is recorded on the job before anything is written, so a replay repeats it: `changes` ({path, old,
    new}), `job` (the new job block, or None when it is unchanged), `fields` (the other revised contract fields with
    their new values), `reason` and `warnings`.

    The patch is applied to a copy of the stored task and the result checked like a new contract (tasks.check_
    contract: pydantic, the graph, ownership, artifacts); a contract warning the revision introduces (an
    unregistered lane, a check verification refuses, a role routing does not configure) is a refusal, since a
    revised task that cannot run only moves the stall. Errors and warnings the task already had are not the
    revision's and do not refuse it. Reads only."""
    if not settings(layout).get("revise", True):
        return None, "revise is turned off (routing.yaml escalation.revise: false)"
    if rec.get("diag_level") != "architect":
        return None, "only the architect may revise a contract"
    earlier = [r for r in _records(layout, task.id) if r.get("diag_revision") and r["attempt_id"] != rec["attempt_id"]]
    if any(r.get("diag_signature") == rec.get("diag_signature") for r in earlier):
        return None, ("this task's contract was already revised once for this failure and it failed the same way "
                      "again, so the revision did not fix it")
    if len(earlier) >= DIAGNOSIS_BUDGET:
        return None, f"diagnosis budget of {DIAGNOSIS_BUDGET} revisions spent"
    reason = str(obj.get("reason") or "").strip()[:2000]
    changes = obj.get("changes")
    if not reason:
        return None, "the revise gives no reason"
    if not isinstance(changes, list) or not changes:
        return None, "the revise names no changes (a list of {path, value})"
    if len(changes) > MAX_CHANGES:
        return None, f"the revise names {len(changes)} changes (at most {MAX_CHANGES})"
    raw = store.read(tasks_mod.path_for(layout, task.id))
    # the paths address the contract as the brief shows it (_stored_contract): validated, so a check written as a
    # bare string in the store is {"command": ...} here too
    stored, dumped = copy.deepcopy(raw), Task.model_validate(raw).model_dump(mode="json")
    stored.update({f: dumped[f] for f in REVISABLE_FIELDS if f != "job" and f in dumped})
    doc, done, touched = copy.deepcopy(stored), [], []
    for ch in changes:
        if not isinstance(ch, dict) or set(ch) != {"path", "value"} or not isinstance(ch["path"], str):
            return None, "each change must be exactly {path, value}"
        path = ch["path"].strip()
        top = path.split(".")[0]
        if top not in REVISABLE_FIELDS:
            return None, f"change {path!r}: {top} is the owner's to change, not a diagnosis's"
        try:
            old = _set_path(doc, path, copy.deepcopy(ch["value"]))
        except ValueError as exc:
            return None, f"change {path!r} {exc}"
        done.append({"path": path, "old": old, "new": ch["value"]})
        touched.append(top)
    if doc == stored:
        return None, "the revise changes nothing"
    old_job, new_job = stored.get("job") or {}, doc.get("job") or {}
    if "job" in touched:
        if not isinstance(doc.get("job"), dict):
            return None, "job must stay an object"
        moved = [k for k in JOB_LANE_KEYS if old_job.get(k) != new_job.get(k)]
        if moved:
            return None, (f"job.{moved[0]} decides which lane runs the task and what it costs; changing it is the "
                          "owner's call")
        argv = new_job.get("argv")
        if argv is not None and not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            return None, "job.argv must be a non-empty list of strings"
    try:
        old_t, new_t = Task.model_validate(stored), Task.model_validate(doc)
    except ValidationError as exc:
        return None, f"the revised task is not a valid task contract: {str(exc)[:400]}"
    for field_name in ADDITIVE_FIELDS:
        dropped = _dropped(field_name, old_t, new_t)
        if dropped:
            return None, (f"the revision drops or alters {field_name} ({json.dumps(dropped, default=str)[:300]}); "
                          "loosening verification is the owner's call")
    combined = tasks_mod.load_all(layout)
    old_errors, old_warnings = tasks_mod.check_contract(layout, old_t, combined)
    errors, warnings = tasks_mod.check_contract(layout, new_t, combined)
    errors = [e for e in errors if e not in old_errors]
    refusals = [w for w in warnings if w not in old_warnings]
    if errors or refusals:
        return None, "the revised contract is refused: " + "; ".join(errors + refusals)[:1000]
    fields = {f: doc.get(f) for f in dict.fromkeys(touched) if f != "job"}
    return {"changes": done, "job": doc["job"] if "job" in touched and new_job != old_job else None,
            "fields": fields, "reason": reason, "warnings": [w for w in warnings if w in old_warnings]}, ""


def _short(value) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 200 else text[:197] + "..."


def _revise(supervisor, task: Task, rec: dict, role: str, summary: str, plan: dict) -> dict:
    """Carry out a revision_plan() through the paths an owner's own fix would take: the job through
    commands.apply_job (`task.set_job`), the other fields through tasks.revise_contract, which also keeps the
    revision on the task. Then one note with old value, new value and reason, a fresh retry budget (keyed by this
    job, so once) and READY. Every step is idempotent, so a replay finishes what a crash left."""
    from . import commands, escalation
    layout, key = supervisor.layout, rec["attempt_id"]
    if plan.get("job") is not None and (tasks_mod.load(layout, task.id).model_extra or {}).get("job") != plan["job"]:
        commands.apply_job(layout, task.id, plan["job"], actor="architect")
    tasks_mod.revise_contract(layout, task.id, plan.get("fields") or {}, actor="architect", key=key,
                              record={"role": role, "signature": rec.get("diag_signature"),
                                      "changes": plan["changes"], "reason": plan["reason"]})
    what = "; ".join(f"{c['path']}: {_short(c['old'])} → {_short(c['new'])}" for c in plan["changes"])
    tasks_mod.add_note(layout, task.id, f"{role} diagnosis revised the contract: {what}. Reason: {plan['reason'][:600]}",
                       actor="architect", command_id=f"{key}:revised")
    tasks_mod.log(layout, "diagnosis_revise", "escalation", task=task.id, role=role,
                  changes=[{"path": c["path"], "old": _short(c["old"]), "new": _short(c["new"])} for c in plan["changes"]],
                  **({"warnings": plan["warnings"]} if plan.get("warnings") else {}))
    escalation.reset_retries(layout, task.id, reason=f"contract revised by {role} diagnosis", key=key)
    escalation.grant_continuations(layout, task.id, reason=f"contract revised by {role} diagnosis")
    note = f"{role} diagnosis: contract revised ({', '.join(c['path'] for c in plan['changes'])}) — {summary[:200]}"
    status = tasks_mod.load(layout, task.id).status
    if status == TaskStatus.IN_PROGRESS:
        tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=note)
        status = TaskStatus.BLOCKED
    if status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
        tasks_mod.set_status(layout, task.id, TaskStatus.READY, actor="escalation", note=note)
    return {"action": "revise", "changes": plan["changes"]}


def _revise_question(task: Task, role: str, obj: dict) -> str:
    changes = obj.get("changes") if isinstance(obj.get("changes"), list) else []
    proposed = "; ".join(f"{c.get('path')} = {_short(c.get('value'))}" for c in changes[:MAX_CHANGES]
                         if isinstance(c, dict)) or "nothing usable"
    return (f"The {role} proposed revising {task.id}'s contract ({proposed[:400]}) and the supervisor did not apply "
            f"it (see Why). Apply the change yourself (`supervisor task set-job`, or the seed and `task import "
            f"--update-contracts {task.id}`) and REQUEUE, or decide here.")


def _defer_question(task: Task, role: str, blocked_on: str) -> str:
    return (f"The {role} deferred {task.id} until: {blocked_on[:300]} — but named no task the supervisor could "
            f"accept to wait for (see Why), so nothing would ever release it. Create the work it needs (`supervisor "
            f"task create`) and add it to {task.id}'s depends_on, or decide here.")


def _stored_contract(task: Task) -> str:
    """The revisable part of the stored task, as the paths of a `revise` address it."""
    doc = task.model_dump(mode="json")
    shown = {f: doc.get(f) if f != "job" else (task.model_extra or {}).get("job") for f in REVISABLE_FIELDS}
    return "\n## Stored contract\n```json\n" + json.dumps({k: v for k, v in shown.items() if v not in (None, "", [])},
                                                         indent=1, ensure_ascii=False)[:4000] + "\n```\n"


def _handoff_block(handoff: dict | None) -> str:
    if not handoff:
        return ""
    return (f"\n## Hand-off from the {handoff.get('role') or 'manager'} diagnosis\nThe manager sent this task to you. "
            f"What it found:\n{str(handoff.get('summary') or '')[:2000]}\n\nWhat it says is wrong with the contract, "
            f"and why:\n{str(handoff.get('guidance') or '(no guidance given)')[:4000]}\n")


def brief(layout: Layout, task: Task, role: str, level: str, source: str, detail: str, issue_id: str | None,
          handoff: dict | None = None) -> str:
    from . import packets
    card = packets.role_card(layout, role)
    revise = settings(layout).get("revise", True)
    attempts = "\n".join(f"- {a.id} ({a.resource}): {a.note}" for a in task.attempts[-6:]) or "- none recorded"
    notes = "\n".join(f"- {n.get('by')}: {n.get('note')}" for n in ((task.model_extra or {}).get("notes") or [])[-6:]) or "- none"
    actions = [a for a in LEVEL_ACTIONS.get(level, LEVEL_ACTIONS["architect"]) if revise or a != "revise"]
    levels = ", ".join(actions[:-1]) + f" or {actions[-1]}"
    text = ((card + "\n\n") if card else "") + (
        f"# Diagnosis for {task.id}: {task.title}\n\nRole: {role} ({level} level). Repository: {layout.root}\n\n"
        f"Automatic retries for this task have stopped. Decide what happens next — {levels}. Read the repository and "
        f"the run directories under runs/{task.id}/; do not modify anything.\n\n"
        + packets.contract_block(task) +
        "\n## Expected artifacts\n" + "\n".join(f"- {a}" for a in task.artifacts) + "\n" + _stored_contract(task) +
        f"\n## Why retries stopped ({source})\n```\n{detail[:4000]}\n```\n\n## Attempts\n{attempts}\n\n## Notes on the task\n{notes}\n"
        + (f"\n## Issue\n{issue_id}\n" if issue_id else "") + _handoff_block(handoff) + "\n"
        + result_contract("manager" if level == "manager" else "architect", revise))
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
             issue_id: str | None = None, grant: int = 0, handoff: dict | None = None) -> dict | None:
    """The diagnosis job for (task, signature, level) under this grant generation (`grant`, R4): the EXISTING
    record when one already matches (a replayed hand-off reuses a transition that is already durable — round-3
    review F7 — never a fresh dispatch, so a second architect for the same signature and generation is still
    never created), a freshly dispatched one otherwise, or None when the route is disabled, the role's lane is
    unavailable, or its capacity is out (the escalation then stands as an issue + BLOCKED task, as before). None
    therefore only ever means "no diagnosis is running for this" — a caller that reads it as "unavailable" is
    never looking at one that is in fact already under way."""
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
    view = _task_view(layout, task, role, adapter_name,
                      brief(layout, task, role, level, source, detail, issue_id, handoff), overrides)
    rec = supervisor.dispatch(view, adapter_name, note=f"diagnosis:{role}", kind=KIND,
                              extra={"diag_signature": signature, "diag_level": level, "diag_issue": issue_id,
                                     "diag_source": source, "diag_detail": detail[:4000], "diag_role": role,
                                     "diag_grant": grant, **({"diag_handoff": handoff} if handoff else {})})
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
                      brief(layout, task, role, level, rec.get("diag_source", ""), rec.get("diag_detail", ""),
                            rec.get("diag_issue"), rec.get("diag_handoff")), {})


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
    rejected = ""
    if result.status == "FAILED" or action not in LEVEL_ACTIONS.get(level, ()):
        why = result.error or (f"action {action!r} not allowed at {level} level" if action else "no structured diagnosis")
        tasks_mod.log(layout, "diagnosis_failed", "loop", task=task.id, role=role, level=level, error=str(why)[:300])
        rejected = str(why)[:300]
        action, summary = "owner", summary or f"The {role} diagnosis did not produce a usable decision ({why})."
    if task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        tasks_mod.log(layout, "diagnosis_ignored", "loop", task=task.id, reason=f"task is {task.status.value}")
        return {"action": "ignored"}
    blocked_on = str(obj.get("blocked_on") or summary)[:1000]
    plan, revision, refused = None, None, ""
    if action == "defer" and not task.held:
        # A replay keeps the plan already recorded: its new task may exist by now, which a fresh check would refuse.
        plan = rec.get("diag_release") or None
        if plan is None:
            plan, refused = release_plan(layout, task, rec, obj)
        if plan is None:
            tasks_mod.log(layout, "diagnosis_defer_refused", "loop", task=task.id, role=role, level=level,
                          reason=refused[:300])
            action = "owner"
    if action == "revise" and not task.held:
        # As for a defer: a replay applies the recorded plan; the store may already hold the revised contract.
        revision = rec.get("diag_revision") or None
        if revision is None:
            revision, refused = revision_plan(layout, task, rec, obj)
        if revision is None:
            tasks_mod.log(layout, "diagnosis_revise_refused", "loop", task=task.id, role=role, level=level,
                          reason=refused[:300])
            action = "owner"
    jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action=action,
                       **({"diag_release": plan} if plan else {}), **({"diag_revision": revision} if revision else {}))
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
        return _release(supervisor, task, rec, role, summary, blocked_on, plan)
    if action == "revise":
        if task.held:
            # The owner's Hold stands, as for a requeue or a defer: nothing is changed, the proposal is written on
            # the task, and the record says "held" so it counts as no revision.
            jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action="held")
            tasks_mod.add_note(layout, task.id, f"{role} diagnosis: contract revision declined — task is held by the "
                               f"owner; it proposed: {_revise_question(task, role, obj)[:400]}", actor="escalation")
            return {"action": "held", "held_by": task.held.get("by")}
        return _revise(supervisor, task, rec, role, summary, revision)
    if action == "architect":
        guidance = str(obj.get("guidance") or "")[:4000]
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: → architect. {summary[:200]}", actor="escalation")
        # Carry this diagnosis's OWN grant generation (F4): `already()`/`dispatch` dedupe an architect request by
        # (signature, level, grant), and omitting it here defaulted to 0, which let a generation-1 manager's
        # architect request match the spent generation-0 architect record and wrongly report the lane unavailable.
        nxt = dispatch(supervisor, task, rec.get("diag_source", "diagnosis"), rec.get("diag_detail", "") or summary,
                       rec.get("diag_signature", ""), level="architect", issue_id=issue_id, grant=rec.get("diag_grant") or 0,
                       handoff={"role": role, "summary": summary, "guidance": guidance})
        if nxt is None:                                        # no architect lane right now: the owner hears about it
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "architect lane unavailable")
        return {"action": "architect", "attempt": nxt.get("attempt_id")}
    if refused and str(obj.get("action") or "").lower() == "revise":
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: contract revision not applied — {refused[:300]} — "
                           "owner decision", actor="escalation")
        return route_to_owner(layout, task, rec, summary, _revise_question(task, role, obj), refused[:300])
    if rejected and str(obj.get("action") or "").lower() == "revise":
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: {rejected} — owner decision", actor="escalation")
        return route_to_owner(layout, task, rec, summary, _revise_question(task, role, obj), rejected)
    if refused:
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: deferred, but {refused[:300]} — owner decision",
                           actor="escalation")
        return route_to_owner(layout, task, rec, summary, _defer_question(task, role, blocked_on), refused[:300])
    tasks_mod.add_note(layout, task.id, f"{role} diagnosis: owner decision — {summary[:200]}", actor="escalation")
    return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "needs the owner")
