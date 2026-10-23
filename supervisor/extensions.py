"""Project extensions: the boundary between the generic framework and one project's specifics.

A project lists modules in `project.yaml` under `extensions:`. Each entry is a module name (`extensions/<name>.py`
in the project, or an installed dotted module) or a mapping `{name: <name>, required: false}`. A module is loaded
once per process and its `setup(registry)` function is called with a `Registry`, through which the module can add:

    registry.adapter(adapter)                      # a new job lane (an Adapter subclass instance) — e.g. a renderer, a build farm, an API
    registry.doctor_check(fn)                      # fn(layout) -> doctor.Check; shown by `supervisor doctor` and the Control Center
    registry.ui_router(router, label, path)        # a FastAPI APIRouter + sidebar entry, for a project-specific Control Center page
    registry.overview_panel(fn)                    # fn(layout) -> {"title": str, "rows": [(label, value), ...]}; rendered on the overview
    registry.packet_screen(fn)                     # fn(layout, text, where) -> None | raises policy.PolicyViolation; runs on every packet
    registry.projection(fn, path=None)             # fn(layout) -> Path; an extra rendered markdown file (like TASKS.md)
    registry.cli(typer_app, name)                  # a `supervisor <name> ...` command group
    registry.python_job(name, fn)                  # a deterministic job the `python` adapter can run: fn(root, run_dir, **params) -> result dict

Isolation: a project's `extensions/` directory is loaded as its own package (`supervisor_ext.<root hash>`) by file
path — never by putting the directory on `sys.path` — so two projects' `shared_ext.py` are two modules, and an
extension called `json` is the project's file, not the stdlib. Modules inside one project's `extensions/` can
import each other with relative imports (`from . import helpers`).

Failure: a configured extension that does not load is recorded in `registry.errors`. Unless the entry says
`required: false`, that failure also BLOCKS the loop and every packet (`assert_loaded`): an extension is usually a
protection (a packet screen, a lane with its own checks), and a protection that silently fell away is worse than a
supervisor that refuses to run until the owner looks.

The framework never imports from a project; a project never edits the framework. Anything that would be useful to
a project about something completely different belongs in the framework instead (docs/FRAMEWORK_VS_EXTENSION.md).
"""
from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.machinery
import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import project
from ._vendor.contract import atomic_json, read_json
from .paths import Layout

PROJECTIONS_FILE = "projection_paths.json"          # state/runtime/: projection files learned at render time


def _recorded_projections(layout: Layout) -> dict[str, list[str]]:
    """{extension name: [root-relative paths]} as recorded by earlier renders (any process)."""
    try:
        data = read_json(layout.runtime / PROJECTIONS_FILE)
    except (OSError, ValueError):
        return {}
    by_ext = data.get("by_extension") if isinstance(data, dict) else None
    if not isinstance(by_ext, dict):
        return {}
    return {str(k): sorted({str(x) for x in v}) for k, v in by_ext.items() if isinstance(v, list)}


def known_projection_paths(layout: Layout) -> set[str]:
    """Root-relative projection files recorded by earlier renders of this project, for the extensions project.yaml
    configures NOW. Rendering happens in the loop; verification may run in a child or after a restart, so what a
    render learned is kept on disk — but a path belongs to the extension that rendered it: remove the extension
    and its files are ordinary files again (an exclusion that outlived its extension hid real undeclared writes)."""
    configured = {name for name, _required in specs(layout)}
    return {path for ext, paths in _recorded_projections(layout).items() if ext in configured for path in paths}

NAMESPACE = "supervisor_ext"


class ExtensionError(RuntimeError):
    """A required extension did not load: the loop and the packet screens refuse to proceed (see `assert_loaded`)."""


