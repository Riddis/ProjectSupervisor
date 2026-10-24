"""Needs-you inbox: every open `state/inbox/*.yaml` item grouped by kind, with evidence rendering,
allowed-response buttons, a MODIFY/free-text field, a file drop zone into `uploads/<item id>/`, snooze and an
archive tab. Every write is a command (`inbox.answer`, `inbox.snooze`, `inbox.reopen`, `gate.ack`) — this module
never mutates state/ directly, and never accepts a filesystem path from the browser."""
from __future__ import annotations

import re
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ... import inbox as inbox_mod
from ... import tasks as tasks_mod
from ..._vendor.contract import atomic_json, contained
from ...paths import Layout
from ...store import now_iso
from .. import thumbs
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render
from ..mdlite import render as render_md

router = APIRouter()

KIND_LABELS = {
    "gate": "Gate approvals",
    "decision": "Stopped tasks — your decision",
    "approval": "Delivery approvals",
    "round": "Rounds",
    "spend": "Spend proposals",
    "policy": "Policy escalations",
    "question": "Agent questions",
}
KIND_ORDER = list(KIND_LABELS)
SCORE_KEYS = {"score", "scores", "decision", "reason", "verdict"}
TEXT_EVIDENCE_LIMIT = 4000


def _get_item(layout: Layout, item_id: str) -> dict:
    try:
        return inbox_mod.load(layout, item_id)
    except inbox_mod.InboxError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _age_seconds(item: dict) -> float | None:
    created = item.get("created")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


