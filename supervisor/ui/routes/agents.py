from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse

from ... import jobs as jobs_mod
from ... import tasks as tasks_mod
from ...paths import Layout
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render
from ..sse import tail_file

router = APIRouter()

AGENT_ADAPTERS = {"claude_cli", "codex_cli", "claude_api"}


def _run_dir(layout: Layout, rec: dict):
    rd = (rec.get("spec") or {}).get("run_dir")
    return layout.root / rd if rd else None


def _enrich(layout: Layout, rec: dict) -> dict:
    started = (rec.get("handle") or {}).get("started") or rec.get("created")
    elapsed = None
    try:
        from datetime import datetime
        if started:
            elapsed = time.time() - datetime.fromisoformat(started).timestamp()
    except ValueError:
        pass
    run_dir = _run_dir(layout, rec)
    from ... import conversations as conversations_mod
    note_status = conversations_mod.owner_note_status(run_dir) if run_dir else "none"
    return {**rec, "elapsed_s": elapsed, "run_dir": str(run_dir) if run_dir else None,
           "is_agent": rec.get("adapter") in AGENT_ADAPTERS, "owner_note_status": note_status}


@router.get("/agents")
def agents_list(request: Request, layout: Layout = Depends(require_auth)):
    running = [_enrich(layout, r) for r in jobs_mod.open_records(layout)]
    recent = [r for r in jobs_mod.all_records(layout) if r.get("phase") in ("DONE", "FAILED")][-15:]
    recent = [_enrich(layout, r) for r in reversed(recent)]
    return render(request, "agents.html", {"running": running, "recent": recent, "flash": decode_flash(request)})


def _find_job(layout: Layout, attempt_id: str) -> dict:
    try:
        return jobs_mod.read(layout, attempt_id)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="no such attempt")


@router.get("/agents/{attempt_id}")
def agent_detail(attempt_id: str, request: Request, layout: Layout = Depends(require_auth)):
    rec = _enrich(layout, _find_job(layout, attempt_id))
    task = None
    try:
        task = tasks_mod.load(layout, rec["task_id"])
    except tasks_mod.TaskError:
        pass
    return render(request, "agent_detail.html", {"rec": rec, "task": task, "flash": decode_flash(request)})


@router.get("/agents/{attempt_id}/stream")
async def agent_stream(attempt_id: str, request: Request, layout: Layout = Depends(require_auth)):
    rec = _find_job(layout, attempt_id)
    run_dir = _run_dir(layout, rec)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="no run directory for this attempt")
    path = run_dir / "stdout.jsonl"
    live = rec.get("phase") in ("SUBMITTED", "RUNNING")
    return StreamingResponse(tail_file(request, path, from_end=live), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agents/{attempt_id}/terminate")
def terminate(attempt_id: str, request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "agent.terminate", {"attempt_id": attempt_id}, "/agents")


@router.post("/agents/{attempt_id}/note")
def send_note(attempt_id: str, request: Request, text: str = Form(...), csrf_token: str = Form(...),
             layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty note")
    return act(request, layout, "agent.note", {"attempt_id": attempt_id, "text": text}, f"/agents/{attempt_id}")
