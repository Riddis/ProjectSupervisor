from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...paths import Layout
from .. import logtail
from ..auth import require_auth
from ..deps import render
from ..sse import tail_file

router = APIRouter()


@router.get("/log")
def log_page(request: Request, layout: Layout = Depends(require_auth)):
    return render(request, "log.html", {"events": list(reversed(logtail.tail(layout, 100)))})


@router.get("/log/stream")
async def log_stream(request: Request, layout: Layout = Depends(require_auth)):
    return StreamingResponse(tail_file(request, layout.log, from_end=True), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
