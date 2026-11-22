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

from . import capacity, leases, routing_config, runtime, tasks as tasks_mod
from ._vendor.contract import CapacityDeferred, overlaps
from .adapters import REGISTRY
from .models.task import EXCLUSIVE, TERMINAL, Task
from .paths import Layout

MODEL_LANES = ("claude_cli", "codex_cli", "claude_api")
# Lanes that run a worker's own subprocess, so a plain stdout/stderr tail is the only account of a silent failure
# that exists (R-12). Named, not inferred from "not a model lane": a plugin model lane (e.g. an HTTP endpoint) or
# `human` is neither, and must not have its own transcript leaked by the same fallback. A plugin that wants the
# fallback opts in by adding its name here; the default for everything else is no tail.
PLAIN_PROCESS_LANES = ("process", "python")
DEFAULT_LIMITS = {"max_agent_jobs": 2, "max_resource_jobs": 1, "resource_cost_categories": []}
ACTIVE_PHASES = ("SUBMITTED", "RUNNING")


def limits(layout: Layout) -> dict:
    out = dict(DEFAULT_LIMITS)
    lim = dict(routing_config.load(layout).get("limits") or {})
    if "max_gpu_jobs" in lim and "max_resource_jobs" not in lim:      # pre-1.0 name
        lim["max_resource_jobs"] = lim.pop("max_gpu_jobs")
    out.update(lim)
    cats = out.get("resource_cost_categories")
    out["resource_cost_categories"] = [str(c) for c in (cats if isinstance(cats, (list, tuple)) else [cats] if cats else [])]
    return out


def holds_resource(layout: Layout, task: Task | None, adapter_name: str, kind: str = "delivery") -> bool:
    """True when the job takes the project's single resource slot (`limits.max_resource_jobs`): an adapter declared
    `gpu = True` (a renderer, a build farm — the lane itself is the resource), OR the delivery of a task whose
    `cost_category` is listed in routing.yaml `limits.resource_cost_categories`, on any lane. The second case is an
    agent session doing the resource's engineering work itself (installing into it, running it, benchmarking it):
    it runs on a model lane, so the adapter flag cannot know, and two such sessions must not share the one resource.
    The job record's `gpu` flag is set from this same rule (loop.dispatch), so a restarted loop re-adopts the lock."""
    ad = REGISTRY.get(adapter_name)
    if ad is not None and getattr(ad, "gpu", False):
        return True
    if task is None or kind != "delivery":
        return False
    cats = limits(layout)["resource_cost_categories"]
    if not cats:
        return False
    cost = getattr(task, "cost_category", None)
    return str(getattr(cost, "value", cost) or "") in cats


def lease_blocked(layout: Layout, task: Task | None, adapter_name: str, kind: str) -> str | None:
    """None when this job's resource work may launch now as far as the machine-wide lease (R4, docs/plan/LONG_WORK.md
    section 5.4) it would take is concerned; else who holds it. The lease itself is `leases.lease_name_for`'s own
    two-case choice between the slot's `"gpu"` and a claim's own configured name — this peek composes the same two
    facts dispatch will later write onto the record (`holds_resource`, `needs.claim_for`), so the peek and the
    loop's own acquire/renew/reclaim/release sweeps can never disagree about which lease a job takes. A read-only
    peek (`supervisor.leases.holder_label`): the loop takes the lease for real only right before `submit()`
    (`Supervisor.launch`), so a record this refuses stays PREPARED and is retried, exactly as every other gate
    refusal is. This peek is also the only place a loop ever learns another project holds the lease — launch()
    never reaches acquire() when this refuses — so it is also where the loop must record its own wait (R4's
    fairness clause, F2): a live foreign holder makes this project's `release_requested_by` its own, through
    `leases._record_waiting_from_peek()`, so the holder's own processes (its server watchdog) see it and can yield
    early instead of only ever being started on top of."""
    from . import needs as needs_mod
    name = leases.lease_name_for(layout, holds_resource(layout, task, adapter_name, kind),
                                 needs_mod.claim_for(layout, task, kind) if task is not None else None)
    if name is None:
        return None
    label = leases.holder_label(layout, name)
    if label:
        leases._record_waiting_from_peek(layout, name)
    return f"{name} lease held by {label}" if label else None


