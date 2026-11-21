"""Pick what to dispatch: eligible tasks, in priority order, each admitted by the one launch gate (supervisor.gate):
decision, capacity, one resource-lock holder, N CLI-agent jobs, no overlapping ownership."""
from __future__ import annotations

from dataclasses import dataclass

from . import gate, jobs as jobs_mod, tasks as tasks_mod
from . import review as review_mod
from ._vendor.contract import CapacityDeferred
from .adapters import REGISTRY
from .models.task import TERMINAL, CostCategory, Task
from .paths import Layout

MODEL_LANES = gate.MODEL_LANES
# Default adapter per cost category; config/routing.yaml `cost_lanes` overrides any entry (a plugin lane for
# `local`, say). Selection order (`adapter_name`): the task's own `job.adapter`; else, when the cost lane is a model
# lane, the owner role's configured lane (`roles.<owner_role>.adapter`); else the cost lane. The role entry is where
# the setup skill records which provider a role runs on, so a task that keeps the default `cost_category: claude`
# still lands on the role's lane - while deterministic/local/human work keeps its non-model lane whoever owns it.
ADAPTER_FOR_COST = {CostCategory.deterministic: "python", CostCategory.local: "python", CostCategory.claude: "claude_cli",
                    CostCategory.codex: "codex_cli", CostCategory.human: "human", CostCategory.mixed: "claude_cli",
                    CostCategory.paid: "claude_api"}


routing_doc = gate.routing_doc


def cost_lanes(layout: Layout | None) -> dict:
    out = {k.value: v for k, v in ADAPTER_FOR_COST.items()}
    out.update({str(k): str(v) for k, v in (routing_doc(layout).get("cost_lanes") or {}).items()})
    return out


def role_entry(task: Task, layout: Layout | None) -> dict:
    """The raw `roles.<owner_role>` entry of routing.yaml for this task, or {} when the role is not configured."""
    return gate.role_config(layout, task.owner_role)


def role_inactive(task: Task, layout: Layout | None) -> bool:
    """True when the task's owner role is configured `active: false` (see gate.role_inactive, which every launch
    path also applies — this early skip only keeps the plan and its log honest)."""
    return gate.role_inactive(layout, task.owner_role)


def unattended_prefers_api_credits(layout: Layout | None) -> bool:
    """routing.yaml `capacity.unattended_prefers_api_credits`: whether `--unattended` may move a `claude_cli` pick
    onto the metered `claude_api` lane. Off unless the owner set it to true (spend is never the default)."""
    cap = routing_doc(layout).get("capacity") or {}
    return isinstance(cap, dict) and cap.get("unattended_prefers_api_credits") is True

limits = gate.limits

_skips_logged: set[tuple[str, str]] = set()


def _skip_once(layout: Layout, task: Task, reason: str) -> None:
    """Log a `schedule_skip` for a parked task once per process per reason: plan() runs every loop tick and the
    condition persists until the owner edits routing.yaml, so repeating it would flood state/log.jsonl."""
    key = (task.id, reason)
    if key in _skips_logged:
        return
    _skips_logged.add(key)
    tasks_mod.log(layout, "schedule_skip", "loop", task=task.id, reason=reason)


def job_spec(task) -> dict:
    """The task's `job` block (adapter, fn/argv, params) — a typed field on the task document."""
    job = getattr(task, "job", None)
    return job.model_dump(exclude_none=True) if job is not None else {}


def adapter_name(task: Task, layout: Layout | None = None) -> str:
    job = job_spec(task)
    if job and job.get("adapter"):
        return job["adapter"]
    lane = cost_lanes(layout).get(task.cost_category.value, "claude_cli")
    role_adapter = role_entry(task, layout).get("adapter")
    if role_adapter and lane in MODEL_LANES:
        return str(role_adapter)           # the role says which provider its agent work runs on
    return lane                            # deterministic / local / human work is not agent work for the role


@dataclass(frozen=True)
class Resolved:
    """Where a delivery job runs, what it carries, and why (R-16): the one return shape for the first dispatch,
    an automatic revision, a continuation and a fallback alike, so a role's own routing never has to be
    re-derived (or forgotten) by each of those separately. `model`/`effort` are the job overrides to apply, if
    any — the adapter's own `prepare()` still supplies the role's default model when neither is set. `reason`
    is the one line to store on the job record and show where the job is listed."""
    adapter: str
    model: str | None
    effort: str | None
    reason: str
    same_vendor: bool = False


def resolve_lane(layout: Layout, task: Task, *, role: str | None = None, home: str | None = None,
                 revision: bool = False, senior: bool = False) -> Resolved:
    """The role's configured lane (`adapter_name`, reading routing.yaml — calling it with no layout is the R-16
    bug: a role's own adapter is silently forgotten and every caller falls back to the cost-lane default), moved
    to the role's configured fallback only when `review.lane_for` finds the primary unavailable. `home`, given,
    is asked about instead of re-deriving the role's current config — a continuation or a fallback keeping the
    attempt's own lane rather than whatever routing.yaml says now. `senior` substitutes the role's
    `senior_model` for the second automatic revision (model lanes only); a non-senior revision, or one with no
    `senior_model` configured, says plainly that it stays on the base model instead of leaving the choice
    unrecorded."""
    role = role or job_spec(task).get("role") or task.owner_role
    base = home or adapter_name(task, layout)
    try:
        lane, overrides, same_vendor = review_mod.lane_for(layout, role, primary=base)
    except CapacityDeferred:
        lane, overrides, same_vendor = base, {}, False
    model, effort = overrides.get("model"), overrides.get("effort")
    if lane == base:
        bits = [f"role {role}'s configured lane ({lane})"]
    else:
        bits = [f"role {role}'s fallback lane ({lane}); {base} unavailable" + (", same vendor" if same_vendor else "")]
    if lane in MODEL_LANES and revision:
        if senior:
            senior_model = gate.role_config(layout, role).get("senior_model")
            if senior_model:
                model = model or senior_model
                bits.append("senior model (second automatic revision)")
            else:
                bits.append("base model (no senior_model configured for this role)")
        else:
            bits.append("base model (first automatic revision)")
    return Resolved(adapter=lane, model=model, effort=effort, reason="; ".join(bits), same_vendor=same_vendor)


