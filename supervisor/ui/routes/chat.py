"""Chat: threads with a role, grounded in the live state packet; one-click conversion of
a reply into a task/decision/issue/owner task. Every write is a command (`conversation.*`) — this module never
mutates state/ directly, matching every other Control Center route."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ... import conversations as conversations_mod
from ...paths import Layout
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()

CONVERT_TARGETS = ("task", "decision", "issue", "owner_task")


def _thread_or_404(layout: Layout, thread_id: str) -> dict:
    try:
        return conversations_mod.load(layout, thread_id)
    except conversations_mod.ConversationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _roles(layout: Layout) -> list[str]:
    """Addressable roles for a new thread: every active role with a card, excluding the inbox-only `owner` role
    and the `conversation` chat lane itself (its own confinement, not an org-chart role)."""
    import yaml
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return [conversations_mod.DEFAULT_ROLE]
    roles = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("roles") or {}
    out = sorted(r for r, prof in roles.items() if prof.get("active") and r not in ("owner", "conversation"))
    return out or [conversations_mod.DEFAULT_ROLE]


@router.get("/chat")
def chat_list(request: Request, layout: Layout = Depends(require_auth)):
    threads = conversations_mod.list_threads(layout)
    return render(request, "chat.html", {"threads": threads, "thread": None, "roles": _roles(layout),
                                         "flash": decode_flash(request)})


@router.post("/chat")
def chat_create(request: Request, role: str = Form(conversations_mod.DEFAULT_ROLE), csrf_token: str = Form(...),
               layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "conversation.create", {"role": role}, "/chat")


@router.get("/chat/{thread_id}")
def chat_detail(thread_id: str, request: Request, layout: Layout = Depends(require_auth)):
    thread = _thread_or_404(layout, thread_id)
    return render(request, "chat.html", {"threads": conversations_mod.list_threads(layout), "roles": _roles(layout),
                                         "thread_id": thread_id, "thread": thread, "convert_targets": CONVERT_TARGETS,
                                         "flash": decode_flash(request)})


@router.post("/chat/{thread_id}/ask")
def chat_ask(thread_id: str, request: Request, text: str = Form(...), task_ref: str = Form(""),
            decision_ref: str = Form(""), csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    _thread_or_404(layout, thread_id)
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty question")
    payload = {"thread_id": thread_id, "text": text}
    if task_ref.strip():
        payload["task_ref"] = task_ref.strip()
    if decision_ref.strip():
        payload["decision_ref"] = decision_ref.strip()
    return act(request, layout, "conversation.ask", payload, f"/chat/{thread_id}")


@router.post("/chat/{thread_id}/convert")
def chat_convert(thread_id: str, request: Request, turn: int = Form(...), into: str = Form(...),
                 title: str = Form(""), owner_role: str = Form("engineer"), csrf_token: str = Form(...),
                 layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    _thread_or_404(layout, thread_id)
    if into not in CONVERT_TARGETS:
        raise HTTPException(status_code=400, detail=f"unknown conversion target {into!r}")
    fields = {"title": title.strip()} if title.strip() else {}
    if into == "task":
        fields["owner_role"] = owner_role.strip() or "engineer"
    return act(request, layout, "conversation.convert", {"thread_id": thread_id, "turn": turn, "into": into,
                                                          "fields": fields}, f"/chat/{thread_id}")
