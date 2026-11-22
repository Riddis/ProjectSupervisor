"""One computed reason a non-terminal task cannot run, read by the idle alert, the Overview and the task page
(RECOVERY_PLAN.md section 4.3, D-027 decision 11, step 10 of the order of work).

Before this module each of the three surfaces either quoted a task's last note -- prose an agent wrote, often
about something else -- or showed nothing. `why_not_runnable(task, tasks, layout)` computes the reason once, from
the closed set of nine codes below, by calling the exact primitives `supervisor.gate.check` itself calls for a
delivery launch (`tasks.unmet_dependency`, `gates.phase_open`, `gate.role_inactive`, the adapter registry, the
stored preflight, `capacity.require`, `gate.holds_resource`/`lease_blocked`/`owned_for`) -- never a second copy of
that logic, so the two can never drift. `choices_for` then builds, from the same reason, only the choices the
underlying effect vocabulary (`supervisor.plans.OPERATIONS`/`OWNER_ONLY_EFFECTS`) would actually accept: a
dependency or a gate or an inactive role is not fixed by flipping the task's own status, so `resume` is never one
of them, and a code that names a configuration line (`role_inactive`, `lane_unregistered`, `phase_gate`) admits no
choice at all -- there is no button that could fix a line in `routing.yaml` or `gates.yaml`.

A held task is never classified here: `loop.alert_if_idle` excludes it before calling this module, exactly as the
pre-2.8.0 idle alert already did (RECOVERY_PLAN.md section 11's own record of what is already closed) -- a hold is
the owner's own call, never the pipeline's problem.

The loop's own decision (PAUSE/DRAIN/STOP) is excluded the same way, for the same reason, and is never a tenth
code: `gate.check`'s first non-terminal refusal (`decision != RUN`) blocks every delivery launch identically,
whatever the task, so it is not this module's per-task reason to compute. `decision_blocked` reads it once;
every caller checks it -- before calling `why_not_runnable` for a task whose reason would otherwise come from the
structural classifier -- exactly as a held task is excluded before it ever reaches this module at all. A task
`WAITING_FOR_HUMAN` or `WAITING_FOR_REVIEW` is unaffected either way: neither path runs through `gate.check`.
"""
from __future__ import annotations

from typing import Any

from .models.task import TERMINAL, Task, TaskStatus
from .paths import Layout

CODES = frozenset({
    "unmet_dependency", "phase_gate", "role_inactive", "capacity_floor", "lane_unregistered",
    "awaiting_review", "awaiting_approval", "preflight_failed", "resource_claimed", "needs_mismatch",
})

# Which choices a reason admits, keyed by code -- built once here so `loop.alert_if_idle`, the Overview and the
# task page can never disagree about what to offer. `resume` never appears: every code below names a structural
# reason a bare status change does not clear. The two diagnosable codes (`unmet_dependency`, `preflight_failed`)
# and the unclaimed fallback of `resource_claimed` (no blocker found at all -- nothing has yet asked a diagnosis
# to look) admit `diagnose_now`; the contention codes admit `hold` as well as `cancel_task`; the three pure
# configuration lines (`role_inactive`, `lane_unregistered`, `phase_gate`) and the two in-flight codes
# (`awaiting_review`, `awaiting_approval`) admit nothing a button could do.
_DIAGNOSABLE = frozenset({"unmet_dependency", "preflight_failed"})
_CONTENDED = frozenset({"capacity_floor"})
# D-028 decision 3: the two writes that clear a needs mismatch (a routing edit, a task.patch on `needs`) are
# named in its own detail, never offered as a button -- `resume` would be refused by the mismatch it ignores, so
# this code admits exactly what a stalled contention admits and no `diagnose_now` (there is nothing to probe).
_NAMED_ESCAPE_ONLY = frozenset({"needs_mismatch"})


def _choice_names(why: dict) -> list[str]:
    code = why["code"]
    if code in _DIAGNOSABLE:
        return ["diagnose_now", "cancel_task"]
    if code in _CONTENDED or code in _NAMED_ESCAPE_ONLY:
        return ["cancel_task", "hold"]
    if code == "resource_claimed":
        return ["cancel_task", "hold"] + (["diagnose_now"] if not why.get("blockers") else [])
    return []                                           # phase_gate, role_inactive, lane_unregistered,
                                                          # awaiting_review, awaiting_approval: no button fixes it


def choices_for(task: Task, why: dict) -> list[dict]:
    """Only the effects `why`'s own code admits, each a bare operation naming `task.id` -- never `resume`, whose
    absence from every row is this function's own guard against offering a button the task's own reason would
    make pointless. A task already held never reaches `hold` again."""
    effects = {"diagnose_now": {"op": "diagnose_now", "task": task.id},
              "cancel_task": {"op": "cancel_task", "task": task.id},
              "hold": {"op": "hold", "task": task.id}}
    return [{"id": name, "effect": effects[name]} for name in _choice_names(why)
           if name != "hold" or not task.held]


