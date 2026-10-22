from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ...paths import Layout
from ..auth import require_auth
from ..thumbs import path_for_key

router = APIRouter()


@router.get("/thumb/{key}")
def get_thumb(key: str, layout: Layout = Depends(require_auth)):
    p = path_for_key(layout, key)
    if p is None:
        raise HTTPException(status_code=404, detail="no such thumbnail")
    return FileResponse(p, media_type="image/jpeg")
