from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ... import jobs as jobs_mod
from ... import tasks as tasks_mod
from ...models.task import TaskStatus
from ...paths import Layout
from .. import delivery as delivery_view
from .. import thumbs
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()


@router.get("/tasks")
def task_list(request: Request, status: str = "", phase: str = "", layout: Layout = Depends(require_auth)):
    tasks = sorted(tasks_mod.load_all(layout).values(), key=lambda t: (t.phase, t.id))
    elig_ids = {t.id for t in tasks_mod.eligible(layout, {t.id: t for t in tasks})}
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    if phase.isdigit():
        tasks = [t for t in tasks if t.phase == int(phase)]
    return render(request, "tasks_list.html", {"tasks": tasks, "elig_ids": elig_ids, "filter_status": status,
                                               "filter_phase": phase if phase.isdigit() else "",
                                               "broken_tasks": tasks_mod.unreadable(layout),
                                               "statuses": [s.value for s in TaskStatus], "flash": decode_flash(request)})


def _get_task(layout: Layout, task_id: str):
    try:
        return tasks_mod.load(layout, task_id)
    except tasks_mod.TaskError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _job_row(layout: Layout, job_id: str | None, kind: str | None, resource: str | None, run_dir: str | None,
            note: str | None, result: str | None, model: str | None) -> dict:
    """One Attempts-table row for one job, on either `Attempt` shape (ATTEMPT_MODEL.md §2.7): the stored entry
    (an un-migrated `Attempt` itself, or one of a migrated `Attempt`'s `jobs[]`) gives the static fields; the live
    job record, when it still exists, gives the kind as a fallback, the current phase/result and the model
    (§4.6 — `Attempt.model` is not read here, the job record is)."""
    job = jobs_mod.resolve(layout, job_id) if job_id else None
    spec = (job or {}).get("spec") or {}
    return {
        "job_id": job_id, "kind": kind or (jobs_mod.kind_of(job) if job else "delivery"),
        "resource": resource, "note": note or "",
        "run_dir": run_dir or spec.get("run_dir"),
        "model": (job or {}).get("model") or spec.get("model") or model,
        "result": (job or {}).get("result") or result,
        "phase": (job or {}).get("phase"),
    }


def attempt_groups(layout: Layout, t) -> list[dict]:
    """The Attempts table's rows, grouped by delivery attempt, on either shape `t.attempts` can hold: a migrated
    document's `jobs[]` under each numbered attempt (joined on `jobs[].id`), or an un-migrated document's one
    `Attempt` entry per job (joined on `Attempt.id`, used only as the fallback when `jobs[]` is empty — never read
    on a migrated document, which never carries entries in `jobs[]` without `n` set). Each migrated group's `n` is
    the attempt number. An un-migrated entry's id suffix is not one (ATTEMPT_MODEL.md §2.7: "an existing job id
    whose kind is whatever happened to be dispatched nth"), so that group names its job instead of asserting a
    delivery-attempt count (`n: None`, `job_id` set — the design's interim remedy, §6). Each row carries the job's
    own kind either way, so a verification, a review or a diagnosis is never shown as if it were a fresh attempt
    at the deliverable."""
    groups = []
    for a in t.attempts:
        if a.jobs:
            rows = [_job_row(layout, e.get("id"), e.get("kind"), e.get("resource"), e.get("run_dir"),
                             e.get("note"), e.get("result"), None) for e in a.jobs]
            groups.append({"n": a.n, "job_id": None, "opened": a.opened, "outcome": a.outcome, "rows": rows})
        else:
            groups.append({"n": None, "job_id": a.id, "opened": a.at, "outcome": a.outcome,
                          "rows": [_job_row(layout, a.id, None, a.resource, a.run_dir, a.note, None, a.model)]})
    return groups


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, request: Request, layout: Layout = Depends(require_auth)):
    t = _get_task(layout, task_id)
    dv = delivery_view.view(layout, t) if t.delivery.digest else None
    action_reasons = {a: tasks_mod.action_blocked_reason(a, t.status) for a in tasks_mod.PAGE_ACTIONS}
    return render(request, "task_detail.html", {"t": t, "attempt_groups": attempt_groups(layout, t), "dv": dv,
                                                "broken_jobs": jobs_mod.unreadable(layout, task_id),
                                                "action_reasons": action_reasons,
                                                "flash": decode_flash(request)})