def render_reason(why: dict) -> str:
    """The one sentence every surface shows for `why` -- its own `detail`, nothing added, so the idle alert, the
    Overview and the task page read byte-identical text by construction rather than by convention. Never cut
    short by this function: a caller that needs to fit the result into a budget (the idle alert's own item) must
    bound the *whole* item (`inbox.fit`), never this sentence alone, or two surfaces showing the same reason at
    different lengths would be a disagreement again."""
    return str(why["detail"])


def decision_blocked(layout: Layout) -> str | None:
    """`None` while the loop's own decision is RUN; else the one sentence every surface shows instead of calling
    this module's structural classifier at all, for a task whose reason would otherwise come from it. Mirrors
    `gate.check`'s own first non-terminal refusal (`decision != RUN`) -- not one of the nine codes, because a
    paused, draining or stopped loop blocks every delivery launch identically, whatever the task: a project-wide
    state, not a per-task reason, read once here rather than folded into `_structural`'s per-task classification.
    A task `WAITING_FOR_HUMAN` or `WAITING_FOR_REVIEW` does not launch through `gate.check` either way, so a
    caller never applies this to one of those; `why_not_runnable` itself is unaffected (it still classifies, and
    the test suite's own fixtures run with decision RUN) -- only the three surfaces guard the structural path
    with this, exactly as they already exclude a held task before calling `why_not_runnable` at all."""
    from . import runtime as runtime_mod
    decision = runtime_mod.read(layout).get("decision", "RUN")
    if decision == "RUN":
        return None
    return f"the loop's decision is {decision}; no delivery launches until it returns to RUN"


def _awaiting_approval(task: Task, layout: Layout) -> dict:
    from . import inbox as inbox_mod
    digest = task.delivery.digest or ""
    open_items = [i for i in inbox_mod.all_items(layout)
                 if i.get("task_id") == task.id and i.get("status") == "open"
                 and i.get("kind") == inbox_mod.APPROVAL_KIND]
    if open_items:
        item = open_items[0]
        detail = (f"awaiting your approval of digest {digest[:12]}, inbox item {item['id']}" if digest
                 else f"awaiting your approval, inbox item {item['id']}")
        return {"code": "awaiting_approval", "detail": detail, "blockers": [item["id"]]}
    detail = (f"awaiting an approval item for digest {digest[:12]}" if digest else "awaiting an approval item")
    return {"code": "awaiting_approval", "detail": detail, "blockers": []}


def _awaiting_review(task: Task, layout: Layout) -> dict:
    from . import gate as gate_mod
    from . import jobs as jobs_mod
    digest = task.delivery.digest
    live = ({r.role for r in task.delivery.reviews if r.digest == digest and not r.superseded}
           if digest else set())
    pending = [r for r in task.reviewer if r not in live]
    if not pending:
        return {"code": "awaiting_review", "detail": "awaiting review results", "blockers": []}
    in_flight = {(r["task_id"], r.get("review_role")) for r in jobs_mod.open_records(layout)
                if (r.get("spec") or {}).get("kind") == "review"}
    parts = []
    for role in pending:
        if gate_mod.role_inactive(layout, role):
            parts.append(f"{role} ({gate_mod.inactive_reason(role)})")
        elif (task.id, role) in in_flight:
            parts.append(f"{role} (dispatched, awaiting its verdict)")
        else:
            parts.append(f"{role} (not yet dispatched)")
    return {"code": "awaiting_review", "detail": "; ".join(parts), "blockers": list(pending)}


