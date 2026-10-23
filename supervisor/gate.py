"""The one launch gate. Every job — a scheduled delivery, a PREPARED record found by reconcile(), a
revision after a failed verification or review, a capacity continuation, a review, a chat turn — reaches an adapter's
submit() only through `Supervisor.launch()`, and launch() asks `check()` first. One function therefore decides,
for every launch path, whether the owner's decision (RUN only — PAUSE, DRAIN and STOP all refuse), the engine's
measured capacity, the agent/GPU concurrency limits and file ownership allow a job to start now. A refused job stays
PREPARED and is retried every iteration: nothing is dropped, nothing is spent.

Ownership: a delivery job on a model lane always owns something — its declared `owned_paths`, or in their
absence the whole tree (exclusive mode, `owned_paths: ["."]`). Two writing jobs whose ownership overlaps never run
together; reviews (read-only lane) and chat turns own nothing and are never blocked by ownership.
"""
from __future__ import annotations

import yaml

from . import capacity, runtime, tasks as tasks_mod
from ._vendor.contract import CapacityDeferred, overlaps
from .adapters import REGISTRY
from .models.task import EXCLUSIVE, Task
from .paths import Layout

MODEL_LANES = ("claude_cli", "codex_cli", "claude_api")
DEFAULT_LIMITS = {"max_agent_jobs": 2, "max_resource_jobs": 1}
ACTIVE_PHASES = ("SUBMITTED", "RUNNING")


def limits(layout: Layout) -> dict:
    cfg = layout.config / "routing.yaml"
    out = dict(DEFAULT_LIMITS)
    if cfg.exists():
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        lim = dict(data.get("limits") or {})
        if "max_gpu_jobs" in lim and "max_resource_jobs" not in lim:      # pre-1.0 name
            lim["max_resource_jobs"] = lim.pop("max_gpu_jobs")
        out.update(lim)
    return out


def routing_doc(layout: Layout | None) -> dict:
    """config/routing.yaml as a mapping; {} when absent or malformed (every reader then uses its default)."""
    if layout is None:
        return {}
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return {}
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def role_config(layout: Layout | None, role: str | None) -> dict:
    """The raw `roles.<role>` entry of routing.yaml, or {} when the role is not configured."""
    roles = routing_doc(layout).get("roles") or {}
    entry = roles.get(role) if isinstance(roles, dict) and role else None
    return entry if isinstance(entry, dict) else {}


def role_inactive(layout: Layout | None, role: str | None) -> bool:
    """True when the role is configured `active: false`: a dormant role never launches, on any lane and by any
    path (plan, queued record, review, continuation). An unconfigured role, or one without the key, is active."""
    return role_config(layout, role).get("active", True) is False


def inactive_reason(role: str) -> str:
    return f"role {role} is inactive (routing.yaml roles.{role}.active: false)"


# Job kinds whose agent is the task's owner role; a review's agent is its `review_role`, a verify job runs
# deterministic commands and a diagnosis/conversation names its own role in the spec.
OWNER_ROLE_KINDS = ("delivery",)


def owned_for(task: Task | None, adapter_name: str, kind: str) -> list[str]:
    """What this job owns while it runs: only a delivery owns anything; a model lane without declared ownership is
    exclusive by default (the scan and the overlap rule must never be inert)."""
    if task is None or kind != "delivery":
        return []
    if task.owned_paths:
        return list(task.owned_paths)
    return [EXCLUSIVE] if adapter_name in MODEL_LANES else []


def busy_paths(layout: Layout, running: list[dict], tasks: dict[str, Task] | None = None) -> list[tuple[str, str]]:
    """(task_id, owned path) for every running delivery job."""
    out: list[tuple[str, str]] = []
    for r in running:
        spec = r.get("spec") or {}
        if spec.get("kind", "delivery") != "delivery":
            continue
        try:
            task = (tasks or {}).get(r["task_id"]) or tasks_mod.load(layout, r["task_id"])
        except Exception:                                   # noqa: BLE001 - a task we cannot read narrows nothing
            continue
        out += [(r["task_id"], p) for p in owned_for(task, r.get("adapter", ""), "delivery")]
    return out


def check(layout: Layout, task: Task | None, adapter_name: str, kind: str, running: list[dict],
          extra_paths: list[tuple[str, str]] = (), extra_agent: int = 0, extra_gpu: int = 0,
          decision: str | None = None, tasks: dict[str, Task] | None = None, role: str | None = None) -> str | None:
    """None when the job may launch now, else the reason it may not. `running` are the job records that count as
    busy (callers choose: all open records when planning, only SUBMITTED/RUNNING when launching a PREPARED one);
    `extra_*` let the scheduler account for picks made earlier in the same plan. `role` is the agent role the job
    runs as (a review's reviewer role, say); for a delivery it defaults to the task's owner role. A role configured
    `active: false` is refused here, so a record queued before the owner disabled the role never launches either."""
    decision = decision or runtime.read(layout).get("decision", "RUN")
    if decision != "RUN" and not (decision == "DRAIN" and kind == "verify"):
        # a verify job finishes work already delivered: a drain lets it run so what was delivered is verified
        # before the stop; PAUSE and STOP still hold it
        return f"decision is {decision}"
    if role is None and task is not None and kind in OWNER_ROLE_KINDS:
        role = task.owner_role
    if role and role_inactive(layout, role):
        return inactive_reason(role)
    ad = REGISTRY.get(adapter_name)
    if ad is None:
        return f"adapter {adapter_name} not registered"
    engine = getattr(ad, "engine", None)
    if engine:
        try:
            capacity.require(layout, engine)
        except CapacityDeferred as exc:
            return f"capacity: {exc}"
    lim = limits(layout)
    gpu_busy = sum(1 for r in running if r.get("gpu")) + extra_gpu
    agent_busy = sum(1 for r in running if r.get("adapter") in MODEL_LANES) + extra_agent
    if ad.gpu and gpu_busy >= lim["max_resource_jobs"]:
        return f"resource-lock limit {lim['max_resource_jobs']} reached"
    if adapter_name in MODEL_LANES and agent_busy >= lim["max_agent_jobs"]:
        return f"agent limit {lim['max_agent_jobs']} reached"
    mine = owned_for(task, adapter_name, kind)
    if mine:
        others = busy_paths(layout, [r for r in running if task is None or r["task_id"] != task.id], tasks) + list(extra_paths)
        try:
            for other_id, theirs in others:
                if any(overlaps(a, theirs, layout.root) for a in mine):
                    return f"owned paths overlap with {other_id} ({theirs})"
        except ValueError as exc:                        # link in an owned path: refuse, never crash the loop
            return f"owned path error: {exc}"
    return None