def routing_doc(layout: Layout | None) -> dict:
    """config/routing.yaml as a mapping; {} when absent or malformed (every reader then uses its default).
    Delegates to `routing_config.load` -- the one place that opens and parses the file (R-28)."""
    return routing_config.load(layout)


def role_config(layout: Layout | None, role: str | None) -> dict:
    """The raw `roles.<role>` entry of routing.yaml, or {} when the role is not configured."""
    roles = routing_doc(layout).get("roles") or {}
    entry = roles.get(role) if isinstance(roles, dict) and role else None
    return entry if isinstance(entry, dict) else {}


def removed_routing_settings(layout: Layout | None) -> list[str]:
    """One line per key `routing_doc()` still carries that R-26 found no reader for — a warning for `doctor`,
    never a refusal: a document carrying one of these keeps loading exactly as it does today; the key is simply
    never consulted. `version` (top-level) was never checked against anything (unlike `job_templates.yaml`'s own
    `version`, which `templates.py` does check). `roles.<role>.active_from` and `roles.<role>.second_opinion` were
    always informational: no scheduler path reads either. `resources.<name>.single_job` is `needs.resources_declared`'s
    own documented exception — read back unexamined, never interpreted as the "one job at a time" rule (that rule
    is `limits.max_resource_jobs` plus the job record's `gpu` flag, untouched; see docs/plan/LONG_WORK.md section 5.1)."""
    doc = routing_doc(layout)
    out = []
    if "version" in doc:
        out.append("routing.yaml: 'version' is no longer read; safe to delete")
    roles = doc.get("roles")
    if isinstance(roles, dict):
        for role, entry in sorted(roles.items()):
            if not isinstance(entry, dict):
                continue
            if "active_from" in entry:
                out.append(f"routing.yaml: roles.{role}.active_from is no longer read; safe to delete")
            if "second_opinion" in entry:
                out.append(f"routing.yaml: roles.{role}.second_opinion is no longer read; safe to delete")
    resources = doc.get("resources")
    if isinstance(resources, dict):
        for name, section in sorted(resources.items()):
            if isinstance(section, dict) and "single_job" in section:
                out.append(f"routing.yaml: resources.{name}.single_job is not read by the framework "
                           f"(limits.max_resource_jobs and a job's own gpu flag already serialise it); safe to delete")
    return out


def role_inactive(layout: Layout | None, role: str | None) -> bool:
    """True when the role is configured `active: false`: a dormant role never launches, on any lane and by any
    path (plan, queued record, review, continuation). An unconfigured role, or one without the key, is active."""
    return role_config(layout, role).get("active", True) is False


def inactive_reason(role: str) -> str:
    return f"role {role} is inactive (routing.yaml roles.{role}.active: false)"


def _preflight_failure_reason(pf) -> str:
    detail = pf.detail or {}
    return f"preflight failed ({detail.get('code', 'unknown')}): {detail.get('message', detail)}"


def preflight_binding(task: Task | None, adapter_name: str, kind: str):
    """The task's own job block, when this launch's preflight rule binds it at all -- `kind == "delivery"`, the
    block's own `adapter` matching this launch's, and that adapter among `PREFLIGHT_ADAPTERS` -- else `None`.
    A verify, review or diagnosis job never runs the task's own stored `job` block (each builds its own spec and
    launches it on whatever adapter that needs, `process` among them), so a task whose own delivery is a model
    lane's (never preflighted, by design: `preflight.attach` skips every adapter outside `PREFLIGHT_ADAPTERS`)
    must not have an unrelated, stale `job.preflight` borrowed and refused the moment one of those launches for
    it. The one guard `preflight_reason` and `why_not_runnable` both apply before reading a stored preflight at
    all, so the two can never bind it differently."""
    if kind != "delivery" or task is None:
        return None
    job = task.job
    if job is None or job.adapter != adapter_name:
        return None
    from . import preflight as preflight_mod
    if adapter_name not in preflight_mod.PREFLIGHT_ADAPTERS:
        return None
    return job


