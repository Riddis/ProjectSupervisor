from __future__ import annotations

import shutil
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request

from ... import capacity, runtime
from ... import gate as gate_mod
from ... import gates as gates_mod
from ... import shutdown as shutdown_mod
from ... import inbox as inbox_mod
from ... import jobs as jobs_mod
from ... import tasks as tasks_mod
from ... import extensions as extensions_mod
from ... import project as project_mod
from ...models.task import TaskStatus
from ...paths import Layout
from .. import logtail
from ..auth import require_auth
from ..deps import act, decode_flash, render

router = APIRouter()

def _panels(layout: Layout) -> list[dict]:
    """Project-specific resource panels (extensions register `overview_panel`); a crashing panel shows its error."""
    out = []
    for fn in extensions_mod.load(layout).overview_panels:
        try:
            panel = fn(layout) or {}
            out.append({"title": str(panel.get("title") or getattr(fn, "__name__", "panel")),
                        "rows": [(str(k), str(v)) for k, v in (panel.get("rows") or [])],
                        "progress": panel.get("progress")})
        except Exception as exc:                            # noqa: BLE001 - never take the overview down
            out.append({"title": getattr(fn, "__name__", "panel"), "rows": [("error", str(exc)[:160])], "progress": None})
    return out


def _disk(layout: Layout) -> dict:
    try:
        usage = shutil.disk_usage(layout.root.anchor or str(layout.root))
        return {"total_gb": usage.total / 1e9, "free_gb": usage.free / 1e9, "used_gb": usage.used / 1e9}
    except OSError:
        return {}


def overview_context(layout: Layout) -> dict:
    tasks = tasks_mod.load_all(layout)
    gate_data = gates_mod.load(layout)
    elig = tasks_mod.eligible(layout, tasks)
    phases_total = len(gate_data.get("phases") or {})
    phases_passed = sorted({g.get("phase", -1) for g in (gate_data.get("gates") or {}).values() if g.get("status") == "PASSED"})
    current_phase = min((t.phase for t in tasks.values() if t.status != TaskStatus.COMPLETE), default=0)
    rt = runtime.read(layout)
    live = runtime.is_live(layout)
    open_inbox = inbox_mod.open_items(layout)
    waiting = [t for t in tasks.values() if t.status == TaskStatus.WAITING_FOR_HUMAN]
    needs_you = len(open_inbox) + len(waiting)
    cap = capacity.windows(layout)
    now = time.time()
    open_jobs = jobs_mod.open_records(layout)
    busy_attempt = rt.get("busy")
    active_job = next((job for job in open_jobs if job.get("attempt_id") == busy_attempt), None)
    active_job = active_job or next(iter(open_jobs), {})
    busy_task = tasks.get(active_job.get("task_id"))
    today_events = [e for e in logtail.tail(layout, 500) if str(e.get("ts", "")).startswith(time.strftime("%Y-%m-%d"))]
    job_counts: dict[str, int] = {}
    for e in today_events:
        if e.get("event") in ("job_submitted", "job_done"):
            job_counts[e.get("adapter") or "?"] = job_counts.get(e.get("adapter") or "?", 0) + 1
    heartbeat_age = max(0, int(now - rt["heartbeat_epoch"])) if rt.get("heartbeat_epoch") else None
    uptime_seconds = None
    if live and rt.get("started"):
        try:
            uptime_seconds = max(0, int(now - datetime.fromisoformat(rt["started"]).timestamp()))
        except (ValueError, TypeError):
            pass
    labels = project_mod.phase_labels(layout)
    all_gates = [dict(row, id=key, label=labels.get(row.get("phase"), "Phase gate"))
                 for key, row in (gate_data.get("gates") or {}).items()]
    nearby_gates = [g for g in all_gates if g.get("phase", 0) >= current_phase][:5]
    active_jobs = [j for j in open_jobs if j.get("phase") in gate_mod.ACTIVE_PHASES and j.get("adapter") != "human"]
    draining = live and rt.get("decision") == "DRAIN"
    return {
        "draining": draining, "active_jobs": active_jobs, "shutdown_armed": bool(rt.get("shutdown_on_drain")),
        "shutdown_pending_s": shutdown_mod.pending(layout),
        "phase": current_phase, "phase_label": labels.get(current_phase, "Project progress"),
        "project_name": project_mod.name(layout), "panels": _panels(layout),
        "phases_total": phases_total, "phases_passed": phases_passed,
        "eligible": elig, "nearby_gates": nearby_gates, "heartbeat_age": heartbeat_age,
        "uptime_seconds": uptime_seconds, "busy_task": busy_task, "runtime": rt, "live": live, "needs_you": needs_you,
        "open_inbox_count": len(open_inbox), "waiting_count": len(waiting),
        "capacity": cap, "now": now, "disk": _disk(layout),
        "open_jobs": open_jobs, "job_counts": job_counts, "last_events": logtail.tail(layout, 10),
        "task_count": len(tasks), "done_count": sum(1 for t in tasks.values() if t.status == TaskStatus.COMPLETE),
    }


