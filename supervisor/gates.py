"""Gate status and phase eligibility (state/gates.yaml): phases.<n>.requires lists the gates that must be open.

A gate is open when its status is PASSED and, when the gate declares them, its requirements are met:
`evidence_required: true` needs a non-empty evidence list, `owner_ack_required: true` needs an `owner_ack` recorded by
an owner actor. Both default to false so legacy gate files keep working. PASSED is refused up front when the actor or
the evidence cannot satisfy them, so an agent can never open an owner gate on its own.
"""
from __future__ import annotations


import yaml

from . import store
from .paths import Layout

STATUSES = ("OPEN", "PASSED", "FAILED")


class GateError(ValueError):
    pass


def is_owner_actor(actor: str | None) -> bool:
    """Owner identities: the CLI's `owner`, the Control Center's `owner-ui`, and any `owner-<channel>`."""
    actor = (actor or "").strip()
    return actor == "owner" or actor.startswith("owner-")


def load(layout: Layout) -> dict:
    if not layout.gates.exists():
        return {"phases": {0: {"requires": []}}, "gates": {}}
    return yaml.safe_load(layout.gates.read_text(encoding="utf-8")) or {}


def requirements_unmet(gate: dict | None) -> list[str]:
    """Why a gate record is not open (empty when it is). Checks the record only, never the actor of the current call."""
    g = gate or {}
    why = []
    if g.get("status") != "PASSED":
        why.append(f"status is {g.get('status') or 'unset'}, not PASSED")
    if g.get("evidence_required") and not g.get("evidence"):
        why.append("evidence required but none recorded")
    if g.get("owner_ack_required"):
        ack = g.get("owner_ack") or {}
        if not ack:
            why.append("owner acknowledgement required but not recorded")
        elif not is_owner_actor(ack.get("by")):
            why.append(f"owner acknowledgement required but recorded by non-owner {ack.get('by')!r}")
    return why


def gate_open(gate: dict | None) -> bool:
    return not requirements_unmet(gate)


def phase_open(layout: Layout, phase: int, data: dict | None = None) -> bool:
    data = data or load(layout)
    phases = data.get("phases") or {}
    if int(phase) not in phases:
        return False                     # unlisted phase: closed, never silently open
    required = (phases.get(int(phase)) or {}).get("requires", [])
    gates = data.get("gates") or {}
    return all(gate_open(gates.get(g)) for g in required)


def set_status(layout: Layout, gate_id: str, status: str, evidence: list[str] | None = None, actor: str = "system"):
    """Record a gate status. PASSED is validated against the gate's declared requirements: an owner gate can only be
    passed by an owner actor (whose pass doubles as the acknowledgement when none is recorded yet) and an evidence
    gate needs at least one evidence entry (already recorded or given here). Raises GateError otherwise."""
    if status not in STATUSES:
        raise GateError(f"{gate_id}: unknown status {status!r} (expected one of {', '.join(STATUSES)})")

    def fn(doc):
        g = doc.setdefault("gates", {}).setdefault(gate_id, {})
        merged = sorted(set(g.get("evidence") or []) | set(evidence or []))
        if status == "PASSED":
            if g.get("evidence_required") and not merged:
                raise GateError(f"{gate_id}: PASSED needs evidence (evidence_required) — pass --evidence path1,path2")
            if g.get("owner_ack_required") and not is_owner_actor(actor):
                raise GateError(f"{gate_id}: only an owner may pass this gate (owner_ack_required); actor {actor!r} "
                                "is not an owner — ask the owner to acknowledge it in the Control Center")
        g["status"] = status
        if merged:
            g["evidence"] = merged
        if status == "PASSED":
            g["passed_at"] = store.now_iso()
            g["passed_by"] = actor
            if g.get("owner_ack_required") and not is_owner_actor((g.get("owner_ack") or {}).get("by")):
                g["owner_ack"] = {"by": actor, "at": g["passed_at"], "note": "passed by owner"}
        return doc

    return store.update(layout.gates, fn, lock_path=layout.state / ".gates.lock")


def ack(layout: Layout, gate_id: str, actor: str = "owner", note: str = ""):
    """Record the owner's acknowledgement. Only an owner actor may acknowledge: an acknowledgement is the owner's
    signature on the gate, so an agent recording one would be exactly the bypass the requirement exists to stop."""
    if not is_owner_actor(actor):
        raise GateError(f"{gate_id}: only an owner may acknowledge a gate; actor {actor!r} is not an owner")

    def fn(doc):
        g = doc.setdefault("gates", {}).setdefault(gate_id, {})
        g["owner_ack"] = {"by": actor, "at": store.now_iso(), "note": note}
        return doc

    return store.update(layout.gates, fn, lock_path=layout.state / ".gates.lock")