@router.get("/tasks/{task_id}/files/{index}")
def task_file(task_id: str, index: int, digest: str = "", layout: Layout = Depends(require_auth)):
    """One file of the task's current delivery, by its position in the task's `paths` and the digest the page was
    shown for — the browser names an index, never a path. Served only while that digest is still the task's and the
    delivered files still hash to it, and only bytes that match the file's manifest entry (read once, then sent).
    Text is served as plain text (never rendered as a page from the project's own origin)."""
    import mimetypes

    from fastapi.responses import Response
    t = _get_task(layout, task_id)
    if not 0 <= index < len(t.paths):
        raise HTTPException(status_code=404, detail="no such delivered file")
    if not digest or digest != t.delivery.digest:
        raise HTTPException(status_code=409, detail="stale link: the task's delivery changed since this page was shown")
    entries = delivery_view.frozen(layout, t, digest)
    if entries is None:
        raise HTTPException(status_code=409, detail="the delivered files changed after this delivery was recorded")
    rel = t.paths[index]
    data = delivery_view.read_frozen(layout, rel, entries)
    if data is None:
        raise HTTPException(status_code=409, detail="this file does not match the delivery (changed, too large or a link)")
    headers = {"X-Content-Type-Options": "nosniff"}
    if thumbs.is_image(Path(rel)):
        return Response(content=data, media_type=mimetypes.guess_type(rel)[0] or "application/octet-stream",
                        headers=headers)
    return Response(content=data, media_type="text/plain; charset=utf-8", headers=headers)


@router.post("/tasks/{task_id}/attempts/{attempt_id}/open-run-dir")
def open_run_dir(task_id: str, attempt_id: str, request: Request, csrf_token: str = Form(...),
                 layout: Layout = Depends(require_auth)):
    """Opens a job's own run directory in the OS file explorer — the client supplies only a job id, of either
    shape (an old flat id or a new `<task>.a<n>.<k><seq>` one), that must belong to this task; the real path is
    resolved server-side from the job entry, never from the request."""
    require_csrf(request, csrf_token)
    import os
    t = _get_task(layout, task_id)
    row = next((r for g in attempt_groups(layout, t) for r in g["rows"] if r["job_id"] == attempt_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="no such attempt")
    from fastapi.responses import RedirectResponse

    from ..deps import encode_flash
    run_dir = row["run_dir"]
    opened = bool(run_dir and os.path.isdir(run_dir) and hasattr(os, "startfile"))
    if opened:
        os.startfile(run_dir)
    flash = encode_flash("applied" if opened else "error", "opened run dir" if opened else "run dir not found")
    return RedirectResponse(url=f"/tasks/{task_id}?flash={flash}", status_code=303)


@router.post("/tasks/{task_id}/priority")
def set_priority(task_id: str, request: Request, priority: int = Form(...), csrf_token: str = Form(...),
                 layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "task.set_priority", {"task_id": task_id, "priority": priority}, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/note")
def add_note(task_id: str, request: Request, note: str = Form(...), csrf_token: str = Form(...),
            layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "task.note", {"task_id": task_id, "note": note}, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/requeue")
def requeue(task_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    t = _get_task(layout, task_id)
    target = "NOT_STARTED" if t.status == TaskStatus.CANCELLED else "READY"
    return act(request, layout, "task.set_status", {"task_id": task_id, "status": target, "note": "re-queued by owner"},
              f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/approve")
def approve(task_id: str, request: Request, digest: str = Form(...), response: str = Form("ACCEPT"),
            note: str = Form(""), csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    """Owner approval of the digest the form was rendered for, not whatever is current when it arrives: the page
    carries its own digest back (R-05), and task.approve refuses when that no longer matches the task's current
    delivery, so a page left open through a newer delivery cannot approve bytes nobody looked at."""
    require_csrf(request, csrf_token)
    t = _get_task(layout, task_id)
    if not t.delivery.digest:
        raise HTTPException(status_code=409, detail=f"{task_id} has no delivery to approve")
    if response not in ("ACCEPT", "REJECT", "MODIFY"):
        raise HTTPException(status_code=422, detail="response must be ACCEPT, REJECT or MODIFY")
    return act(request, layout, "task.approve", {"task_id": task_id, "digest": digest, "response": response,
                                                "note": note}, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: str, request: Request, note: str = Form(""), csrf_token: str = Form(...),
          layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "task.set_status", {"task_id": task_id, "status": "CANCELLED", "note": note or "cancelled by owner"},
              f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/hold")
def hold(task_id: str, request: Request, note: str = Form(""), csrf_token: str = Form(...),
        layout: Layout = Depends(require_auth)):
    """`task.hold`, not the generic `task.set_status`: a Hold on a task the escalation already moved to BLOCKED
    must still leave a durable record (round-4 F9) - `task.set_status` with the same target status is a silent
    `_NoChange`, with no history entry and no durable identity the automatic path can tell apart from the cap
    event's own BLOCKED."""
    require_csrf(request, csrf_token)
    return act(request, layout, "task.hold", {"task_id": task_id, "note": note or "held by owner"},
              f"/tasks/{task_id}")
