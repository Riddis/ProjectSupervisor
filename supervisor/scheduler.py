"""Pick what to dispatch: eligible tasks, in priority order, each admitted by the one launch gate (supervisor.gate):
decision, capacity, one resource-lock holder, N CLI-agent jobs, no overlapping ownership."""
from __future__ import annotations

from . import gate, jobs as jobs_mod, tasks as tasks_mod
from .adapters import REGISTRY
from .models.task import TERMINAL, CostCategory, Task
from .paths import Layout

DEFAULT_LIMITS = gate.DEFAULT_LIMITS
MODEL_LANES = gate.MODEL_LANES
# Default adapter per cost category; config/routing.yaml `cost_lanes` overrides any entry (an extension lane for
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
    """The task's `job` block (adapter, fn/argv, params) — an extra field on the task document."""
    extra = getattr(task, "model_extra", None) or {}
    return dict(extra.get("job") or {})


def adapter_name(task: Task, layout: Layout | None = None) -> str:
    job = job_spec(task)
    if job and job.get("adapter"):
        return job["adapter"]
    lane = cost_lanes(layout).get(task.cost_category.value, "claude_cli")
    role_adapter = role_entry(task, layout).get("adapter")
    if role_adapter and lane in MODEL_LANES:
        return str(role_adapter)           # the role says which provider its agent work runs on
    return lane                            # deterministic / local / human work is not agent work for the role


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