@router.get("/overview")
def overview(request: Request, layout: Layout = Depends(require_auth)):
    ctx = overview_context(layout)
    ctx["flash"] = decode_flash(request)
    return render(request, "overview.html", ctx)


def _control(kind: str):
    def handler(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
        from ..auth import require_csrf
        require_csrf(request, csrf_token)
        return act(request, layout, kind, {}, "/overview")
    return handler


@router.post("/overview/start")
def start_supervisor(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    import subprocess
    import sys

    from ..auth import require_csrf

    require_csrf(request, csrf_token)
    mismatch = project_mod.version_mismatch(layout)
    if runtime.is_live(layout):
        level, message = "applied", "supervisor already running"
    elif mismatch:
        level, message = "error", mismatch                 # never start a loop on state the new code may not understand
    else:
        import os

        log_path = layout.runtime / "supervisor_ui_launch.log"
        with open(log_path, "ab") as out:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            env = {**os.environ, "SUPERVISOR_ROOT": str(layout.root)}  # `supervisor run` otherwise always targets the
            subprocess.Popen([sys.executable, "-m", "supervisor", "run"], cwd=str(layout.root), stdout=out, stderr=out,
                             stdin=subprocess.DEVNULL, creationflags=flags, env=env)
        level, message = "applied", "supervisor starting (see state/runtime/supervisor_ui_launch.log)"
    if request.headers.get("HX-Request"):
        return render(request, "partials/action_result.html", {"level": level, "message": message, "outcome": {}})
    from fastapi.responses import RedirectResponse

    from ..deps import encode_flash
    return RedirectResponse(url=f"/overview?flash={encode_flash(level, message)}", status_code=303)


router.add_api_route("/overview/pause", _control("loop.pause"), methods=["POST"])
router.add_api_route("/overview/resume", _control("loop.resume"), methods=["POST"])
router.add_api_route("/overview/stop", _control("loop.stop"), methods=["POST"])
router.add_api_route("/overview/reload", _control("loop.reload"), methods=["POST"])


@router.post("/overview/shutdown")
def shutdown_when_idle(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    """"Shut down PC when idle": with a live supervisor, drain it with the power-off armed — running jobs
    finish and are verified first; with no supervisor running, nothing is in flight, so power off after the grace
    period right away. Either way the owner can cancel from the same control row."""
    from ..auth import require_csrf
    require_csrf(request, csrf_token)
    if runtime.is_live(layout):
        return act(request, layout, "loop.drain", {"shutdown": True}, "/overview")
    return act(request, layout, "system.shutdown", {}, "/overview")


@router.post("/overview/shutdown-cancel")
def shutdown_cancel(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    """Cancel a drain-and-shutdown (supervisor resumes) or an OS power-off already counting down."""
    from ..auth import require_csrf
    require_csrf(request, csrf_token)
    if runtime.is_live(layout) and runtime.read(layout).get("decision") == "DRAIN":
        return act(request, layout, "loop.resume", {}, "/overview")
    return act(request, layout, "system.shutdown_cancel", {}, "/overview")


@router.post("/overview/doctor")
def run_doctor(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    from ... import doctor as doctor_mod
    from ..auth import require_csrf
    require_csrf(request, csrf_token)
    checks = doctor_mod.run(layout)
    ctx = overview_context(layout)
    ctx["doctor_checks"] = checks
    ctx["flash"] = {"level": "applied" if all(c.ok for c in checks) else "error",
                    "message": f"doctor: {sum(c.ok for c in checks)}/{len(checks)} checks passed"}
    if request.headers.get("HX-Request"):
        return render(request, "partials/doctor_result.html", ctx)
    return render(request, "overview.html", ctx)


@router.post("/overview/capacity-reset")
def capacity_reset(request: Request, engine: str = Form(...), csrf_token: str = Form(...),
                   layout: Layout = Depends(require_auth)):
    from ..auth import require_csrf
    require_csrf(request, csrf_token)
    return act(request, layout, "capacity.reset", {"engine": engine, "message": "owner reset via Control Center"}, "/overview")