def lane_in_use(layout: Layout, lane: str) -> bool:
    """True when anything could send work to `lane`: an active role's `adapter` or `fallback.adapter`, a `cost_lanes`
    override, a live task that resolves to it (`adapter_name`), or an open job record on it. The loop's token-free
    Codex capacity poll runs only then: a lane being registered says nothing about the project using it, and a
    project or rehearsal copy whose config names no Codex lane must not reach the installed CLI every iteration."""
    doc = routing_doc(layout)
    roles = doc.get("roles") or {}
    for entry in (roles.values() if isinstance(roles, dict) else ()):
        if not isinstance(entry, dict) or entry.get("active", True) is False:
            continue
        fallback = entry.get("fallback")
        if entry.get("adapter") == lane or (isinstance(fallback, dict) and fallback.get("adapter") == lane):
            return True
    overrides = doc.get("cost_lanes") or {}
    if isinstance(overrides, dict) and lane in {str(v) for v in overrides.values()}:
        return True
    if any((rec.get("adapter") or (rec.get("spec") or {}).get("adapter")) == lane
           for rec in jobs_mod.open_records(layout)):
        return True
    return any(t.status not in TERMINAL and adapter_name(t, layout) == lane
               for t in tasks_mod.load_all(layout).values())


def writing_lane(task: Task, layout: Layout | None = None) -> bool:
    """A task whose delivery job runs on a model lane — the jobs that must declare ownership."""
    return adapter_name(task, layout) in MODEL_LANES


def plan(layout: Layout, tasks: dict[str, Task] | None = None, unattended: bool = False) -> list[tuple[Task, str]]:
    """Return [(task, adapter_name)] to dispatch now. Every pick passes gate.check() against the open job records
    plus the picks already made in this plan, so the plan can never admit what launch() would refuse.

    A task whose owner role is configured `active: false` is parked (skipped with a logged reason), never moved to
    another lane.

    `unattended`: a `claude_cli` pick is switched to `claude_api` only when routing.yaml
    `capacity.unattended_prefers_api_credits` is true, the Console org is linked, its monthly credit budget is
    not exhausted and its balance is not marked spent (`claude_api.credit_exhausted`), so overnight batches draw
    API credits instead of the owner's interactive window. With the
    preference off the pick stays on `claude_cli` and waits on that lane's capacity gate; interactive runs (the
    default) never take this branch."""
    tasks = tasks or tasks_mod.load_all(layout)
    running = jobs_mod.open_records(layout)
    running_task_ids = {r["task_id"] for r in running}
    chosen: list[tuple[Task, str]] = []
    chosen_paths: list[tuple[str, str]] = []
    extra_agent = extra_gpu = 0
    prefer_api = unattended and unattended_prefers_api_credits(layout)
    for t in tasks_mod.eligible(layout, tasks):
        if t.id in running_task_ids:
            continue
        if role_inactive(t, layout):
            _skip_once(layout, t, gate.inactive_reason(t.owner_role))
            continue
        name = adapter_name(t, layout)
        if prefer_api and name == "claude_cli" and "claude_api" in REGISTRY:
            from .adapters import claude_api
            # ...and only for a contract the tool-less Messages API lane can actually fulfil (unattended_eligible):
            # an engineering task moved here could neither read its inputs nor write its artifacts.
            if claude_api.credits_remaining(layout) and claude_api.unattended_eligible(layout, t, job_spec(t)):
                name = "claude_api"
        # D-012 F-B Change 1: a role's OWN delivery task falls back the same way a review of someone else's
        # delivery already does (review.lane_for) - whoever owns this job's lane, not only astra reviewing
        # another role's work. A role with no configured fallback still just waits here, token-free, exactly as
        # before: lane_for() re-raises CapacityDeferred and the task is skipped like any other capacity hold.
        role = job_spec(t).get("role") or t.owner_role
        try:
            name, _, _ = review_mod.lane_for(layout, role, primary=name)
        except CapacityDeferred:
            continue
        if name not in REGISTRY:
            continue                                   # adapter not built yet, or not registered in this process
        reason = gate.check(layout, t, name, "delivery", running, extra_paths=chosen_paths,
                            extra_agent=extra_agent, extra_gpu=extra_gpu, decision="RUN", tasks=tasks)
        if reason:
            if reason.startswith("owned path error"):
                tasks_mod.log(layout, "schedule_skip", "loop", task=t.id, reason=reason)
            continue
        chosen.append((t, name))
        chosen_paths += [(t.id, p) for p in gate.owned_for(t, name, "delivery")]
        if gate.holds_resource(layout, t, name, "delivery"):
            extra_gpu += 1
        if name in MODEL_LANES:
            extra_agent += 1
    return chosen