@dataclass
class Registry:
    layout: Layout
    doctor_checks: list[Callable] = field(default_factory=list)
    ui_routers: list[tuple[Any, str, str]] = field(default_factory=list)
    overview_panels: list[Callable] = field(default_factory=list)
    packet_screens: list[Callable] = field(default_factory=list)
    projections: list[Callable] = field(default_factory=list)
    projection_paths: set[str] = field(default_factory=set)     # root-relative files the projections write (verify excludes them)
    cli_apps: list[tuple[Any, str]] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)             # every failure, required or not (doctor shows all)
    blocking: list[str] = field(default_factory=list)           # failures of required extensions: "<name>: <why>"

    _setting_up: str | None = None                              # the extension whose setup(registry) is running

    def __post_init__(self) -> None:
        self.projection_paths |= known_projection_paths(self.layout)

    def _remember_projection(self, ext: str, rel: str) -> None:
        """A projection file learned from a render, attributed to the extension that registered the projection:
        kept in state/runtime so a verify child or a restarted loop excludes it too, and dropped with the extension
        (`known_projection_paths` only returns paths of extensions still configured)."""
        self.projection_paths.add(rel)
        recorded = _recorded_projections(self.layout)
        if rel in recorded.get(ext, []):
            return
        recorded[ext] = sorted(set(recorded.get(ext, [])) | {rel})
        try:
            atomic_json(self.layout.runtime / PROJECTIONS_FILE, {"by_extension": recorded})
        except OSError:                                     # best effort: the in-memory set still covers this process
            pass

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

    def projection(self, fn: Callable, path: str | None = None) -> None:
        """`path` (root-relative) declares the file up front; otherwise it is learned from the first render. Either
        way the file is a rendered view like TASKS.md: never an artifact, never an undeclared write (supervisor.verify)."""
        ext = self._setting_up or getattr(fn, "__module__", None) or "?"
        if path:
            self._remember_projection(ext, Path(path).as_posix())
        root = self.layout.root

        @functools.wraps(fn)
        def wrapped(layout):
            out = fn(layout)
            try:
                p = Path(out)
                self._remember_projection(ext, (p.resolve().relative_to(root.resolve()) if p.is_absolute() else p).as_posix())
            except (TypeError, ValueError):               # not a path under the root: nothing to exclude
                pass
            return out
        self.projections.append(wrapped)

    def cli(self, app, name: str) -> None:
        self.cli_apps.append((app, name))

    def python_job(self, name: str, fn: Callable) -> None:
        from .adapters.python import job
        job(name)(fn)

    def blocking_reason(self) -> str | None:
        """Why the loop must not run, or None: names every required extension that failed to load."""
        if not self.blocking:
            return None
        return "required extension(s) failed to load: " + "; ".join(self.blocking)


_registries: dict[str, Registry] = {}


def specs(layout: Layout) -> list[tuple[str, bool]]:
    """[(name, required)] from project.yaml `extensions:` — a bare string is required; a mapping may say
    `required: false` to make its failure a doctor warning instead of a block."""
    out: list[tuple[str, bool]] = []
    for entry in (project.load(layout).get("extensions") or []):
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("module") or "").strip()
            required = entry.get("required", True)
            required = required if isinstance(required, bool) else str(required).strip().lower() not in ("false", "no", "0")
        else:
            name, required = str(entry or "").strip(), True
        if name:
            out.append((name, required))
    return out


def _valid_name(name: str) -> bool:
    return all(part.isidentifier() for part in name.split(".")) if name else False


def _package_name(layout: Layout) -> str:
    return f"{NAMESPACE}.{hashlib.sha1(str(Path(layout.root).resolve()).encode('utf-8')).hexdigest()[:12]}"


def _ensure_package(layout: Layout) -> str | None:
    """Register the project's `extensions/` directory as the package `supervisor_ext.<hash>` (no sys.path change):
    its modules are then importable by their namespaced name and nothing else on the path can shadow them."""
    ext_dir = Path(layout.extensions)
    if not ext_dir.is_dir():
        return None
    if NAMESPACE not in sys.modules:
        ns = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(NAMESPACE, None, is_package=True))
        ns.__path__ = []                                   # a pure namespace: holds one sub-package per project root
        sys.modules[NAMESPACE] = ns
    pkg_name = _package_name(layout)
    if pkg_name not in sys.modules:
        init = ext_dir / "__init__.py"
        if init.exists():
            spec = importlib.util.spec_from_file_location(pkg_name, str(init), submodule_search_locations=[str(ext_dir)])
        else:                                              # no __init__: a package with a search path and no code
            spec = importlib.machinery.ModuleSpec(pkg_name, None, is_package=True)
            spec.submodule_search_locations = [str(ext_dir)]
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = pkg
        if init.exists():
            try:
                spec.loader.exec_module(pkg)
            except Exception:
                del sys.modules[pkg_name]
                raise
        setattr(sys.modules[NAMESPACE], pkg_name.rsplit(".", 1)[1], pkg)
    return pkg_name


