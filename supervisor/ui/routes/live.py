"""`GET /live/version`: what an open page polls to learn that the project changed (see supervisor.ui.live).
`GET /commands/<id>/status`: what an action result still marked `data-pending` polls, so "queued" becomes applied
or failed once the loop has processed the command (R-19) instead of sitting there until the page is reloaded."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ... import commands as commands_mod
from ...paths import Layout
from .. import live
from ..auth import require_auth

router = APIRouter()


@router.get("/live/version")
def live_version(layout: Layout = Depends(require_auth)):
    return JSONResponse({"v": live.fingerprint(layout), "boot": live.BOOT_ID},
                        headers={"Cache-Control": "no-store"})


@router.get("/commands/{command_id}/status")
def command_status(command_id: str, layout: Layout = Depends(require_auth)):
    status = commands_mod.status_of(layout, command_id)
    if status is None:
        status = {"id": command_id, "ok": False, "error": "no such command", "pending": False}
    return JSONResponse(status, headers={"Cache-Control": "no-store"})
