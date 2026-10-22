"""Gate status and phase eligibility (state/gates.yaml): phases.<n>.requires lists the gates that must be PASSED."""
from __future__ import annotations


import yaml

from . import store
from .paths import Layout


def load(layout: Layout) -> dict:
    if not layout.gates.exists():
        return {"phases": {0: {"requires": []}}, "gates": {}}
    return yaml.safe_load(layout.gates.read_text(encoding="utf-8")) or {}


def phase_open(layout: Layout, phase: int, data: dict | None = None) -> bool:
    data = data or load(layout)
    phases = data.get("phases") or {}
    if int(phase) not in phases:
        return False                     # unlisted phase: closed, never silently open
    required = (phases.get(int(phase)) or {}).get("requires", [])
    gates = data.get("gates") or {}
    return all((gates.get(g) or {}).get("status") == "PASSED" for g in required)


def set_status(layout: Layout, gate_id: str, status: str, evidence: list[str] | None = None, actor: str = "system"):
    if status not in ("OPEN", "PASSED", "FAILED"):
        raise ValueError(status)

    def fn(doc):
        g = doc.setdefault("gates", {}).setdefault(gate_id, {})
        g["status"] = status
        if evidence:
            g["evidence"] = sorted(set(g.get("evidence", []) + evidence))
        if status == "PASSED":
            g["passed_at"] = store.now_iso()
            g["passed_by"] = actor
        return doc

    return store.update(layout.gates, fn, lock_path=layout.state / ".gates.lock")


def ack(layout: Layout, gate_id: str, actor: str = "owner", note: str = ""):
    def fn(doc):
        g = doc.setdefault("gates", {}).setdefault(gate_id, {})
        g["owner_ack"] = {"by": actor, "at": store.now_iso(), "note": note}
        return doc

    return store.update(layout.gates, fn, lock_path=layout.state / ".gates.lock")