def _import(layout: Layout, name: str):
    """The project's `extensions/<name>.py` (loaded by file path under the project's namespace) or, when the
    project has no such file, an installed dotted module. The project's file always wins over an installed name."""
    if not _valid_name(name):
        raise ExtensionError(f"invalid extension name {name!r}: use identifiers (letters, digits, underscore), dotted for packages")
    ext_dir = Path(layout.extensions)
    rel = Path(*name.split("."))
    if ext_dir.is_dir() and ((ext_dir / rel).with_suffix(".py").exists() or (ext_dir / rel / "__init__.py").exists()):
        pkg_name = _ensure_package(layout)
        return importlib.import_module(f"{pkg_name}.{name}")
    try:
        return importlib.import_module(name)              # an installed package; never via a sys.path mutation
    except ModuleNotFoundError as exc:
        if exc.name in (name, name.split(".")[0]):
            raise ExtensionError(f"{layout.extensions / rel.with_suffix('.py')} not found and no installed module {name!r}") from None
        raise


def load(layout: Layout) -> Registry:
    """Import and set up every extension listed in project.yaml, once per project root per process. A broken
    extension is recorded in `registry.errors` (shown by doctor); a broken *required* one is also recorded in
    `registry.blocking`, and the loop and the packet screens refuse to proceed until it is fixed (`assert_loaded`)."""
    key = str(layout.root)
    reg = _registries.get(key)
    if reg is not None:
        return reg
    reg = Registry(layout)
    _registries[key] = reg
    if project.exists(layout):
        _doc, note = project._read_raw(project.path(layout))
        if note:
            # A project.yaml that cannot be parsed hides its `extensions:` list — which may name the policy screens
            # this project relies on. Falling back to "no extensions" would silently drop that protection, so the
            # unreadable file blocks exactly like a required extension that failed to import.
            reg.errors.append(f"project.yaml: {note}")
            reg.blocking.append(f"project.yaml: {note} (its extensions: list cannot be read)")
            return reg
    for name, required in specs(layout):
        try:
            mod = _import(layout, name)
            setup = getattr(mod, "setup", None)
            if setup is None:
                raise ExtensionError(f"no setup(registry) function in {getattr(mod, '__file__', None) or mod.__name__}")
            reg._setting_up = name
            try:
                setup(reg)
            finally:
                reg._setting_up = None
            reg.loaded.append(name)
        except Exception as exc:                            # noqa: BLE001 - recorded; a required one blocks the loop
            why = str(exc) if isinstance(exc, ExtensionError) else traceback.format_exc(limit=3).strip().splitlines()[-1]
            reg.errors.append(f"{name}: {why}")
            if required:
                reg.blocking.append(f"{name}: {why}")
    return reg


def registry(layout: Layout) -> Registry:
    return load(layout)


def assert_loaded(layout: Layout) -> Registry:
    """The registry, or ExtensionError naming every required extension that did not load. Called before the loop
    dispatches and before any packet is cleared: a missing protection must stop work, not let it through."""
    reg = load(layout)
    reason = reg.blocking_reason()
    if reason:
        raise ExtensionError(reason)
    return reg


def reset() -> None:
    """Tests only: forget loaded registries and the per-project extension packages (adapters registered by
    extensions stay registered)."""
    _registries.clear()
    for key in [k for k in sys.modules if k == NAMESPACE or k.startswith(NAMESPACE + ".")]:
        del sys.modules[key]


def scaffold_module(layout: Layout, name: str) -> Path:
    """Write a starter extension module under extensions/ (used by `supervisor extension new <name>`)."""
    if not name.isidentifier():
        raise ExtensionError(f"invalid extension name {name!r}: letters, digits and underscores only")
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
