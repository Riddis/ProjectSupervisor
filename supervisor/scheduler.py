"""Pick what to dispatch: eligible tasks, in priority order, each admitted by the one launch gate (supervisor.gate):
decision, capacity, one resource-lock holder, N CLI-agent jobs, no overlapping ownership."""
from __future__ import annotations

from . import gate, jobs as jobs_mod, tasks as tasks_mod
from .adapters import REGISTRY
from .models.task import CostCategory, Task
from .paths import Layout

DEFAULT_LIMITS = gate.DEFAULT_LIMITS
MODEL_LANES = gate.MODEL_LANES
# Default adapter per cost category; config/routing.yaml `cost_lanes` overrides any entry (an extension lane for
# `local`, say). A task's own `job.adapter` always wins.
ADAPTER_FOR_COST = {CostCategory.deterministic: "python", CostCategory.local: "python", CostCategory.claude: "claude_cli",
                    CostCategory.codex: "codex_cli", CostCategory.human: "human", CostCategory.mixed: "claude_cli",
                    CostCategory.paid: "claude_api"}


def cost_lanes(layout: Layout | None) -> dict:
    out = {k.value: v for k, v in ADAPTER_FOR_COST.items()}
    if layout is not None:
        cfg = layout.config / "routing.yaml"
        if cfg.exists():
            import yaml
            try:
                data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                data = {}
            out.update({str(k): str(v) for k, v in (data.get("cost_lanes") or {}).items()})
    return out

limits = gate.limits


def job_spec(task) -> dict:
    """The task's `job` block (adapter, fn/argv, params) — an extra field on the task document."""
    extra = getattr(task, "model_extra", None) or {}
    return dict(extra.get("job") or {})


def adapter_name(task: Task, layout: Layout | None = None) -> str:
    job = job_spec(task)
    if job and job.get("adapter"):
        return job["adapter"]
    return cost_lanes(layout).get(task.cost_category.value, "claude_cli")


def writing_lane(task: Task, layout: Layout | None = None) -> bool:
    """A task whose delivery job runs on a model lane — the jobs that must declare ownership."""
    return adapter_name(task, layout) in MODEL_LANES


def plan(layout: Layout, tasks: dict[str, Task] | None = None, unattended: bool = False) -> list[tuple[Task, str]]:
    """Return [(task, adapter_name)] to dispatch now. Every pick passes gate.check() against the open job records
    plus the picks already made in this plan, so the plan can never admit what launch() would refuse.

    `unattended`: a `claude_cli` pick is switched to `claude_api` when the Console org is
    linked and its monthly credit budget is not exhausted, so overnight batches draw API credits instead of the
    owner's interactive window. Interactive runs (the default) never take this branch."""
    tasks = tasks or tasks_mod.load_all(layout)
    running = jobs_mod.open_records(layout)
    running_task_ids = {r["task_id"] for r in running}
    chosen: list[tuple[Task, str]] = []
    chosen_paths: list[tuple[str, str]] = []
    extra_agent = extra_gpu = 0
    for t in tasks_mod.eligible(layout, tasks):
        if t.id in running_task_ids:
            continue
        name = adapter_name(t, layout)
        if unattended and name == "claude_cli" and "claude_api" in REGISTRY:
            from .adapters import claude_api
            if claude_api.credits_remaining(layout):
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
        if REGISTRY[name].gpu:
            extra_gpu += 1
        if name in MODEL_LANES:
            extra_agent += 1
    return chosen
