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
