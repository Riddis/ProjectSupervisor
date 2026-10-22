"""State/config migrations between framework versions. Each migration is idempotent and records nothing but the new
`framework_version` in project.yaml. `supervisor doctor` reports a mismatch; `supervisor migrate` applies these."""
from __future__ import annotations

from typing import Callable

import yaml

from . import __version__, project
from .paths import Layout

Migration = Callable[[Layout, bool], list[str]]


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


MIGRATIONS: list[Migration] = [_routing_limits_rename, _tasks_cost_category]


def run(layout: Layout, dry_run: bool = False) -> list[str]:
    notes: list[str] = []
    for m in MIGRATIONS:
        notes += m(layout, dry_run)
    doc = dict(project.load(layout)) if project.exists(layout) else {}
    recorded = str(doc.get("framework_version") or "")
    if recorded != __version__:
        notes.append(f"project.yaml: framework_version {recorded or 'unset'} → {__version__}")
        if not dry_run and doc:
            raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8")) or {}
            raw["framework_version"] = __version__
            project.write(layout, raw)
    return notes
