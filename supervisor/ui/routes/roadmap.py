from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ... import gates as gates_mod
from ... import tasks as tasks_mod
from ...paths import Layout
from ..auth import require_auth, require_csrf
from ..deps import act, decode_flash, render

router = APIRouter()


@router.get("/roadmap")
def roadmap(request: Request, layout: Layout = Depends(require_auth)):
    tasks = tasks_mod.load_all(layout)
    gate_data = gates_mod.load(layout)
    elig_ids = {t.id for t in tasks_mod.eligible(layout, tasks)}
    by_phase: dict[int, list] = {}
    for t in tasks.values():
        by_phase.setdefault(t.phase, []).append(t)
    phases = []
    for phase in sorted(set(by_phase) | {int(p) for p in (gate_data.get("phases") or {})}):
        rows = sorted(by_phase.get(phase, []), key=lambda x: x.id)
        requires = ((gate_data.get("phases") or {}).get(phase) or {}).get("requires", [])
        gates = [dict(gate_data.get("gates", {}).get(g, {}), id=g) for g in requires]
        phases.append({"phase": phase, "tasks": rows, "gates": gates,
                       "open": gates_mod.phase_open(layout, phase, gate_data)})
    return render(request, "roadmap.html", {"phases": phases, "elig_ids": elig_ids, "flash": decode_flash(request)})


@router.post("/roadmap/gate/{gate_id}/ack")
def gate_ack(gate_id: str, request: Request, note: str = Form(""), csrf_token: str = Form(...),
            layout: Layout = Depends(require_auth)):
    require_csrf(request, csrf_token)
    return act(request, layout, "gate.ack", {"gate_id": gate_id, "note": note}, "/roadmap")


@router.post("/roadmap/gate/{gate_id}/pass")
def gate_pass(gate_id: str, request: Request, evidence: str = Form(""), csrf_token: str = Form(...),
              layout: Layout = Depends(require_auth)):
    """The owner passes a gate from the Roadmap: the Control Center acts as an owner actor, so an owner-acknowledged
    gate is passed and acknowledged in one step (agents cannot do this; see gates.set_status). `evidence` is one
    entry per line — an evidence gate refuses without any."""
    require_csrf(request, csrf_token)
    items = [ln.strip() for ln in evidence.splitlines() if ln.strip()]
    return act(request, layout, "gate.set", {"gate_id": gate_id, "status": "PASSED", "evidence": items}, "/roadmap")