def _structural(task: Task, tasks: dict[str, Task], layout: Layout) -> dict:
    from . import gate as gate_mod
    from . import gates as gates_mod
    from . import jobs as jobs_mod
    from . import scheduler as scheduler_mod
    from . import tasks as tasks_mod
    from ._vendor.contract import CapacityDeferred, overlaps
    from .adapters import REGISTRY

    dep = tasks_mod.unmet_dependency(layout, task, tasks)
    if dep:
        return {"code": "unmet_dependency", "detail": f"waiting on {dep}", "blockers": [dep.split(" ", 1)[0]]}

    gate_data = gates_mod.load(layout)
    if not gates_mod.phase_open(layout, task.phase, gate_data):
        required = (gate_data.get("phases") or {}).get(int(task.phase), {}).get("requires", [])
        gates = gate_data.get("gates") or {}
        unmet = [(gid, gates_mod.requirements_unmet(gates.get(gid))) for gid in required
                if not gates_mod.gate_open(gates.get(gid))]
        detail = ("; ".join(f"gates.yaml gate {gid}: {', '.join(why) or 'not open'}" for gid, why in unmet)
                 or f"phase {task.phase} is not an open phase (gates.yaml phases)")
        return {"code": "phase_gate", "detail": detail, "blockers": [gid for gid, _ in unmet]}

    role = scheduler_mod.job_spec(task).get("role") or task.owner_role
    if gate_mod.role_inactive(layout, role):
        return {"code": "role_inactive", "detail": gate_mod.inactive_reason(role), "blockers": [role]}

    adapter_name = scheduler_mod.adapter_name(task, layout)
    if adapter_name not in REGISTRY:
        return {"code": "lane_unregistered",
               "detail": f"adapter {adapter_name!r} is not registered (register it in the plugin's adapters)",
               "blockers": [adapter_name]}

    pf_job = gate_mod.preflight_binding(task, adapter_name, "delivery")
    if pf_job is not None:
        pf_reason = gate_mod.stored_preflight_reason(layout, pf_job)
        if pf_reason:
            return {"code": "preflight_failed", "detail": f"{pf_job.template or pf_job.adapter}: {pf_reason}",
                   "blockers": [pf_job.template or pf_job.adapter]}

    needs_reason = gate_mod.needs_mismatch_reason(layout, task, adapter_name, "delivery")
    if needs_reason:
        detail = (f"{needs_reason}; put the task back on a lane that satisfies the declaration (a routing edit) "
                 "or change the declaration (task.patch on needs)")
        return {"code": "needs_mismatch", "detail": detail, "blockers": [adapter_name]}

    ad = REGISTRY.get(adapter_name)
    engine = getattr(ad, "engine", None)
    if engine:
        from . import capacity as capacity_mod
        try:
            capacity_mod.require(layout, engine)
        except CapacityDeferred as exc:
            return {"code": "capacity_floor", "detail": str(exc), "blockers": [engine]}

    # Never the task's own open job: a task already running (or with any other job open for it) is not this
    # function's to classify at all -- every caller excludes it before reaching here (the Overview's own
    # `running_ids`, the task page's `task_context` guard below) -- but excluding it here too means a holder
    # list is never the task naming itself as its own blocker, whatever a caller does.
    running = [r for r in jobs_mod.open_records(layout) if r.get("task_id") != task.id]
    lim = gate_mod.limits(layout)
    gpu_busy = sum(1 for r in running if r.get("gpu"))
    if gate_mod.holds_resource(layout, task, adapter_name, "delivery") and gpu_busy >= lim["max_resource_jobs"]:
        holders = [r["task_id"] for r in running if r.get("gpu")]
        return {"code": "resource_claimed",
               "detail": f"resource-lock limit {lim['max_resource_jobs']} reached, held by {', '.join(holders) or 'another job'}",
               "blockers": holders}
    lease = gate_mod.lease_blocked(layout, task, adapter_name, "delivery")
    if lease:
        return {"code": "resource_claimed", "detail": lease, "blockers": []}
    agent_busy = sum(1 for r in running if r.get("adapter") in gate_mod.MODEL_LANES)
    if adapter_name in gate_mod.MODEL_LANES and agent_busy >= lim["max_agent_jobs"]:
        holders = [r["task_id"] for r in running if r.get("adapter") in gate_mod.MODEL_LANES]
        return {"code": "resource_claimed",
               "detail": f"agent concurrency limit {lim['max_agent_jobs']} reached, held by {', '.join(holders) or 'other jobs'}",
               "blockers": holders}
    mine = gate_mod.owned_for(task, adapter_name, "delivery")
    if mine:
        others = gate_mod.busy_paths(layout, [r for r in running if r.get("task_id") != task.id], tasks)
        for other_id, theirs in others:
            try:
                if any(overlaps(a, theirs, layout.root) for a in mine):
                    return {"code": "resource_claimed", "detail": f"owned paths overlap with {other_id} ({theirs})",
                           "blockers": [other_id]}
            except ValueError:
                return {"code": "resource_claimed", "detail": f"owned path error against {other_id}",
                       "blockers": [other_id]}

    return {"code": "resource_claimed", "detail": "nothing currently claims it; no diagnosis has resolved it yet",
           "blockers": []}


def why_not_runnable(task: Task, tasks: dict[str, Task], layout: Layout) -> dict[str, Any]:
    """`{code, detail, blockers}` for `task`, one of the nine codes of `CODES` -- never raised for a non-terminal
    task, whatever its status. `tasks` is the preloaded graph `unmet_dependency` reads; callers that already hold
    one (the idle alert, the Overview, the task page) pass it so this never re-parses the store."""
    if task.status in TERMINAL:
        raise ValueError(f"{task.id} is {task.status.value} (terminal); why_not_runnable classifies only a task "
                         "still open")
    if task.status == TaskStatus.WAITING_FOR_HUMAN:
        return _awaiting_approval(task, layout)
    if task.status == TaskStatus.WAITING_FOR_REVIEW:
        return _awaiting_review(task, layout)
    return _structural(task, tasks, layout)