def stored_preflight_reason(layout: Layout, job) -> str | None:
    """None when `job`'s own *stored* preflight (never a freshly run one) clears it to launch; else why not --
    side-effect-free, so this is also safe to call from a 5s poll. The one case it cannot resolve on its own is
    an absent preflight (`pf is None`) while section 3.3's backfill window is still open: that needs a live probe
    against what is on disk (`preflight_reason`'s own next branch), which this never runs, so it returns `None`
    there too -- not a clearance, just "not yet decided by what is stored". Call `preflight_binding` first; this
    assumes the binding already holds."""
    from . import preflight as preflight_mod
    pf = job.preflight
    if pf is None:
        if preflight_mod.backfill_complete(layout):
            return "preflight missing: write the job block again to record one"
        return None
    if not pf.ok:
        return _preflight_failure_reason(pf)
    current = job.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    current.pop("preflight", None)
    if preflight_mod.block_digest(current) != (pf.detail or {}).get("block_digest"):
        return "preflight stale: the job block changed since it was recorded"
    return None


def preflight_reason(layout: Layout, task: Task | None, adapter_name: str, kind: str) -> str | None:
    """None when a `process`/`python` *delivery* job's stored preflight clears it to launch (RECOVERY_PLAN.md
    section 3.3, invariant 7); else why not. `preflight_binding` decides whether this launch's rule binds the
    task's own job block at all; `stored_preflight_reason` decides everything a stored result (or its absence,
    once the backfill window is closed) can decide on its own, with no side effect -- the same two primitives
    `why_not_runnable` reads for its own, read-only classification.

    An absent preflight (`pf is None`) is not always an outright refusal while the window is still open:
    section 3.3's backfill preflights a block that predates the check right here, synchronously, against exactly
    what is on disk, and the result stored before this launch is allowed to proceed -- the loop's own bounded
    sweep (`loop.Supervisor.preflight_backfill`) covers whatever this window does not reach first. Once the sweep
    records that nothing is left pending, the window is closed and `stored_preflight_reason` refuses an absent
    preflight outright, the same as a failed one always has, and this branch is never reached. A failure here is
    stored the same for any task, but opens an owner item only when `task.status` is not `TERMINAL` -- a
    `COMPLETE` task cannot launch again to act on it, and the owner item exists so someone can; see
    `loop.preflight_backfill`'s own docstring for why `CANCELLED` is still swept rather than skipped."""
    job = preflight_binding(task, adapter_name, kind)
    if job is None:
        return None
    reason = stored_preflight_reason(layout, job)
    if reason is not None or job.preflight is not None:
        return reason
    from . import preflight as preflight_mod
    from . import tasks as tasks_mod
    pf = tasks_mod.backfill_job_preflight(layout, task.id)
    if pf is None:
        return "preflight missing: write the job block again to record one"
    if not pf.ok:
        if task.status not in TERMINAL:
            preflight_mod.report_backfill_failure(layout, task, pf)
        return _preflight_failure_reason(pf)
    return None


