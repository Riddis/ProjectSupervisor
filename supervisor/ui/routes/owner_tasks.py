from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ... import owner_tasks as owner_tasks_mod
from ...paths import Layout
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()


@router.get("/owner-tasks")
def owner_tasks_page(request: Request, layout: Layout = Depends(require_auth)):
    rows = owner_tasks_mod.load(layout)
    return render(request, "owner_tasks.html", {"rows": rows, "flash": decode_flash(request)})


@router.post("/owner-tasks/{owner_task_id}/done")
def mark_done(owner_task_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "owner_task.set_status", {"owner_task_id": owner_task_id, "status": "done"}, "/owner-tasks")


@router.post("/owner-tasks/{owner_task_id}/snooze")
def snooze(owner_task_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "owner_task.set_status", {"owner_task_id": owner_task_id, "status": "snoozed"}, "/owner-tasks")


@router.post("/owner-tasks/{owner_task_id}/reopen")
def reopen(owner_task_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "owner_task.set_status", {"owner_task_id": owner_task_id, "status": "open"}, "/owner-tasks")
