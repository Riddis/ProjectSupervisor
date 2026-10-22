"""State/config migrations between framework versions. Each migration is idempotent and records nothing but the new
`framework_version` in project.yaml. `supervisor doctor` reports a mismatch; `supervisor migrate` applies these."""
from __future__ import annotations

from typing import Callable

from . import __version__, project, store
from .paths import Layout

Migration = Callable[[Layout, bool], list[str]]

# Owner responses recorded before 1.0.0 (the project-specific supervisor) → ApprovalResponse. MORE_LIKE and
# REGENERATE both asked for another attempt at the same deliverable, which is what REDO means now; APPROVE was the
# inbox spelling of ACCEPT (still accepted as an alias when answering, see inbox.APPROVAL_ALIASES).
LEGACY_RESPONSES = {"MORE_LIKE": "REDO", "REGENERATE": "REDO", "APPROVE": "ACCEPT"}


class MigrationError(RuntimeError):
    """The project cannot be migrated as it stands (no or malformed project.yaml); the message says what to do."""


def _routing_limits_rename(layout: Layout, dry_run: bool) -> list[str]:
    """limits.max_gpu_jobs → limits.max_resource_jobs (1.0.0)."""
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return []
    text = cfg.read_text(encoding="utf-8")
    if "max_gpu_jobs" not in text:
        return []
    if not dry_run:
        cfg.write_text(text.replace("max_gpu_jobs", "max_resource_jobs"), encoding="utf-8")
    return ["config/routing.yaml: limits.max_gpu_jobs renamed to max_resource_jobs"]


def _tasks_cost_category(layout: Layout, dry_run: bool) -> list[str]:
    """cost_category local_gpu → local (1.0.0)."""
    notes = []
    for p in sorted(layout.tasks.glob("*.yaml")) + [layout.root / "state" / "tasks_seed.yaml"]:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        if "local_gpu" not in text:
            continue
        if not dry_run:
            p.write_text(text.replace("cost_category: local_gpu", "cost_category: local"), encoding="utf-8")
        notes.append(f"{p.relative_to(layout.root).as_posix()}: cost_category local_gpu → local")
    return notes


def _approval_responses(layout: Layout, dry_run: bool) -> list[str]:
    """Task approvals (and inbox allowed_responses) with a legacy response → the current enum (1.1.1):
    MORE_LIKE/REGENERATE → REDO, APPROVE → ACCEPT. A task holding an old value fails validation and cannot load."""
    notes = []
    for p in sorted(layout.tasks.glob("*.yaml")) + sorted(layout.inbox.glob("*.yaml")):
        try:
            doc = store.read(p)
        except Exception:                                   # noqa: BLE001 - an unreadable file is the doctor's job
            continue
        changed = []
        delivery = doc.get("delivery") if isinstance(doc.get("delivery"), dict) else {}
        for a in list(delivery.get("approvals") or []) + list(doc.get("approvals") or []):
            old = str(a.get("response") or "") if isinstance(a, dict) else ""
            if old in LEGACY_RESPONSES:
                a["response"] = LEGACY_RESPONSES[old]
                changed.append(f"{old} → {LEGACY_RESPONSES[old]}")
        allowed = doc.get("allowed_responses")
        if isinstance(allowed, list) and any(str(x) in LEGACY_RESPONSES for x in allowed):
            new = []
            for x in allowed:
                mapped = LEGACY_RESPONSES.get(str(x), x)
                if mapped not in new:
                    new.append(mapped)
            doc["allowed_responses"] = new
            changed.append("allowed_responses")
        if not changed:
            continue
        if not dry_run:
            p.write_text(store.dump(doc), encoding="utf-8")
        notes.append(f"{p.relative_to(layout.root).as_posix()}: response {', '.join(changed)}")
    return notes


MIGRATIONS: list[Migration] = [_routing_limits_rename, _tasks_cost_category, _approval_responses]


def run(layout: Layout, dry_run: bool = False) -> list[str]:
    """Apply every migration, then record the installed version in project.yaml. Raises MigrationError when there
    is no project.yaml to record it in, or the file is malformed — never reports success without a usable file."""
    if not project.exists(layout):
        raise MigrationError(f"no project.yaml at {layout.root}; run `supervisor init {layout.root}` first")
    note = project.problem(layout)
    if note and "framework_version" not in note:
        raise MigrationError(f"cannot migrate: {note}; fix the file first")
    notes: list[str] = []
    for m in MIGRATIONS:
        notes += m(layout, dry_run)
    raw = project.raw(layout)
    recorded = str(raw.get("framework_version") or "")
    if recorded != __version__:
        notes.append(f"project.yaml: framework_version {recorded or 'unset'} → {__version__}")
        if not dry_run:
            raw["framework_version"] = __version__
            project.write(layout, raw)
    return notes
