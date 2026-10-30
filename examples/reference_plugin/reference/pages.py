"""The reference plugin's Control Center page: list notes, annotate one, close one."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from supervisor import plugin
from supervisor.plugin import ui

router = APIRouter()


@router.get("/reference")
def index(request: Request, layout=Depends(ui.require_auth)):
    items = plugin.owned_items(layout, "reference_note")
    return ui.render(request, "reference.html", {"items": items})


@router.post("/reference/{item_id}/note")
def note(item_id: str, request: Request, text: str = Form(...), form_id: str = Form(""),
         csrf_token: str = Form(""), layout=Depends(ui.require_auth)):
    ui.require_csrf(request, csrf_token)
    try:
        plugin.owned_item(layout, item_id)
    except plugin.InboxError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    outcome = plugin.submit_command(layout, "reference.note", {"item_id": item_id, "text": text},
                                 command_id=form_id or None)       # a form posted twice adds one annotation
    level = "error" if outcome.get("ok") is False else ("pending" if outcome.get("queued") else "applied")
    return RedirectResponse(f"/reference?flash={ui.flash(level, f'note on {item_id}')}", status_code=303)


@router.post("/reference/{item_id}/close")
def close(item_id: str, request: Request, csrf_token: str = Form(""), layout=Depends(ui.require_auth)):
    ui.require_csrf(request, csrf_token)
    return ui.act(request, layout, "reference.close", {"item_id": item_id}, "/reference")
