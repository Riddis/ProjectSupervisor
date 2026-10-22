"""Project extensions: the boundary between the generic framework and one project's specifics.

A project lists modules in `project.yaml` under `extensions:`. Each is imported (from the project's `extensions/`
directory first, then as a dotted module name) and its `setup(registry)` function is called once per process with
a `Registry`, through which the module can add:

    registry.adapter(adapter)                      # a new job lane (an Adapter subclass instance) — e.g. a renderer, a build farm, an API
    registry.doctor_check(fn)                      # fn(layout) -> doctor.Check; shown by `supervisor doctor` and the Control Center
    registry.ui_router(router, label, path)        # a FastAPI APIRouter + sidebar entry, for a project-specific Control Center page
    registry.overview_panel(fn)                    # fn(layout) -> {"title": str, "rows": [(label, value), ...]}; rendered on the overview
    registry.packet_screen(fn)                     # fn(layout, text, where) -> None | raises policy.PolicyViolation; runs on every packet
    registry.projection(fn)                        # fn(layout) -> Path; an extra rendered markdown file (like TASKS.md)
    registry.cli(typer_app, name)                  # a `supervisor <name> ...` command group
    registry.python_job(name, fn)                  # a deterministic job the `python` adapter can run: fn(root, run_dir, **params) -> result dict

The framework never imports from a project; a project never edits the framework. Anything that would be useful to
a project about something completely different belongs in the framework instead (docs/FRAMEWORK_VS_EXTENSION.md).
"""
from __future__ import annotations

import importlib
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import project
from .paths import Layout


@dataclass
class Registry:
    layout: Layout
    doctor_checks: list[Callable] = field(default_factory=list)
    ui_routers: list[tuple[Any, str, str]] = field(default_factory=list)
    overview_panels: list[Callable] = field(default_factory=list)
    packet_screens: list[Callable] = field(default_factory=list)
    projections: list[Callable] = field(default_factory=list)
    cli_apps: list[tuple[Any, str]] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def adapter(self, adapter) -> None:
        from .adapters.base import register
        register(adapter)

    def doctor_check(self, fn: Callable) -> None:
        self.doctor_checks.append(fn)

    def ui_router(self, router, label: str, path: str) -> None:
        self.ui_routers.append((router, label, path))

    def overview_panel(self, fn: Callable) -> None:
        self.overview_panels.append(fn)

    def packet_screen(self, fn: Callable) -> None:
        self.packet_screens.append(fn)

    def projection(self, fn: Callable) -> None:
        self.projections.append(fn)

    def cli(self, app, name: str) -> None:
        self.cli_apps.append((app, name))

    def python_job(self, name: str, fn: Callable) -> None:
        from .adapters.python import job
        job(name)(fn)


_registries: dict[str, Registry] = {}


def _import(layout: Layout, spec: str):
    ext_dir = str(layout.extensions)
    if layout.extensions.is_dir() and ext_dir not in sys.path:
        sys.path.insert(0, ext_dir)
    return importlib.import_module(spec)


def load(layout: Layout) -> Registry:
    """Import and set up every extension listed in project.yaml, once per project root per process. A broken
    extension is recorded in `registry.errors` (and shown by doctor) rather than taking the supervisor down."""
    key = str(layout.root)
    reg = _registries.get(key)
    if reg is not None:
        return reg
    reg = Registry(layout)
    _registries[key] = reg
    for spec in project.extensions(layout):
        try:
            mod = _import(layout, spec)
            setup = getattr(mod, "setup", None)
            if setup is None:
                reg.errors.append(f"{spec}: no setup(registry) function")
                continue
            setup(reg)
            reg.loaded.append(spec)
        except Exception:                                   # noqa: BLE001 - one bad extension must not kill the loop
            reg.errors.append(f"{spec}: {traceback.format_exc(limit=3).strip().splitlines()[-1]}")
    return reg


def registry(layout: Layout) -> Registry:
    return load(layout)


def reset() -> None:
    """Tests only: forget loaded registries (adapters registered by extensions stay registered)."""
    _registries.clear()


def scaffold_module(layout: Layout, name: str) -> Path:
    """Write a starter extension module under extensions/ (used by `supervisor extension new <name>`)."""
    layout.extensions.mkdir(parents=True, exist_ok=True)
    init = layout.extensions / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    p = layout.extensions / f"{name}.py"
    if p.exists():
        return p
    p.write_text(f'''"""{name}: a project extension (see docs/FRAMEWORK_VS_EXTENSION.md in the framework repo).

Registered in project.yaml under `extensions: [{name}]`. Everything here is specific to THIS project.
"""
from __future__ import annotations


def setup(registry):
    # Examples — delete what you do not need:
    #
    # from supervisor.doctor import Check
    # registry.doctor_check(lambda layout: Check("{name}", True, "ok"))
    #
    # registry.overview_panel(lambda layout: {{"title": "{name}", "rows": [("status", "ok")]}})
    #
    # from supervisor.adapters.process import ProcessAdapter
    # class MyAdapter(ProcessAdapter):
    #     name = "my_lane"
    #     def prepare(self, layout, task, attempt_id): ...
    # registry.adapter(MyAdapter())
    pass
''', encoding="utf-8")
    return p