def _age_display(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _is_score_block(entry, extra_keys=()) -> bool:
    return isinstance(entry, dict) and bool((SCORE_KEYS | set(extra_keys)) & set(entry.keys()))


def _kinds(layout: Layout) -> tuple[dict, list[str], dict]:
    """(labels, order, extension kinds) — the built-in groups plus every kind an extension claimed
    (registry.inbox_kind), in registration order after the built-ins."""
    from ... import extensions as extensions_mod
    labels = dict(KIND_LABELS)
    ext = dict(extensions_mod.load(layout).inbox_kinds)
    for kind, info in ext.items():
        labels[kind] = info.label
    return labels, list(labels), ext


def _evidence_view(layout: Layout, ref: str) -> dict:
    """Evidence is a reference the store wrote (never a path the browser supplies): an image renders as a
    thumbnail, a markdown/text file renders inline (truncated), anything else shows only as its reference."""
    try:
        path = (layout.root / str(ref)).resolve()
    except (OSError, ValueError):
        return {"ref": ref, "kind": "missing"}
    if not contained(path, [layout.root], root=layout.root) or not path.is_file():
        return {"ref": ref, "kind": "missing"}
    if thumbs.is_image(path):
        key = thumbs.ensure(layout, path)
        return {"ref": ref, "kind": "image", "thumb_key": key} if key else {"ref": ref, "kind": "missing"}
    if path.suffix.lower() in (".md", ".txt"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {"ref": ref, "kind": "missing"}
        if len(text) > TEXT_EVIDENCE_LIMIT:
            text = text[:TEXT_EVIDENCE_LIMIT] + "\n…(truncated)"
        return {"ref": ref, "kind": "text", "html": render_md(text)}
    return {"ref": ref, "kind": "link"}


def _item_view(layout: Layout, item: dict, info=None) -> dict:
    """`info` is the extension's InboxKind for the item's kind, if claimed. An extension page owns the detail of its
    items: `show_evidence=False` keeps the per-option rows (and their scores) off this card — the owner sees a count
    and a link to the page — and `answerable=False` replaces the generic form with that link."""
    sub_items = item.get("items") or []
    blind = info is not None and not info.show_evidence
    keys = info.score_keys if info is not None else ()
    return {
        **item,
        "summary_html": render_md(item.get("summary", "")),
        "detail_html": render_md(item["detail"]) if item.get("detail") else "",
        "evidence_views": [_evidence_view(layout, e) for e in (item.get("evidence") or [])],
        "score_items": [] if blind else [i for i in sub_items if _is_score_block(i, keys)],
        "plain_items": [] if blind else [i for i in sub_items if not _is_score_block(i, keys)],
        "item_count": len(sub_items),
        "ext_page": info.page_for(str(item.get("id") or "")) if info is not None else None,
        "ext_answerable": info.answerable if info is not None else True,
        "age_display": _age_display(_age_seconds(item)),
    }


def inbox_badge_count(layout: Layout) -> int:
    return len(inbox_mod.open_items(layout))


@router.get("/inbox")
def inbox_page(request: Request, tab: str = "open", layout: Layout = Depends(require_auth)):
    items = inbox_mod.all_items(layout)
    tab = "archive" if tab == "archive" else "open"
    shown = [i for i in items if (i.get("status") == "open") == (tab == "open")]
    labels, order, ext_kinds = _kinds(layout)
    groups = {k: [] for k in order}
    for item in shown:
        kind = item.get("kind") if item.get("kind") in labels else "question"
        groups[kind].append(_item_view(layout, item, ext_kinds.get(item.get("kind"))))
    return render(request, "inbox.html", {
        "tab": tab, "groups": groups, "kind_labels": labels, "kind_order": order,
        "open_count": sum(1 for i in items if i.get("status") == "open"),
        "archive_count": sum(1 for i in items if i.get("status") != "open"),
        "shown_count": len(shown), "flash": decode_flash(request),
    })


@router.post("/inbox/{item_id}/answer")
def answer_item(item_id: str, request: Request, response: str = Form(""), text: str = Form(""),
                note: str = Form(""), csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    item = _get_item(layout, item_id)
    refusal = inbox_mod.owned_by_extension(layout, item)
    if refusal:                                          # the extension's page is the door; name it in the error
        raise HTTPException(status_code=400, detail=refusal)
    text = text.strip()
    if response and text:
        final = f"{response} {text}"
    elif response:
        final = response
    else:
        final = text
    if not final:
        raise HTTPException(status_code=400, detail="empty response")
    return act(request, layout, "inbox.answer", {"item_id": item_id, "response": final, "note": note}, "/inbox")


@router.post("/inbox/{item_id}/snooze")
def snooze_item(item_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    _get_item(layout, item_id)
    return act(request, layout, "inbox.snooze", {"item_id": item_id}, "/inbox")


@router.post("/inbox/{item_id}/reopen")
def reopen_item(item_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    _get_item(layout, item_id)
    return act(request, layout, "inbox.reopen", {"item_id": item_id}, "/inbox?tab=archive")


@router.post("/inbox/{item_id}/gate-ack")
def gate_ack(item_id: str, request: Request, note: str = Form(""), csrf_token: str = Form(...),
            layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    item = _get_item(layout, item_id)
    gate_id = item.get("gate_id")
    if not gate_id:
        raise HTTPException(status_code=400, detail="item has no gate_id")
    return act(request, layout, "gate.ack", {"gate_id": gate_id, "note": note}, "/inbox")


@router.post("/inbox/{item_id}/upload")
async def upload_file(item_id: str, request: Request, csrf_token: str = Form(...),
                      file: UploadFile = File(...), layout: Layout = Depends(require_auth)):
    """File drop zone: a file the owner hands to an agent in reply to an item lands under `uploads/<item id>/` with
    a JSON sidecar recording where it came from; the agent reads it from there (nothing here interprets it)."""
    require_csrf(request, csrf_token)
    item = _get_item(layout, item_id)
    safe_item = re.sub(r"[^A-Za-z0-9_-]", "", item_id)[:64]
    incoming = layout.uploads / safe_item
    if not safe_item or not contained(incoming, [layout.uploads], root=layout.root):
        raise HTTPException(status_code=400, detail="invalid item id")
    incoming.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload.bin").name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    dest = incoming / safe_name
    data = await file.read()
    dest.write_bytes(data)
    sidecar = {"item_id": item_id, "task_id": item.get("task_id"), "original_filename": file.filename,
              "uploaded_at": now_iso(), "uploaded_by": "owner-ui", "size": len(data), "status": "pending_ingest"}
    atomic_json(dest.with_name(dest.name + ".json"), sidecar)
    tasks_mod.log(layout, "inbox_upload", "owner-ui", item=item_id, file=str(dest.relative_to(layout.root)))
    from fastapi.responses import RedirectResponse

    from ..deps import encode_flash
    flash = encode_flash("applied", f"uploaded {safe_name} to uploads/{safe_item}/")
    return RedirectResponse(url=f"/inbox?flash={flash}", status_code=303)
