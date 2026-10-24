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
from ... import timeouts as timeouts_mod
from ... import extensions as extensions_mod
from ... import project as project_mod
from ... import updates as updates_mod
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


# What each kind of job is doing, in the owner's words (spec.kind; a missing kind is a delivery).
JOB_ACTIVITY = {"delivery": "Working", "review": "Reviewing", "diagnosis": "Diagnosing a failure",
                "conversation": "Answering chat", "verify": "Verifying"}


def _iso_epoch(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp() if value else None
    except (ValueError, TypeError):
        return None


def now_running(open_jobs: list[dict], tasks: dict, now: float) -> list[dict]:
    """One row per open, non-human job for the Overview's "Now running" panel: which task, what the job is doing,
    who is doing it, and how long it has been at it against its timeout."""
    rows = []
    for rec in open_jobs:
        if rec.get("adapter") == "human":                    # owner work shows under "Needs your attention"
            continue
        spec = rec.get("spec") or {}
        handle = rec.get("handle") or {}
        kind = spec.get("kind") or "delivery"
        task = tasks.get(rec.get("task_id"))
        started = _iso_epoch(handle.get("started") or rec.get("submitted"))
        elapsed = max(0, int(now - started)) if started and rec.get("phase") != "PREPARED" else None
        timeout = spec.get("timeout_s")
        deadline, idle_s = handle.get("deadline"), spec.get("idle_s")
        if deadline is not None:                              # what the loop enforces (legacy jobs: current policy)
            deadline, idle_s, _ = timeouts_mod.effective_limits(rec.get("adapter"), spec, handle)
        if started and isinstance(deadline, (int, float)) and deadline != float("inf"):
            timeout = max(0, int(deadline - started))         # the hard cap, including any Extend clicks
        idle_for = None
        if rec.get("phase") in ("SUBMITTED", "RUNNING") and idle_s and spec.get("run_dir"):
            last = timeouts_mod.read_activity(spec["run_dir"])
            idle_for = max(0, int(now - last)) if last else None
        deferred = float(rec.get("deferred_until") or 0)
        if rec.get("phase") == "PREPARED" and deferred > now:
            state, state_note = "deferred", rec.get("deferred_reason") or "waiting to launch"
        elif rec.get("phase") == "PREPARED":
            state, state_note = "launching", "about to launch"
        elif rec.get("phase") == "SUBMITTED":
            state, state_note = "starting", "launched, no output yet"
        else:
            state, state_note = "running", ""
        attempt_no = str(rec.get("attempt_id", "")).rpartition(".a")[2]
        if kind == "conversation":
            title, href = "Chat reply", f"/chat/{rec.get('conversation_thread')}" if rec.get("conversation_thread") else "/chat"
        else:
            title, href = (task.title if task else rec.get("task_id") or "Unknown task"), f"/tasks/{rec.get('task_id')}"
        rows.append({
            "attempt_id": rec.get("attempt_id"), "task_id": rec.get("task_id"), "title": title, "href": href,
            "is_chat": kind == "conversation", "activity": JOB_ACTIVITY.get(kind, kind.replace("_", " ").capitalize()),
            "role": rec.get("review_role") or spec.get("role"), "model": spec.get("model"), "effort": spec.get("effort"),
            "adapter": rec.get("adapter"), "attempt": int(attempt_no) if attempt_no.isdigit() else None,
            "state": state, "state_note": state_note, "elapsed_s": elapsed, "timeout_s": timeout,
            "idle_s": idle_s, "idle_for_s": idle_for,
            "extended_s": sum(int(e.get("seconds") or 0) for e in handle.get("extensions") or []),
            "can_extend": rec.get("phase") in ("SUBMITTED", "RUNNING") and deadline is not None,
            "retry_in_s": int(deferred - now) if state == "deferred" else None,
            "gpu": bool(rec.get("gpu")),
        })
    order = {"running": 0, "starting": 1, "launching": 2, "deferred": 3}
    return sorted(rows, key=lambda r: (order[r["state"]], -(r["elapsed_s"] or 0)))


def controls(live: bool, rt: dict) -> list[dict]:
    """The Supervisor card's control row. Nothing is highlighted: a button is enabled only when its action makes
    sense in the current state, and a disabled one says why on hover. A decision the loop has not applied yet
    (Pause clicked, status still RUNNING) already counts, so the button flips at once."""
    status = rt.get("status") if live else "STOPPED"
    decision = rt.get("decision") or "RUN"
    paused = live and (status == "PAUSED" or decision == "PAUSE")
    draining = live and (status == "DRAINING" or decision == "DRAIN")
    stopping = live and decision in ("STOP", "RELOAD")
    offline = "The supervisor is not running"
    rows = [
        ("start", "Start", "play", not live, "Start the supervisor", "Already running" if live else ""),
        ("pause", "Pause", "pause", live and not paused and not draining and not stopping,
         "Stop dispatching new work; running jobs finish", offline if not live else
         "Already paused" if paused else "Draining: cancel the drain with Resume" if draining else "Stopping"),
        ("resume", "Resume", "play", live and (paused or draining) and not stopping,
         "Cancel the drain and keep dispatching" if draining else "Dispatch work again",
         offline if not live else "Stopping" if stopping else "Already running"),
        ("stop", "Stop", "stop", live and decision != "STOP", "Stop the supervisor",
         offline if not live else "Stopping"),
        ("reload", "Reload", "reload", live and not stopping, "Restart the supervisor on the current code",
         offline if not live else "Stopping"),
        ("doctor", "Run doctor", "doctor", True, "Check the installation and project state", ""),
    ]
    return [{"action": a, "label": label, "icon": ic, "enabled": bool(on), "title": hint if on else why}
            for a, label, ic, on, hint, why in rows]


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
    uptime_seconds, started_local = None, None
    if live and rt.get("started"):
        try:
            started_at = datetime.fromisoformat(rt["started"])
            uptime_seconds = max(0, int(now - started_at.timestamp()))
            started_local = started_at.astimezone().strftime("%H:%M" if uptime_seconds < 86400 else "%d %b %H:%M")
        except (ValueError, TypeError):
            pass
    labels = project_mod.phase_labels(layout)
    all_gates = [dict(row, id=key, label=labels.get(row.get("phase"), "Phase gate"))
                 for key, row in (gate_data.get("gates") or {}).items()]
    nearby_gates = [g for g in all_gates if g.get("phase", 0) >= current_phase][:5]
    active_jobs = [j for j in open_jobs if j.get("phase") in gate_mod.ACTIVE_PHASES and j.get("adapter") != "human"]
    draining = live and rt.get("decision") == "DRAIN"
    try:
        update = updates_mod.check(layout, fetch=False)       # cached by the background checker; cheap otherwise
    except Exception:                                       # noqa: BLE001
        update = None
    running_rows = now_running(open_jobs, tasks, now)
    running_ids = {r["task_id"] for r in running_rows}
    waiting_to_start = [t for t in elig if t.id not in running_ids]
    return {
        "now_running": running_rows, "up_next": waiting_to_start[:3], "eligible_count": len(waiting_to_start),
        "human_jobs": sum(1 for j in open_jobs if j.get("adapter") == "human"),
        "controls": controls(live, rt), "paused": live and (rt.get("status") == "PAUSED" or rt.get("decision") == "PAUSE"),
        "update": update, "update_lines": updates_mod.describe(update) if update else [],
        "draining": draining, "active_jobs": active_jobs, "shutdown_armed": bool(rt.get("shutdown_on_drain")),
        "shutdown_pending_s": shutdown_mod.pending(layout),
        "phase": current_phase, "phase_label": labels.get(current_phase, "Project progress"),
        "project_name": project_mod.name(layout), "panels": _panels(layout),
        "phases_total": phases_total, "phases_passed": phases_passed,
        "eligible": elig, "nearby_gates": nearby_gates, "heartbeat_age": heartbeat_age,
        "uptime_seconds": uptime_seconds, "started_local": started_local, "busy_task": busy_task, "runtime": rt, "live": live, "needs_you": needs_you,
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


@router.get("/overview/live")
def overview_live(request: Request, layout: Layout = Depends(require_auth)):
    """The Overview's self-refreshing parts (polled every few seconds): the "Now running" panel, plus the
    Supervisor card's status, details and control row swapped out-of-band."""
    return render(request, "partials/overview_live.html", overview_context(layout))


def _control(kind: str):
    def handler(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
        from ..auth import require_csrf
        require_csrf(request, csrf_token)
        return act(request, layout, kind, {}, "/overview")
    return handler


@router.post("/overview/start")
def start_supervisor(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    """Start the loop through `supervisor.launcher`, the restart-on-reload wrapper, so a Reload or a framework
    update (exit 3) brings it back on the new code just as the console launchers do."""
    from ... import launcher
    from ..auth import require_csrf

    require_csrf(request, csrf_token)
    mismatch = project_mod.version_mismatch(layout)
    if runtime.is_live(layout):
        level, message = "applied", "supervisor already running"
    elif mismatch:
        level, message = "error", mismatch                 # never start a loop on state the new code may not understand
    else:
        launcher.spawn(layout)
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


@router.post("/overview/update")
def apply_update(request: Request, csrf_token: str = Form(...), layout: Layout = Depends(require_auth)):
    """Apply the framework update the banner showed: pull, reinstall, migrate, doctor, reload the loop, then restart
    this Control Center process (exit 3; the launcher starts it again on the new code)."""
    from ..auth import require_csrf
    require_csrf(request, csrf_token)
    status = updates_mod.check(layout, fetch=True, max_age_s=0)
    result = updates_mod.apply(layout, status)
    if result.ok and result.restart_ui:
        updates_mod.restart_process_later()
        level, message = "applied", "updated: " + "; ".join(result.steps) + " — Control Center restarting, reload this page in a few seconds"
    elif result.ok:
        level, message = "applied", "; ".join(result.steps)
    else:
        level, message = "error", result.error
    if request.headers.get("HX-Request"):
        return render(request, "partials/action_result.html", {"level": level, "message": message, "outcome": {}})
    from fastapi.responses import RedirectResponse

    from ..deps import encode_flash
    return RedirectResponse(url=f"/overview?flash={encode_flash(level, message)}", status_code=303)
