"""`GET /live/version`: what an open page polls to learn that the project changed (see supervisor.ui.live)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...paths import Layout
from .. import live
from ..auth import require_auth

router = APIRouter()


@router.get("/live/version")
def live_version(layout: Layout = Depends(require_auth)):
    return JSONResponse({"v": live.fingerprint(layout), "boot": live.BOOT_ID},
                        headers={"Cache-Control": "no-store"})
