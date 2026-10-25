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
    dv = delivery_view.view(layout, t) if t.delivery.digest else None
    return render(request, "task_detail.html", {"t": t, "attempts": attempts, "dv": dv,
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
