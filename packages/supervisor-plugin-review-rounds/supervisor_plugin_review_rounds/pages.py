"""The review grid's Control Center pages: the sidebar index, one round's grid, its verdict and submit actions.

The grid page carries no upload form and no project-specific identity-folder path - a project with an
`external_edit` request kind of its own (boundary D6, D7) gives it its own navigation entry and route prefix;
this page only ever lists and renders `round` / `labelling` items."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from supervisor import plugin
from supervisor.plugin import ui

from . import (ROUND_KINDS, VERDICTS, RoundError, agreement_stats, canonical_verdict, label_order, load_round,
               progress, recorded_verdict, review_rounds_config)

router = APIRouter()

ZOOM_SIZE = (1024, 1024)


def _get_round(layout: plugin.Layout, item_id: str) -> dict:
    try:
        return load_round(layout, item_id)
    except plugin.InboxError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _thumb(layout: plugin.Layout, ref: str | None, size: tuple[int, int] | None = None) -> str | None:
    if not ref:
        return None
    try:
        path = (layout.root / str(ref)).resolve()
    except (OSError, ValueError):
        return None
    if not ui.is_image(path) or not path.is_file():
        return None
    return ui.thumbnail(layout, path) if size is None else ui.thumbnail(layout, path, size)


def _tile_view(layout: plugin.Layout, row: dict, blind: bool, round_references: list[str] | None, cfg: dict) -> dict:
    refs = row.get("references") or round_references or []
    return {
        "id": row.get("id"),
        "label": row.get("label") or str(row.get("path") or row.get("id")),
        "thumb_key": _thumb(layout, row.get("path")),
        "zoom_key": _thumb(layout, row.get("path"), ZOOM_SIZE),
        "reference_keys": [k for k in (_thumb(layout, r, ZOOM_SIZE) for r in refs) if k],
        "verdict": canonical_verdict(row.get("verdict")),
        "verdict_legacy": recorded_verdict(row),
        "note": row.get("note") or "",
        "reason_tags": row.get("reason_tags") or [],
        "scores": {} if blind else {k: row[k] for k in cfg["tile_score_fields"] if k in row and row[k] is not None},
        "filter_values": {f["field"]: row.get(f["field"]) for f in cfg["filters"]},
    }


@router.get("/review")
def review_index(request: Request, layout: plugin.Layout = Depends(ui.require_auth)):
    """The sidebar destination: every open round and labelling session, with its page."""
    items = [i for i in plugin.owned_items(layout) if i.get("kind") in ROUND_KINDS]
    open_items = [i for i in items if i.get("status") == "open"]
    rows = [{"id": i["id"], "kind": i.get("kind"), "subject": i.get("subject"), "status": i.get("status"),
             "progress": progress(i)} for i in sorted(items, key=lambda x: x["id"])]
    return ui.render(request, "review_index.html", {"rows": rows, "open_count": len(open_items)})


@router.get("/review/{item_id}")
def review_page(item_id: str, request: Request, reveal: str = "0", layout: plugin.Layout = Depends(ui.require_auth)):
    item = _get_round(layout, item_id)
    cfg = review_rounds_config(layout)
    order = label_order(item)                      # read-only: a GET must never mutate state/
    is_labelling = item.get("kind") == "labelling"
    blind = is_labelling and reveal != "1"
    if is_labelling and reveal == "1":
        plugin.log_event(layout, "labelling_reveal", "owner-ui", item=item_id)
    by_id = {str(r.get("id")): r for r in item.get("items") or []}
    round_refs = item.get("references") or []
    tiles = [_tile_view(layout, by_id[i], blind, round_refs, cfg) for i in order if i in by_id]
    answered, total = progress(item)
    references = [k for k in (_thumb(layout, r, ZOOM_SIZE) for r in round_refs) if k]
    # Test-retest agreement is part of what submission reveals - showing it mid-session on an open labelling item
    # would leak exactly the signal blinding is for.
    show_stats = is_labelling and item.get("status") != "open"
    return ui.render(request, "review.html", {
        "item": item, "item_id": item_id, "tiles": tiles, "answered": answered, "total": total,
        "is_labelling": is_labelling, "blind": blind,
        "stats": agreement_stats(item) if show_stats else None,
        "references": references, "reason_tag_options": item.get("reason_tags") or [],
        "filters": cfg["filters"], "verdicts": VERDICTS,
    })


@router.post("/review/{item_id}/verdict")
def verdict_route(item_id: str, request: Request, image_id: str = Form(...), verdict: str = Form(...),
                note: str = Form(""), reason_tags: str = Form(""), reason_tags_set: str = Form(""),
                csrf_token: str = Form(...), layout: plugin.Layout = Depends(ui.require_auth)):
    ui.require_csrf(request, csrf_token)
    _get_round(layout, item_id)
    # A urlencoded body with an empty value (`reason_tags=`) is indistinguishable from the field being absent by
    # the time it reaches a Form() param - Starlette's form parser drops blank values outright - so "clear the
    # tags" needs its own marker. The grid's tag checkboxes always post `reason_tags_set=1` alongside
    # `reason_tags` (even when nothing is checked); a plain round with no tag UI sends neither, so "leave it
    # alone" still works for everything that never had tags to begin with.
    tags = [t.strip() for t in reason_tags.split(",") if t.strip()] if reason_tags_set else None
    outcome = plugin.submit_command(layout, "review.set_verdict",
                                    {"item_id": item_id, "image_id": image_id, "verdict": verdict,
                                     "note": note, "reason_tags": tags}, actor="owner-ui")
    if outcome.get("queued"):
        return JSONResponse({"ok": True, "queued": True, "image_id": image_id, "verdict": verdict})
    if not outcome.get("ok", True):
        raise HTTPException(status_code=400, detail=outcome.get("error", "failed"))
    item = load_round(layout, item_id)
    answered, total = progress(item)
    return JSONResponse({"ok": True, "image_id": image_id, "verdict": verdict, "answered": answered, "total": total})


@router.post("/review/{item_id}/submit")
def submit_round(item_id: str, request: Request, partial: str = Form(""), session_seconds: str = Form(""),
                 csrf_token: str = Form(...), layout: plugin.Layout = Depends(ui.require_auth)):
    ui.require_csrf(request, csrf_token)
    _get_round(layout, item_id)
    payload = {"item_id": item_id, "partial": partial in ("1", "true", "on", "True")}
    if session_seconds:
        try:
            payload["session_seconds"] = float(session_seconds)
        except ValueError:
            pass
    return ui.act(request, layout, "review.submit", payload, "/inbox")