def needs_mismatch_reason(layout: Layout, task: Task | None, adapter_name: str, kind: str) -> str | None:
    """None unless `task`'s own effective need (docs/plan/LONG_WORK.md section 3.4, D-028 decision 3) is
    refusal-tier against `adapter_name` -- the write-time `needs.match` run again with the adapter this launch is
    actually about to use, since routing can move a task onto a different lane after the write. Scoped to a
    delivery alone (`needs.needs_for`): a review, a verify job or a chat turn is never matched. The one stable
    prefix ("needs mismatch") is what `scheduler.plan`/`why_not_runnable` key their own handling on, never a
    second copy of `needs.match`'s own verdict."""
    from . import needs as needs_mod
    eff = needs_mod.needs_for(layout, task, kind)
    if not eff:
        return None
    refusal = next((r for r in needs_mod.match(layout, task, adapter_name, eff) if r["tier"] == "refusal"), None)
    if refusal is None:
        return None
    return f"needs mismatch on {adapter_name}: {refusal['code']} ({refusal['need']} = {eff.get(refusal['need'])})"


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
          extra_claims: list[tuple[str, str]] = (),
          decision: str | None = None, tasks: dict[str, Task] | None = None, role: str | None = None) -> str | None:
    """None when the job may launch now, else the reason it may not. `running` are the job records that count as
    busy (callers choose: all open records when planning, only SUBMITTED/RUNNING when launching a PREPARED one);
    `extra_*` let the scheduler account for picks made earlier in the same plan -- `extra_claims` is `[(task_id,
    claim), ...]`, the same shape `extra_paths` already uses, so `scheduler.plan()` can never admit two claimants
    of one name in the same tick (docs/plan/LONG_WORK.md section 5.2). `role` is the agent role the job
    runs as (a review's reviewer role, say); for a delivery it defaults to the task's owner role. A role configured
    `active: false` is refused here, so a record queued before the owner disabled the role never launches either.
    A `delivery` whose task is `held` is refused here too (F11), whatever queued it or which lane it is on.
    A job of any kind whose task already reached a terminal status (COMPLETE, CANCELLED) is refused outright
    (R-04): `tasks.set_status` retires every job it knows about in the same breath as the move, so reaching
    this refusal at all means a record it missed, or one dispatched in the gap before that write landed."""
    if task is not None and task.status in TERMINAL:
        return f"task is {task.status.value}"
    decision = decision or runtime.read(layout).get("decision", "RUN")
    if decision != "RUN" and not (decision == "DRAIN" and kind == "verify"):
        # a verify job finishes work already delivered: a drain lets it run so what was delivered is verified
        # before the stop; PAUSE and STOP still hold it
        return f"decision is {decision}"
    if kind == "delivery" and task is not None and task.held:
        # F11: a continuation PREPARED before or during a Hold (requeue() queues it either way, crash-safe) is
        # never launched while `held` is set - the owner's one explicit stop control, durable on the task itself
        # (F9), not undone by the loop's own launch the way `_ensure_in_progress` used to. A diagnosis still runs
        # against a held task (it is how the owner hears the requeue it proposes was declined, diagnosis.py's own
        # `held` read); only the delivery/continuation lane is refused here.
        note = f" ({task.held['note']})" if task.held.get("note") else ""
        return f"held by the owner{note}"
    if kind == "delivery" and task is not None:
        dep = tasks_mod.unmet_dependency(layout, task, tasks)
        if dep:
            # G6, 2.4.0: a revision re-dispatched before the work it depends on is finished. The record stays
            # PREPARED (retried every reconcile()) instead of launching a worker against unfinished dependencies.
            return f"waiting on {dep}"
    if role is None and task is not None and kind in OWNER_ROLE_KINDS:
        role = task.owner_role
    if role and role_inactive(layout, role):
        return inactive_reason(role)
    reason = preflight_reason(layout, task, adapter_name, kind)
    if reason:
        return reason
    needs_reason = needs_mismatch_reason(layout, task, adapter_name, kind)
    if needs_reason:
        return needs_reason
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
    if holds_resource(layout, task, adapter_name, kind) and gpu_busy >= lim["max_resource_jobs"]:
        return f"resource-lock limit {lim['max_resource_jobs']} reached"
    blocked = lease_blocked(layout, task, adapter_name, kind)
    if blocked:
        return blocked
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
    from . import needs as needs_mod
    claim = needs_mod.claim_for(layout, task, kind) if task is not None else None
    if claim:
        # docs/plan/LONG_WORK.md section 5.2: the holder is an open job record carrying this same claim, the rule
        # the `gpu` flag already uses -- modelled on the owned-path overlap clause just above, including its
        # same-task exclusion (a task's own continuation or re-adopted record never blocks itself, §5.2 point 3).
        holder = next((tid for tid, c in extra_claims if c == claim), None)
        if holder is None:
            holder = next((r["task_id"] for r in running
                          if (task is None or r["task_id"] != task.id) and r.get("claim") == claim), None)
        if holder:
            return f"resource {claim!r} claimed by {holder}"
    return None
