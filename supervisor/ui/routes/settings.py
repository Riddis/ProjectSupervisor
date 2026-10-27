from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ... import settings as settings_mod
from ...paths import Layout
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, layout: Layout = Depends(require_auth)):
    raw = (layout.config / "routing.yaml").read_text(encoding="utf-8") if (layout.config / "routing.yaml").exists() else ""
    doc = settings_mod.load(layout) if raw else {}
    return render(request, "settings.html", {
        "editable": settings_mod.view(layout) if raw else {}, "labels": settings_mod.LABELS,
        "roles": (doc or {}).get("roles") or {},
        "routing_raw": raw, "flash": decode_flash(request),
    })


@router.post("/settings/routing")
def update_routing(request: Request, field: str = Form(...), value: str = Form(...), csrf_token: str = Form(...),
                   layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "settings.update_routing", {"field": field, "value": value}, "/settings")
