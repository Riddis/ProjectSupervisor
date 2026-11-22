"""Declared task needs, matched against declared lane capability data (docs/plan/LONG_WORK.md sections 3-4,
D-028 decisions 1-4, 6, 7, 11). One pure-data matcher, called from every reader that needs to know whether a
declaration and a lane agree -- the write-time checks (`tasks.check_new`/`import_seed`/`patch_task`), the launch
gate's re-match (`gate.check`), `why_not_runnable`, and every `prepare()` that resolves a budget -- so the two can
never drift into two different answers for the same question.

`match()` takes no `kind`: a write is about the contract's own work and has no job record to scope by, so every
write-time caller passes the delivery's effective need unconditionally. `applies_to`/`needs_for` carry the
delivery-only scoping (section 3.5) for the launch-side callers that do have a kind to scope by.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .paths import Layout

if TYPE_CHECKING:
    from .models.task import Task

VOCABULARY = ("duration_min", "network", "resource", "resumable")


def applies_to(kind: str) -> bool:
    """True for a delivery alone (section 3.5): a review, a verify job and a chat turn are matched against
    nothing, claim nothing and derive no budget -- the runtime copies the task's own contract (and its `needs`)
    onto each of those, so without this scoping a correct declaration would stall or misbudget its own review."""
    return kind == "delivery"


def _template_needs(layout: Layout, job) -> dict:
    if job is None or not job.template:
        return {}
    from . import templates as templates_mod
    try:
        tmpl = templates_mod.load(layout).get(str(job.template)) or {}
    except templates_mod.TemplateError:
        return {}
    needs = tmpl.get("needs")
    return dict(needs) if isinstance(needs, dict) else {}


def template_declares_resumable(layout: Layout, task: "Task") -> bool:
    """The template's own `resumable: true` sibling key (section 3.2) -- a capability of the *recipe*, never a
    key inside the template's `needs` block, which stays exactly the `{duration_min, network, resource}` shape
    RECOVERY_PLAN.md section 3.2 fixed."""
    job = getattr(task, "job", None)
    if job is None or not job.template:
        return False
    from . import templates as templates_mod
    try:
        tmpl = templates_mod.load(layout).get(str(job.template)) or {}
    except templates_mod.TemplateError:
        return False
    return tmpl.get("resumable") is True


def effective(task_needs: dict | None, template_needs: dict | None) -> dict:
    """The one need a job actually carries (section 3.1): `duration_min` the greater of the two; `network` either;
    `resumable` the task's own (a template declares a capability, not a need); `resource` whichever of the two is
    set, or a recorded conflict when both are set and differ (RF-33)."""
    task_needs, template_needs = task_needs or {}, template_needs or {}
    out: dict[str, Any] = {}
    durations = [v for v in (task_needs.get("duration_min"), template_needs.get("duration_min")) if v is not None]
    if durations:
        out["duration_min"] = max(durations)
    networks = [v for v in (task_needs.get("network"), template_needs.get("network")) if v is not None]
    if networks:
        out["network"] = any(networks)
    if task_needs.get("resumable") is not None:
        out["resumable"] = task_needs["resumable"]
    task_resource, tmpl_resource = task_needs.get("resource"), template_needs.get("resource")
    if task_resource and tmpl_resource and task_resource != tmpl_resource:
        out["resource_conflict"] = (task_resource, tmpl_resource)
    elif task_resource or tmpl_resource:
        out["resource"] = task_resource or tmpl_resource
    return out


def effective_for(layout: Layout, task: "Task") -> dict:
    """`effective()` for `task`'s own delivery: its `needs` block and its job's template's, resolved from the
    store (section 3.1) -- the one place the template is read, so every caller composes the same need."""
    task_needs = task.needs.model_dump(exclude_none=True) if task.needs else {}
    return effective(task_needs, _template_needs(layout, task.job))


def needs_for(layout: Layout, task: "Task | None", kind: str) -> dict | None:
    """The effective need `kind` is matched/budgeted against: `effective_for` for a delivery, `None` for every
    other kind (section 3.5) -- a review, a verify job or a chat turn never derives a budget and never matches."""
    if task is None or not applies_to(kind):
        return None
    return effective_for(layout, task)


def match(layout: Layout, task: "Task", adapter: str, effective_need: dict) -> list[dict]:
    """[{code, tier, need, message}] for `effective_need` against `adapter`'s capability data (section 3.3): a
    "refusal" tier entry is what a write refuses (or the gate re-match defers on); a "warning" tier entry never
    refuses anything. Pure data: two dict lookups and an integer comparison, no probe, no `kind` -- a write is
    about the contract's own work, which is always the delivery's own need."""
    from . import capabilities as capabilities_mod
    from . import timeouts as timeouts_mod
    out: list[dict] = []
    if effective_need.get("resource_conflict"):
        a, b = effective_need["resource_conflict"]
        out.append({"code": "resource_conflict", "tier": "refusal", "need": "resource",
                    "message": f"needs.resource on the task ({a!r}) and on its job's template ({b!r}) differ"})
    caps = capabilities_mod.for_lane(layout, adapter)
    if not caps:
        if any(k in effective_need for k in VOCABULARY):
            out.append({"code": "needs_lane_unknown", "tier": "warning", "need": None,
                        "message": f"{adapter!r} declares no capability data; the declaration is recorded but "
                                   "not matched against it"})
        return out
    duration_min = effective_need.get("duration_min")
    if duration_min is not None:
        ceiling = timeouts_mod.ceiling_for(layout, task, adapter)
        if duration_min * 60 > ceiling:
            out.append({"code": "needs_exceed_lane", "tier": "refusal", "need": "duration_min",
                        "message": f"needs.duration_min ({duration_min} min) exceeds {adapter!r}'s ceiling "
                                   f"({ceiling // 60} min)"})
    resumable = effective_need.get("resumable")
    if resumable:
        durable = caps.get("durable")
        has_template = bool(getattr(task, "job", None) and task.job.template)
        if durable is False:
            out.append({"code": "needs_unsupported", "tier": "refusal", "need": "resumable",
                        "message": f"needs.resumable: true against {adapter!r}, which declares durable: false"})
        elif has_template and not template_declares_resumable(layout, task):
            out.append({"code": "needs_unsupported", "tier": "refusal", "need": "resumable",
                        "message": "needs.resumable: true but the job's template does not declare resumable: true"})
        elif durable is None:
            out.append({"code": "needs_unverifiable", "tier": "warning", "need": "resumable",
                        "message": f"{adapter!r} declares no durable key; needs.resumable recorded but unverified"})
    network = effective_need.get("network")
    if network:
        if "network" not in caps:
            out.append({"code": "needs_unverifiable", "tier": "warning", "need": "network",
                        "message": f"{adapter!r} declares no network key; needs.network recorded but unverified"})
        elif caps.get("network") is False:
            out.append({"code": "needs_unsupported", "tier": "refusal", "need": "network",
                        "message": f"needs.network: true against {adapter!r}, which declares network: false"})
    return out


def results_for(layout: Layout, task: "Task | None", job_block: dict | None = None) -> list[dict]:
    """`match()` for the work a write is about to create, against the lane it would run on -- the one entry point
    every write-time caller goes through (section 3.3), so none of them composes the effective need, the lane or
    the ceiling privately.

    With `job_block` given it is *that* block, not the one the task carries, that the template half of the
    effective need and the lane are read from: the owner's `task.set_job` and a plan's `set_job` both replace the
    block inside the same write the preflight runs in (section 3.3's second and third rows), and a plan's
    `set_job` on a task it creates in the same plan has no stored task to read at all."""
    from . import scheduler as scheduler_mod
    from .models.task import Job
    job = None
    if job_block is not None:
        try:
            job = Job.model_validate({k: v for k, v in dict(job_block).items() if k != "preflight"})
        except Exception:                        # noqa: BLE001 - an uncanonicalisable block is its own refusal
            return []
    subject = task if job is None else (None if task is None else task.model_copy(update={"job": job}))
    if subject is not None:
        return match(layout, subject, scheduler_mod.adapter_name(subject, layout), effective_for(layout, subject))
    return match(layout, None, (job.adapter if job is not None else "") or "",
                 effective({}, _template_needs(layout, job)))


def write_check(layout: Layout, task: "Task") -> tuple[list[str], list[str]]:
    """(errors, warnings) for `task`'s own effective need against the lane it would run on -- called at every
    write that creates or changes a task's contract or job (section 3.3): a refusal-tier entry refuses the row,
    the patch or the plan it came through; a warning-tier entry never refuses anything."""
    results = results_for(layout, task)
    errors = [r["message"] for r in results if r["tier"] == "refusal"]
    warnings = [r["message"] for r in results if r["tier"] == "warning"]
    return errors, warnings


def cleared_warning(before: dict, incoming: dict) -> list[str]:
    """`needs_cleared` (section 2.3): an import row or a patch that clears a non-empty `needs` block explicitly --
    the only way to drop one, since `needs` is sticky across a whole-contract refresh (section 3.1)."""
    if "needs" not in incoming:
        return []
    if (before.get("needs") or None) and not (incoming.get("needs") or None):
        return ["needs_cleared: the task's needs block was explicitly cleared"]
    return []
