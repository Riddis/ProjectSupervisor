from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ... import jobs as jobs_mod
from ... import tasks as tasks_mod
from ...models.task import TaskStatus
from ...paths import Layout
from .. import thumbs
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()


@router.get("/tasks")
def task_list(request: Request, status: str = "", layout: Layout = Depends(require_auth)):
    tasks = sorted(tasks_mod.load_all(layout).values(), key=lambda t: (t.phase, t.id))
    elig_ids = {t.id for t in tasks_mod.eligible(layout, {t.id: t for t in tasks})}
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    return render(request, "tasks_list.html", {"tasks": tasks, "elig_ids": elig_ids, "filter_status": status,
                                               "statuses": [s.value for s in TaskStatus], "flash": decode_flash(request)})


def _get_task(layout: Layout, task_id: str):
    try:
        return tasks_mod.load(layout, task_id)
    except tasks_mod.TaskError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, request: Request, layout: Layout = Depends(require_auth)):
    t = _get_task(layout, task_id)
    jobs_by_id = {r["attempt_id"]: r for r in jobs_mod.for_task(layout, task_id)}
    attempts = [{"attempt": a, "job": jobs_by_id.get(a.id)} for a in t.attempts]
    thumb_keys = {}
    for rel in t.paths:
        key = thumbs.ensure(layout, layout.root / rel)
        if key:
            thumb_keys[rel] = key
    return render(request, "task_detail.html", {"t": t, "attempts": attempts, "thumb_keys": thumb_keys,
                                                "flash": decode_flash(request)})


@router.post("/tasks/{task_id}/attempts/{attempt_id}/open-run-dir")
def open_run_dir(task_id: str, attempt_id: str, request: Request, csrf_token: str = Form(...),
                 layout: Layout = Depends(require_auth)):
    """Opens the attempt's own run directory in the OS file explorer — the client supplies only an attempt id that
    must belong to this task; the real path is resolved server-side from the job record, never from the request."""
    require_csrf(request, csrf_token)
    import os
    t = _get_task(layout, task_id)
    attempt = next((a for a in t.attempts if a.id == attempt_id), None)
    if attempt is None:
        raise HTTPException(status_code=404, detail="no such attempt")
    from fastapi.responses import RedirectResponse

    from ..deps import encode_flash
    recs = [r for r in jobs_mod.for_task(layout, task_id) if r.get("attempt_id") == attempt_id]
    run_dir = ((recs[0].get("spec") or {}).get("run_dir") if recs else None) or attempt.run_dir
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
def approve(task_id: str, request: Request, response: str = Form("ACCEPT"), note: str = Form(""),
            csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    """Owner approval of the CURRENT delivery digest (human_approval=required tasks parked in WAITING_FOR_HUMAN).
    The digest is read server-side from the task, never from the client, so an approval can only ever bind to what
    is on disk now; a stale page approving a superseded digest is refused by tasks.record_approval()."""
    require_csrf(request, csrf_token)
    t = _get_task(layout, task_id)
    if not t.delivery.digest:
        raise HTTPException(status_code=409, detail=f"{task_id} has no delivery to approve")
    if response not in ("ACCEPT", "REJECT", "MODIFY"):
        raise HTTPException(status_code=422, detail="response must be ACCEPT, REJECT or MODIFY")
    return act(request, layout, "task.approve", {"task_id": task_id, "digest": t.delivery.digest, "response": response,
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
    require_csrf(request, csrf_token)
    return act(request, layout, "task.set_status", {"task_id": task_id, "status": "BLOCKED", "note": note or "held by owner"},
              f"/tasks/{task_id}")
