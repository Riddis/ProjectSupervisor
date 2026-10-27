"""Project extensions: the boundary between the generic framework and one project's specifics.

A project lists modules in `project.yaml` under `extensions:`. Each entry is a module name (`extensions/<name>.py`
in the project, or an installed dotted module) or a mapping `{name: <name>, required: false}`. A module is loaded
once per process and its `setup(registry)` function is called with a `Registry`, through which the module can add:

    registry.adapter(adapter)                      # a new job lane (an Adapter subclass instance) — e.g. a renderer, a build farm, an API
    registry.doctor_check(fn)                      # fn(layout) -> doctor.Check; shown by `supervisor doctor` and the Control Center
    registry.ui_router(router, label, path)        # a FastAPI APIRouter + sidebar entry, for a project-specific Control Center page
    registry.overview_panel(fn)                    # fn(layout) -> {"title": str, "rows": [(label, value), ...]}; rendered on the overview
    registry.packet_screen(fn)                     # fn(layout, text, where) -> None | raises policy.PolicyViolation; runs on every packet
    registry.projection(fn, path=None, name=None)  # fn(layout) -> Path; an extra rendered markdown file (like TASKS.md)
    registry.cli(typer_app, name)                  # a `supervisor <name> ...` command group
    registry.python_job(name, fn)                  # a deterministic job the `python` adapter can run: fn(root, run_dir, **params) -> result dict
    registry.command(kind, fn)                     # an owner command kind the loop/UI/CLI apply: fn(layout, payload, actor) -> result dict
    registry.inbox_kind(kind, label, ...)          # an inbox item kind the extension owns (own page, not answerable from the generic form, ...)
    registry.template_dir(path)                    # Jinja templates an extension page renders; they may `{% extends "base.html" %}`
    registry.static_dir(path, prefix)              # static files (js/css/images) served under /static/<prefix>/

Isolation: a project's `extensions/` directory is loaded as its own package (`supervisor_ext.<root hash>`) by file
path — never by putting the directory on `sys.path` — so two projects' `shared_ext.py` are two modules, and an
extension called `json` is the project's file, not the stdlib. Modules inside one project's `extensions/` can
import each other with relative imports (`from . import helpers`).

Loading is transactional (1.12.0, boundary plan phase 3). setup() registers into a staging area: the process-wide
tables (lanes, python jobs, command kinds) are written only by one commit after setup() returns, and the project's
registry is put back as it was when anything fails. The failures are an exception in setup(), a registration the
extension's manifest (`supervisor-extension.toml`, supervisor.manifest) does not declare, a manifest discovery refuses
(supervisor.discovery), or an import error. Then nothing the extension registered remains, and a module that failed is
dropped from sys.modules. An extension without a manifest still loads, staged the same way, and is listed in
`registry.unmanifested`.

Approval comes first. Every configured extension is discovered, and what its manifest decides is settled before any
extension module is imported, so a refused extension never executes:
- the framework range;
- identifiers that are built into the framework;
- identifiers another manifest claims too (both are refused);
- `requires.extensions` that project.yaml does not list, or a cycle among them.

Extensions then load after the ones they require, and one whose requirement did not load is refused.

Failure: a configured extension that does not load is recorded in `registry.errors`. Unless the entry says
`required: false`, that failure also BLOCKS the loop and every packet (`assert_loaded`): an extension is usually a
protection (a packet screen, a lane with its own checks), and a protection that silently fell away is worse than a
supervisor that refuses to run until the owner looks.

The framework never imports from a project; a project never edits the framework. Anything that would be useful to
a project about something completely different belongs in the framework instead (docs/FRAMEWORK_VS_EXTENSION.md).
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import functools
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
import threading
import traceback
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import discovery, project
from ._vendor.contract import atomic_json, read_json
from .paths import Layout

PROJECTIONS_FILE = "projection_paths.json"          # state/runtime/: projection files learned at render time


def _read_projection_record(layout: Layout) -> dict:
    """The projection record as written: {"projections": {"<ext>:<fn>": {"declared": [...], "rendered": path|None}},
    "legacy": [...]}. A 1.2.1 file (`paths`: a flat list, unattributed) is carried as `legacy` until every projection
    of the current registry has rendered once, so an upgrade never loses the exclusions of untouched views."""
    try:
        data = read_json(layout.runtime / PROJECTIONS_FILE)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    projections = data.get("projections") if isinstance(data.get("projections"), dict) else {}
    out: dict[str, dict] = {}
    for key, entry in projections.items():
        if not isinstance(entry, dict):
            continue
        declared = entry.get("declared") if isinstance(entry.get("declared"), list) else []
        rendered = entry.get("rendered") if isinstance(entry.get("rendered"), str) else None
        out[str(key)] = {"declared": sorted({str(x) for x in declared}), "rendered": rendered}
    legacy = data.get("legacy") if isinstance(data.get("legacy"), list) else data.get("paths")   # 1.2.1 format
    legacy = sorted({str(x) for x in legacy}) if isinstance(legacy, list) else []
    # 1.2.1.x interim format: {"by_extension": {ext: [paths]}} - attributed to the extension but not the projection
    by_ext = data.get("by_extension")
    if isinstance(by_ext, dict) and not projections:
        legacy = sorted(set(legacy) | {str(x) for v in by_ext.values() if isinstance(v, list) for x in v})
    return {"projections": out, "legacy": legacy}


def _recorded_projections(layout: Layout) -> dict[str, list[str]]:
    """{extension name: [root-relative paths]} currently recorded, for inspection and tests."""
    out: dict[str, set[str]] = {}
    for key, entry in _read_projection_record(layout)["projections"].items():
        ext = key.split(":", 1)[0]
        out.setdefault(ext, set()).update(entry["declared"])
        if entry["rendered"]:
            out[ext].add(entry["rendered"])
    return {k: sorted(v) for k, v in out.items()}


def known_projection_paths(layout: Layout) -> set[str]:
    """Root-relative projection files recorded by earlier renders of this project, for the extensions project.yaml
    configures NOW. Rendering happens in the loop; verification may run in a child or after a restart, so what a
    render learned is kept on disk — but a path belongs to the projection that rendered it: remove the extension,
    or point its projection at another file, and the old file is an ordinary file again (an exclusion that outlived
    its projection hid real undeclared writes). Unattributed 1.2.1 entries count while any extension is configured
    and until the registry has seen every projection render once."""
    return _paths_in_record(_read_projection_record(layout), {name for name, _required in specs(layout)})


def _paths_in_record(rec: dict, configured: set[str]) -> set[str]:
    out: set[str] = set()
    for key, entry in rec["projections"].items():
        if key.split(":", 1)[0] in configured:
            out.update(entry["declared"])
            if entry["rendered"]:
                out.add(entry["rendered"])
    if configured:
        out.update(rec["legacy"])
    return out

NAMESPACE = "supervisor_ext"
PUBLIC_MODULES = ("supervisor.ext", "supervisor.testing")     # all of supervisor an extension may import (1.11.0)


class ExtensionError(RuntimeError):
    """A required extension did not load: the loop and the packet screens refuse to proceed (see `assert_loaded`)."""


@dataclass(frozen=True)
class InboxKind:
    """How the Control Center and the generic answer path treat inbox items of one extension-owned `kind`.

    `label`: the group heading on the inbox page. `page`: a path (e.g. "/review/{id}") the card links to instead of
    showing the generic form; `{id}` is the item id. `answerable`: whether the generic answer path — the inbox form,
    `inbox.answer` command, `inbox.answer()` without `internal=True` — may close the item; False means only the
    extension's own code may (it calls `inbox.answer(..., internal=True)`). `show_evidence`: whether the per-option
    sub-items (`item["items"]`) and their score fields are rendered on the card; False keeps them for the extension's
    page (blind labelling). `score_keys`: the sub-item fields that mark a scored option, added to the built-in set."""
    kind: str
    label: str
    page: str | None = None
    answerable: bool = False
    show_evidence: bool = True
    score_keys: tuple[str, ...] = ()

    def page_for(self, item_id: str) -> str | None:
        return self.page.replace("{id}", item_id) if self.page else None


@dataclass(frozen=True)
class InternalImport:
    """An import of a framework internal in an extension's source: deprecated since 1.11.0 (supervisor.ext only)."""
    file: str                 # root-relative (posix) under the project root, else absolute
    line: int
    module: str               # the dotted module imported (`from supervisor import inbox` -> supervisor.inbox)


@dataclass
class Registry:
    layout: Layout
    doctor_checks: list[Callable] = field(default_factory=list)
    doctor_check_owners: list[str] = field(default_factory=list)   # the extension of each doctor check, same order
    ui_routers: list[tuple[Any, str, str]] = field(default_factory=list)
    overview_panels: list[Callable] = field(default_factory=list)
    packet_screens: list[Callable] = field(default_factory=list)
    projections: list[Callable] = field(default_factory=list)
    _projection_keys: list[str] = field(default_factory=list)   # "<ext>:<name>" per registered projection, same order
    _declared: dict[str, set[str]] = field(default_factory=dict)  # key -> paths its registration declares NOW
    projection_paths: set[str] = field(default_factory=set)     # root-relative files the projections write (verify excludes them)
    cli_apps: list[tuple[Any, str]] = field(default_factory=list)
    adapters: dict[str, str] = field(default_factory=dict)          # lane name -> the extension that registered it
    python_jobs: dict[str, str] = field(default_factory=dict)       # python job name -> the extension that registered it
    config_owners: dict[str, str] = field(default_factory=dict)     # routing.yaml section -> the extension that declared it
    inbox_kind_owners: dict[str, str] = field(default_factory=dict)  # inbox kind -> the extension that claimed it
    origins: dict[str, str] = field(default_factory=dict)           # extension -> its module's source file, resolved
    commands: dict[str, Callable] = field(default_factory=dict)    # command kinds registered by extensions (commands.HANDLERS has them too)
    inbox_kinds: dict[str, InboxKind] = field(default_factory=dict)
    template_dirs: list[Path] = field(default_factory=list)
    static_dirs: list[tuple[str, Path]] = field(default_factory=list)   # (url prefix, directory)
    loaded: list[str] = field(default_factory=list)
    internal_imports: list[InternalImport] = field(default_factory=list)   # deprecated imports in extension sources
    errors: list[str] = field(default_factory=list)             # every failure, required or not (doctor shows all)
    blocking: list[str] = field(default_factory=list)           # failures of required extensions: "<name>: <why>"
    unmanifested: list[str] = field(default_factory=list)       # loaded without a manifest (1.12.x: a warning)
    manifests: dict[str, Any] = field(default_factory=dict)     # extension -> its Manifest, when it has one
    config_schemas: dict[str, Any] = field(default_factory=dict)  # extension -> the pydantic model of its section

    _setting_up: str | None = None                              # the extension whose setup(registry) is running

    def __post_init__(self) -> None:
        self.projection_paths |= known_projection_paths(self.layout)
        self._rendered_keys: set[str] = set()
        # While load() runs an extension's setup(): the process-wide writes it asked for, applied by _commit(), and
        # what it registered, (manifest kind, identifier), checked against its manifest. None outside setup().
        self._pending: dict[tuple[str, str], tuple[Any, str, str]] | None = None
        self._registered: list[tuple[str, str]] | None = None

    @property
    def identity(self) -> str | None:
        """The identity of the extension whose setup() is running (its manifest's, which is the name project.yaml
        uses), or None outside setup()."""
        return self._setting_up

    # ---------------------------------------------------------------- staging (1.12.0)

    def _note(self, kind: str, identifier: str) -> None:
        if self._registered is not None:
            self._registered.append((kind, identifier))

    def _staged(self, table: str, name: str):
        """What this setup() already asked to write under `name` in a process-wide table, or None."""
        entry = (self._pending or {}).get((table, name))
        return entry[0] if entry is not None else None

    def _write(self, table: str, name: str, obj, origin: str, owner: str) -> None:
        """A process-wide write: held until the commit while setup() runs, applied at once otherwise."""
        if self._pending is not None:
            self._pending[(table, name)] = (obj, origin, owner)
        else:
            _apply(table, name, obj, origin, owner)

    def _commit(self) -> None:
        for (table, name), (obj, origin, owner) in (self._pending or {}).items():
            _apply(table, name, obj, origin, owner)
        self._pending = None

    def _snapshot(self) -> dict:
        """Every field a setup() can change, copied deep enough to put back: lists and sets copied, dicts copied
        with their set values (projection declarations). Generic over the dataclass, so a field added later is
        covered without anyone remembering to."""
        out = {}
        for f in dataclasses.fields(self):
            if f.name in _OUTCOME_FIELDS:
                continue
            value = getattr(self, f.name)
            if isinstance(value, dict):
                value = {k: (set(v) if isinstance(v, set) else v) for k, v in value.items()}
            elif isinstance(value, (list, set)):
                value = copy.copy(value)
            out[f.name] = value
        return out

    def _restore(self, snapshot: dict) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)
        self._pending = None

    def _write_record(self, rec: dict) -> None:
        try:
            atomic_json(self.layout.runtime / PROJECTIONS_FILE,
                        {"projections": rec["projections"], "legacy": rec["legacy"]})
        except OSError:                                     # best effort: the in-memory set still covers this process
            pass

    def _remember_projection(self, key: str, rel: str) -> None:
        """A rendered projection file, attributed to its projection (`<ext>:<name>`): kept in state/runtime so a verify
        child or a restarted loop excludes it too. A render replaces the projection's previous rendered path (a
        renamed output retires the old file). Once every projection of this registry has rendered, unattributed 1.2.1
        entries are dropped: they are all re-recorded."""
        self.projection_paths.add(rel)
        rec = _read_projection_record(self.layout)
        entry = rec["projections"].setdefault(key, {"declared": sorted(self._declared.get(key, ())), "rendered": None})
        before = (list(entry["declared"]), entry["rendered"], list(rec["legacy"]))
        if entry["rendered"] and entry["rendered"] != rel and entry["rendered"] not in entry["declared"]:
            self.projection_paths.discard(entry["rendered"])
        entry["rendered"] = rel
        self._rendered_keys.add(key)
        if rec["legacy"] and all(k in self._rendered_keys for k in self._projection_keys):
            rec["legacy"] = []
            configured = {name for name, _required in specs(self.layout)}
            self.projection_paths = _paths_in_record(rec, configured) | self._all_declared() | {rel}
        if before != (entry["declared"], entry["rendered"], rec["legacy"]):
            self._write_record(rec)

    def _all_declared(self) -> set[str]:
        return {p for paths in self._declared.values() for p in paths}

    def _reconcile(self, loaded: set[str]) -> None:
        """Make the record match what is registered NOW, for every extension that just set up successfully: a
        projection it no longer registers is forgotten, a projection's declared paths are exactly what it declares
        (a changed declaration retires the old file, and the rendered path learned for it), and with no projection
        registered at all the unattributed 1.2.1 entries go too (nothing would ever re-record them). Extensions that
        are not configured keep their entries (filtered on read, so re-adding one restores them); one that failed to
        load keeps them as they were (its registrations are unknown)."""
        rec = _read_projection_record(self.layout)
        snapshot = repr(rec)
        current = set(self._projection_keys)
        for key in list(rec["projections"]):
            if key.split(":", 1)[0] in loaded and key not in current:
                del rec["projections"][key]
        for key in self._projection_keys:
            new = sorted(self._declared.get(key, ()))
            entry = rec["projections"].get(key)
            if entry is None:
                if new:
                    rec["projections"][key] = {"declared": new, "rendered": None}
                continue
            if entry["declared"] != new:
                if entry["rendered"] in entry["declared"] and entry["rendered"] not in new:
                    entry["rendered"] = None            # learned from the old declaration: re-learned on the next render
                entry["declared"] = new
            if not entry["declared"] and not entry["rendered"]:
                del rec["projections"][key]
        if not self.errors and not self._projection_keys:
            rec["legacy"] = []
        if repr(rec) != snapshot:
            self._write_record(rec)
        configured = {name for name, _required in specs(self.layout)}
        self.projection_paths = _paths_in_record(rec, configured) | self._all_declared()

    def adapter(self, adapter) -> None:
        """A job lane: an `Adapter` instance, registered under its `name`. Call it from setup(), never at import
        (importing an extension changes no registry). Registering the same object again is harmless (an extension
        that also registers at import, as before 1.11.0), and so is the extension that registered a name registering
        it again after a reload; a name another lane already has, built in or another extension's, is an error,
        never an overwrite: the new lane would take over that lane's jobs."""
        from .adapters.base import REGISTRY
        name = getattr(adapter, "name", None)
        if not isinstance(name, str) or not name:
            raise ExtensionError(f"an adapter needs a non-empty string `name`, got {adapter!r}")
        owner = self._setting_up or "?"
        current = REGISTRY.get(name)
        if (self._staged("adapter", name) is None and current is not None and current is not adapter
                and _taken(name, _extension_adapters, _ever_adapters, self._origin())):
            raise ExtensionError(f"adapter name {name!r} is already registered {_holder(name, current, _extension_adapters)}")
        self._write("adapter", name, adapter, self._origin(), owner)   # now, or at the commit
        self.adapters[name] = owner
        self._note("adapters", name)

    def config_sections(self, *names: str) -> None:
        """The routing.yaml sections this extension reads with `supervisor.ext.config()`; nothing else of the file is
        open to it (the shared resource policy comes from `supervisor.ext.resources()`). One owner per section: a
        framework section, or one another extension declared, is an error."""
        from .ext._config import framework_sections
        owner = self._setting_up or "?"
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
                raise ExtensionError(f"a config section must be a routing.yaml key (letters, digits, _ and -), got {name!r}")
            if name in framework_sections():
                raise ExtensionError(f"config section {name!r} is the framework's; extensions read the shared resource "
                                     f"policy with supervisor.ext.resources()")
            if self.config_owners.get(name, owner) != owner:
                raise ExtensionError(f"config section {name!r} is already declared by extension {self.config_owners[name]!r}")
        for name in names:
            self.config_owners[name] = owner

    def doctor_check(self, fn: Callable) -> None:
        self.doctor_checks.append(fn)                      # its name is known only when it runs: doctor checks it
        self.doctor_check_owners.append(self._setting_up or "?")

    def ui_router(self, router, label: str, path: str) -> None:
        self.ui_routers.append((router, label, path))
        self._note("routes", str(path))

    def overview_panel(self, fn: Callable) -> None:
        self.overview_panels.append(fn)
        self._note("hooks", "overview_panel")

    def packet_screen(self, fn: Callable) -> None:
        self.packet_screens.append(fn)
        self._note("hooks", "packet_screen")

    def projection(self, fn: Callable, path: str | None = None, name: str | None = None) -> None:
        """`path` (root-relative) declares the file up front; otherwise it is learned from the first render. Either
        way the file is a rendered view like TASKS.md: never an artifact, never an undeclared write (supervisor.verify).

        Each projection is recorded under `<extension>:<name>` — `name` if given, else the function's __name__. Two
        callbacks with the same __name__ (two lambdas, two closures from one factory) get `#2`, `#3`, ... in
        registration order, so neither overwrites the other's record; pass `name=` for an identity that survives
        reordering. A `name` registered twice by one extension is an error."""
        ext = self._setting_up or getattr(fn, "__module__", None) or "?"
        base = f"{ext}:{name or getattr(fn, '__name__', None) or 'projection'}"
        if name and base in self._projection_keys:
            raise ExtensionError(f"projection name {name!r} is registered twice by {ext}")
        key, n = base, 2
        while key in self._projection_keys:
            key, n = f"{base}#{n}", n + 1
        self._projection_keys.append(key)
        self._note("projections", key.split(":", 1)[1].split("#", 1)[0])
        if path:
            rel = Path(path).as_posix()
            self._declared.setdefault(key, set()).add(rel)
            self.projection_paths.add(rel)
            if self._setting_up is None:                  # registered outside load(): nothing reconciles it later
                rec = _read_projection_record(self.layout)
                entry = rec["projections"].setdefault(key, {"declared": [], "rendered": None})
                if entry["declared"] != sorted(self._declared[key]):
                    entry["declared"] = sorted(self._declared[key])
                    self._write_record(rec)
        root = self.layout.root

        @functools.wraps(fn)
        def wrapped(layout):
            out = fn(layout)
            try:
                p = Path(out)
                self._remember_projection(key, (p.resolve().relative_to(root.resolve()) if p.is_absolute() else p).as_posix())
            except (TypeError, ValueError):               # not a path under the root: nothing to exclude
                pass
            return out
        self.projections.append(wrapped)

    def cli(self, app, name: str) -> None:
        self.cli_apps.append((app, name))
        self._note("cli", str(name))

    def python_job(self, name: str, fn: Callable) -> None:
        """A deterministic job for `job: {adapter: python, fn: <name>, params: {...}}`, `fn(root, run_dir, **params)
        -> {"status", "artifacts", "notes"}`. Call it from setup(), never at import. Like `adapter()`, the same
        function again is harmless and a name already taken is an error."""
        from .adapters.python import JOBS
        if not isinstance(name, str) or not name:
            raise ExtensionError(f"a python job needs a non-empty string name, got {name!r}")
        if not callable(fn):
            raise ExtensionError(f"python job {name!r}: fn must be callable, got {fn!r}")
        owner = self._setting_up or "?"
        current = JOBS.get(name)
        if (self._staged("python_job", name) is None and current is not None and current is not fn
                and _taken(name, _extension_jobs, _ever_jobs, self._origin())):
            raise ExtensionError(f"python job {name!r} is already registered {_holder(name, current, _extension_jobs)}")
        self._write("python_job", name, fn, self._origin(), owner)
        self.python_jobs[name] = owner
        self._note("python_jobs", name)

    def _origin(self) -> str:
        """Who is registering, for name ownership: the source file of the extension being set up (one file is one
        extension, whatever a project or its tests configure it as), or its name when the file is unknown."""
        name = self._setting_up or "?"
        return self.origins.get(name) or f"name:{name}"

    def command(self, kind: str, fn: Callable) -> None:
        """An owner command kind, `fn(layout, payload, actor) -> dict`, applied by whoever holds the state lock (the
        loop, the Control Center's direct path, the CLI) exactly like the built-in kinds. Handlers must be idempotent
        by command id (`payload["_id"]`): a crash between the handler and the applied-file write replays it. A
        documented wrapper over `commands.handler`, so the registry can list what an extension added; a kind that
        collides with a built-in or another extension's is an error (a silent overwrite would be a hijack)."""
        from . import commands as commands_mod
        if not kind or not isinstance(kind, str):
            raise ExtensionError(f"command kind must be a non-empty string, got {kind!r}")
        current = commands_mod.HANDLERS.get(kind)
        if (self._staged("command", kind) is None and current is not None and current is not fn
                and _extension_commands.get(kind) != self._origin()):
            raise ExtensionError(f"command kind {kind!r} is already registered")      # the same source file may again
        self._write("command", kind, fn, self._origin(), self._setting_up or "?")
        self.commands[kind] = fn
        self._note("commands", kind)

    def inbox_kind(self, kind: str, label: str, page: str | None = None, answerable: bool = False,
                   show_evidence: bool = True, score_keys: tuple[str, ...] | list[str] = ()) -> None:
        """Claim an inbox item kind (see InboxKind). Built-in kinds cannot be claimed; one kind, one extension."""
        from . import inbox as inbox_mod
        if not kind or not isinstance(kind, str):
            raise ExtensionError(f"inbox kind must be a non-empty string, got {kind!r}")
        if kind in inbox_mod.BUILTIN_KINDS:
            raise ExtensionError(f"inbox kind {kind!r} is built in and cannot be claimed by an extension")
        if kind in self.inbox_kinds:
            raise ExtensionError(f"inbox kind {kind!r} is already claimed")
        if page is not None and (not page.startswith("/") or "{id}" not in page):
            raise ExtensionError(f"inbox kind {kind!r}: page must be an absolute path containing {{id}}, got {page!r}")
        self.inbox_kinds[kind] = InboxKind(kind, str(label), page, bool(answerable), bool(show_evidence),
                                           tuple(str(k) for k in score_keys))
        self.inbox_kind_owners[kind] = self._setting_up or "?"
        self._note("inbox_kinds", kind)

    def template_dir(self, path) -> None:
        """A directory of Jinja templates for an extension's Control Center pages. Loaded *after* the framework's own
        templates (ChoiceLoader), so an extension page can `{% extends "base.html" %}` and use the partials, and a
        framework template can never be overridden by an extension file of the same name."""
        p = Path(path)
        if not p.is_dir():
            raise ExtensionError(f"template_dir {p} is not a directory")
        if p not in self.template_dirs:
            self.template_dirs.append(p)
        self._note("hooks", "template_dir")

    def static_dir(self, path, prefix: str) -> None:
        """Static files served at `/static/<prefix>/...` (the framework's own live under `/static/` directly)."""
        p = Path(path)
        if not p.is_dir():
            raise ExtensionError(f"static_dir {p} is not a directory")
        prefix = str(prefix).strip("/")
        if not prefix or "/" in prefix or not all(c.isalnum() or c in "-_" for c in prefix):
            raise ExtensionError(f"static_dir prefix must be one path segment (letters, digits, - _), got {prefix!r}")
        if any(pre == prefix for pre, _ in self.static_dirs):
            raise ExtensionError(f"static prefix {prefix!r} is already registered")
        self.static_dirs.append((prefix, p))
        self._note("static", prefix)

    def blocking_reason(self) -> str | None:
        """Why the loop must not run, or None: names every required extension that failed to load."""
        if not self.blocking:
            return None
        return "required extension(s) failed to load: " + "; ".join(self.blocking)


_registries: dict[str, Registry] = {}                       # published: loads that have finished
# Loads in progress, by (project root, thread): only that thread sees its registry before it is published, so
# ext.config() in a setup() reads it, and no other thread ever gets an unfinished one (review 1.12.0 R2). G5's
# staging registry is registered here the same way.
_loading: dict[tuple[str, int], Registry] = {}
_load_lock = threading.RLock()                               # one load at a time; other threads wait for its result
_extension_commands: dict[str, str] = {}                     # command kind -> origin that registered it (process)
# Name ownership, since the last reset(): lane / python job name -> (origin, extension) that registered it.
_extension_adapters: dict[str, tuple[str, str]] = {}
_extension_jobs: dict[str, tuple[str, str]] = {}
# Every name an extension registered through the registry in this process (forget_registrations() drops them).
_ever_adapters: set[str] = set()
_ever_jobs: set[str] = set()


def _taken(name: str, owners: dict[str, tuple[str, str]], ever: set[str], origin: str) -> bool:
    """Whether `name`, already registered to another object, is closed to the extension at `origin`: owned by
    another extension since the last reset, or never registered through the registry at all (a built-in lane, or
    one an old-style extension registered at import). A name an extension registered before the last reset is
    open again: reset() means the projects are being loaded anew."""
    if name in owners:
        return owners[name][0] != origin
    return name not in ever


# Registry fields that record the outcome of a load rather than what a setup() registered: not put back on failure.
_OUTCOME_FIELDS = frozenset({"layout", "loaded", "errors", "blocking", "internal_imports", "unmanifested",
                             "_setting_up"})


def _apply(table: str, name: str, obj, origin: str, owner: str) -> None:
    """Write one process-wide registration: a lane, a python job or a command kind, with who owns the name."""
    if table == "adapter":
        from .adapters.base import register
        register(obj)
        _extension_adapters[name] = (origin, owner)
        _ever_adapters.add(name)
    elif table == "python_job":
        from .adapters.python import JOBS
        JOBS[name] = obj
        _extension_jobs[name] = (origin, owner)
        _ever_jobs.add(name)
    elif table == "command":
        from . import commands as commands_mod
        commands_mod.HANDLERS[name] = obj
        _extension_commands[name] = origin
    else:                                                  # pragma: no cover - a programming error
        raise ValueError(f"unknown process-wide table {table!r}")


def _built_in(kind: str) -> set[str]:
    """The identifiers of `kind` the framework itself registers (never through an extension's registry)."""
    if kind == "adapters":
        from .adapters import REGISTRY
        return {n for n in REGISTRY if n not in _ever_adapters}
    if kind == "python_jobs":
        from .adapters.python import JOBS
        return {n for n in JOBS if n not in _ever_jobs}
    if kind == "commands":
        from . import commands as commands_mod
        return {k for k in commands_mod.HANDLERS if k not in _extension_commands}
    if kind == "inbox_kinds":
        from . import inbox as inbox_mod
        return set(inbox_mod.BUILTIN_KINDS)
    return set()


def _approve(finds: dict) -> dict[str, str]:
    """{extension: why it is refused} for what the manifests decide before any import: the framework range, built-in
    identifiers, identifiers two manifests claim (both refused: which one is meant is the owner's call), and
    requires.extensions that project.yaml does not list."""
    refused: dict[str, str] = {}
    claims: dict[tuple[str, str], list[str]] = {}
    for name, found in finds.items():
        m = found.manifest
        if m is None:
            continue
        why = m.unsupported_reason()
        if why:
            refused[name] = why
            continue
        for kind, identifiers in m.provides.items():
            if kind == "hooks":
                continue
            builtin = _built_in(kind)
            for identifier in identifiers:
                if identifier in builtin:
                    refused.setdefault(name, f"its manifest claims {kind} {identifier!r}, which is built into the "
                                             f"framework")
                claims.setdefault((kind, identifier), []).append(name)
        if m.config is not None:
            from .ext._config import framework_sections
            owned = sorted(set(m.config.legacy_sections) & framework_sections())
            if owned:
                refused.setdefault(name, f"its config.legacy_sections name {', '.join(owned)}, which the "
                                         f"framework owns")
            for section in m.config.legacy_sections:
                claims.setdefault(("config.legacy_sections", section), []).append(name)
        unlisted = [d for d in m.requires.extensions if d not in finds]
        if unlisted:
            refused.setdefault(name, f"it requires {', '.join(unlisted)}, which project.yaml does not list")
    for (kind, identifier), names in claims.items():
        if len(names) > 1:
            for name in names:
                others = ", ".join(n for n in names if n != name)
                refused.setdefault(name, f"{kind} {identifier!r} is claimed by the manifests of {name} and {others}")
    return refused


@dataclass(frozen=True)
class Approval:
    """What the manifests decide before any extension is imported (approval()): each configured name's discovery,
    the names refused with why, and the load order (project.yaml's, each extension after what it requires)."""
    finds: dict
    refused: dict[str, str]
    entries: list[tuple[str, bool]]

    def approved(self) -> list:
        """The manifests of the extensions with one that are not refused, in load order."""
        return [self.finds[n].manifest for n, _r in self.entries
                if n not in self.refused and not self.finds[n].problems and self.finds[n].manifest is not None]


_refused_modules: dict[str, str] = {}                        # module name -> why its extension was refused


class _RefusalGuard(importlib.abc.MetaPathFinder):
    """Refuses importing the module of a refused configured extension, by any path (review 1.12.0 R3): a sibling's
    `from . import <refused>`, the extensions/ package initializer, a schema or migration module. Without it the
    loader skipped only its own import, and the refused module's import-time code still ran."""

    def find_spec(self, fullname, path=None, target=None):
        for name, why in _refused_modules.items():
            if fullname == name or fullname.startswith(name + "."):
                raise ImportError(f"{fullname} belongs to a refused extension: {why}", name=fullname)
        return None


_GUARD = _RefusalGuard()


def guard_refused(layout: Layout, plan: "Approval") -> None:
    """Close the import paths to every extension `plan` refused, for the rest of the process (until reset()): its
    project-local module under the project's namespace, and an installed one's module."""
    if _GUARD not in sys.meta_path:
        sys.meta_path.insert(0, _GUARD)
    for name, why in plan.refused.items():
        found = plan.finds.get(name)
        names = {f"{_package_name(layout)}.{name}"}
        if found is not None and found.source in ("installed", "module"):
            names.add(found.module or name)
        for module in names:
            _refused_modules[module] = why


def approval(layout: Layout) -> Approval:
    """Import-free approval of every configured extension, shared by the loader and `supervisor migrate` (review
    1.12.0 R1). A name is refused for a discovery problem, an excluded framework range, a built-in or doubly claimed
    identifier or legacy section, an unlisted requirement, a requires cycle, or requiring a refused extension. Setup
    failures are not decided here: they happen later, in load()."""
    entries = specs(layout)
    finds = {name: discovery.find(layout, name) for name, _required in entries}
    refused = _approve(finds)
    for name, found in finds.items():
        if found.problems:
            refused.setdefault(name, "; ".join(found.problems))
    entries, cycle = _load_order(entries, finds)
    for name in cycle:
        refused.setdefault(name, f"its requires.extensions form a cycle ({' -> '.join(cycle)}); nothing in it loads")
    for name, _required in entries:                        # load order: a requirement is decided before its users
        m = finds[name].manifest
        bad = [d for d in (m.requires.extensions if m else ()) if d in refused]
        if bad and name not in refused:
            refused[name] = f"it requires {', '.join(bad)}, which is refused"
    return Approval(finds, refused, entries)


def _load_order(entries: list[tuple[str, bool]], finds: dict) -> tuple[list[tuple[str, bool]], list[str]]:
    """project.yaml's order, each extension moved only as far as the ones it requires make it (the first listed
    extension whose requirements are all placed goes next), and the names caught in a requires cycle (placed last:
    approval refuses them)."""
    requires = {name: [d for d in (finds[name].manifest.requires.extensions if finds[name].manifest else ())
                       if d in finds] for name, _ in entries}
    placed: list[tuple[str, bool]] = []
    done: set[str] = set()
    waiting = list(entries)
    while waiting:
        ready = next((e for e in waiting if all(d in done for d in requires[e[0]])), None)
        if ready is None:
            break
        placed.append(ready)
        done.add(ready[0])
        waiting.remove(ready)
    cycle = [name for name, _ in waiting]
    return placed + waiting, cycle


def _undeclared(m, registered: list[tuple[str, str]]) -> list[str]:
    """What setup() registered that manifest `m` does not declare in `provides`."""
    out = []
    for kind, identifier in registered:
        if identifier not in m.provides.get(kind, ()):
            out.append(f"{kind} {identifier!r}" if kind != "hooks" else f"the {identifier} hook (provides.hooks)")
    return sorted(set(out))


def _drop_modules(before: set[str], names: tuple[str, ...]) -> None:
    """Forget the modules an extension's failed load added, so the next load imports them afresh."""
    for key in [k for k in sys.modules if k not in before]:
        if any(key == n or key.startswith(n + ".") for n in names):
            del sys.modules[key]


def _holder(name: str, current, owners: dict[str, tuple[str, str]]) -> str:
    """Who has `name` already, for a refused registration."""
    if name in owners:
        return f"by extension {owners[name][1]!r}"
    module = getattr(current, "__module__", None) or type(current).__module__
    return "as a built-in" if module.startswith("supervisor.") else f"by {module} (at import)"


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


def import_named_module(layout: Layout, module: str):
    """A module a manifest names (its config schema's, its migrations), resolved like an extension name: the
    project's extensions/ first (under its namespace), else an installed module."""
    ext_dir = Path(layout.extensions)
    rel = Path(*module.split("."))
    if ext_dir.is_dir() and ((ext_dir / rel).with_suffix(".py").exists() or (ext_dir / rel / "__init__.py").exists()):
        return importlib.import_module(f"{_ensure_package(layout)}.{module}")
    return importlib.import_module(module)


def _import_schema(layout: Layout, spec: str):
    """The class a manifest's `config.schema` names (`module:Class`)."""
    module, _, cls = spec.partition(":")
    mod = import_named_module(layout, module)
    schema = getattr(mod, cls, None)
    if schema is None or not hasattr(schema, "model_validate"):
        raise ExtensionError(f"config.schema {spec!r}: {cls} is not a pydantic model in {module}")
    return schema


def _import(layout: Layout, name: str, module: str | None = None):
    """The project's `extensions/<name>.py` (loaded by file path under the project's namespace) or, when the
    project has no such file, an installed module: `module` when discovery found one (an entry point names its own
    module, e.g. `supervisor_ext_notes` for identity `notes`), else the dotted name itself."""
    if not _valid_name(name):
        raise ExtensionError(f"invalid extension name {name!r}: use identifiers (letters, digits, underscore), dotted for packages")
    ext_dir = Path(layout.extensions)
    rel = Path(*name.split("."))
    if ext_dir.is_dir() and ((ext_dir / rel).with_suffix(".py").exists() or (ext_dir / rel / "__init__.py").exists()):
        pkg_name = _ensure_package(layout)
        return importlib.import_module(f"{pkg_name}.{name}")
    try:
        return importlib.import_module(module or name)    # an installed package; never via a sys.path mutation
    except ModuleNotFoundError as exc:
        if exc.name in (name, name.split(".")[0]):
            raise ExtensionError(f"{layout.extensions / rel.with_suffix('.py')} not found and no installed module {name!r}") from None
        raise


def load(layout: Layout) -> Registry:
    """Import and set up every extension listed in project.yaml, once per project root per process. A broken
    extension is recorded in `registry.errors` (shown by doctor); a broken *required* one is also recorded in
    `registry.blocking`, and the loop and the packet screens refuse to proceed until it is fixed (`assert_loaded`).

    The registry is published only when the load has finished. Another thread calling load() meanwhile waits for it;
    the loading thread itself, re-entering through ext.config() in a setup(), gets the registry in progress."""
    key = str(layout.root)
    me = threading.get_ident()
    own = _loading.get((key, me))
    if own is not None:
        return own
    reg = _registries.get(key)
    if reg is not None:
        return reg
    with _load_lock:
        reg = _registries.get(key)
        if reg is not None:
            return reg                                     # finished while this thread waited
        reg = Registry(layout)
        _loading[(key, me)] = reg
        try:
            _load_into(layout, reg)
        finally:
            _loading.pop((key, me), None)
            _registries[key] = reg
        return reg


def _load_into(layout: Layout, reg: Registry) -> None:
    """load()'s work on the unpublished registry `reg`."""
    if project.exists(layout):
        _doc, note = project._read_raw(project.path(layout))
        if note:
            # A project.yaml that cannot be parsed hides its `extensions:` list — which may name the policy screens
            # this project relies on. Falling back to "no extensions" would silently drop that protection, so the
            # unreadable file blocks exactly like a required extension that failed to import.
            reg.errors.append(f"project.yaml: {note}")
            reg.blocking.append(f"project.yaml: {note} (its extensions: list cannot be read)")
            return
    modules = []
    plan = approval(layout)
    guard_refused(layout, plan)                            # before the first import, the package initializer's too
    finds, refused = plan.finds, plan.refused
    for name, required in plan.entries:
        snapshot = reg._snapshot()
        before = set(sys.modules)
        reg._pending, reg._registered = {}, []
        found = None
        try:
            found = finds[name]
            if found.problems:
                raise ExtensionError("; ".join(found.problems))
            if name in refused:
                raise ExtensionError(refused[name] + " (refused before import)")
            missing = [d for d in (found.manifest.requires.extensions if found.manifest else ()) if d not in reg.loaded]
            if missing:
                raise ExtensionError(f"it requires {', '.join(missing)}, which did not load (refused before import)")
            mod = _import(layout, name, found.module)
            modules.append(mod)
            if getattr(mod, "__file__", None):
                reg.origins[name] = str(Path(mod.__file__).resolve())
            if found.manifest is not None:
                reg.manifests[name] = found.manifest
                if found.manifest.config is not None:     # validated before setup(): invalid config keeps nothing
                    from .ext import _config
                    schema = _import_schema(layout, found.manifest.config.schema)
                    reg.config_schemas[name] = schema
                    _config.validated(layout, found.manifest, schema)
            setup = getattr(mod, "setup", None)
            if setup is None:
                raise ExtensionError(f"no setup(registry) function in {getattr(mod, '__file__', None) or mod.__name__}")
            reg._setting_up = name
            try:
                setup(reg)
            finally:
                reg._setting_up = None
            if found.manifest is not None:
                extra = _undeclared(found.manifest, reg._registered)
                if extra:
                    raise ExtensionError(f"setup() registered what {found.manifest.source} does not declare in "
                                         f"provides: {', '.join(extra)}; nothing of it was kept")
            else:
                reg.unmanifested.append(name)
            reg._commit()
            reg.loaded.append(name)
        except Exception as exc:                            # noqa: BLE001 - recorded; a required one blocks the loop
            reg._restore(snapshot)
            schema_module = (found.manifest.config.schema.split(":", 1)[0]
                             if found is not None and found.manifest is not None and found.manifest.config else name)
            installed = found.module if found is not None and found.module else name
            _drop_modules(before, (f"{_package_name(layout)}.{name}", name, installed,
                                   f"{_package_name(layout)}.{schema_module}", schema_module))
            why = str(exc) if isinstance(exc, ExtensionError) else traceback.format_exc(limit=3).strip().splitlines()[-1]
            reg.errors.append(f"{name}: {why}")
            if required:
                reg.blocking.append(f"{name}: {why}")
            else:
                _report_optional_failure(layout, name, why)
        finally:
            reg._pending, reg._registered = None, None
    reg._reconcile(set(reg.loaded))
    reg.internal_imports = internal_imports(layout, modules)
    _warn_internal_imports(layout, reg.internal_imports)


def _internal_names(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = ([(node.lineno, f"supervisor.{alias.name}") for alias in node.names] if node.module == "supervisor"
                     else [(node.lineno, node.module)])
        else:
            continue
        out += [(line, name) for line, name in names
                if (name == "supervisor" or name.startswith("supervisor."))
                and not any(name == p or name.startswith(p + ".") for p in PUBLIC_MODULES)]
    return out


def internal_imports(layout: Layout, modules=()) -> list[InternalImport]:
    """Every import of a framework internal (anything in `supervisor` but `supervisor.ext` and `supervisor.testing`)
    in the source of the project's extensions: every module in its extensions/ folder, helpers included, and the files
    of installed extension modules (a package: all its modules). Read as source (AST), never imported, so the
    framework's own imports never count; a file that does not parse is left to the loader. Sorted by file and line."""
    ext_dir = Path(layout.extensions).resolve()
    files = set(p.resolve() for p in ext_dir.rglob("*.py")) if ext_dir.is_dir() else set()
    for mod in modules:
        origin = getattr(mod, "__file__", None)
        if not origin:
            continue
        path = Path(origin).resolve()
        if ext_dir in path.parents:
            continue
        files |= set(p.resolve() for p in path.parent.rglob("*.py")) if path.name == "__init__.py" else {path}
    root = Path(layout.root).resolve()
    out = []
    for path in files:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        shown = path.relative_to(root).as_posix() if root in path.parents else str(path)
        out += [InternalImport(shown, line, name) for line, name in _internal_names(tree)]
    return sorted(set(out), key=lambda f: (f.file, f.line, f.module))


_warned: set[tuple[str, int, str]] = set()


def _warn_internal_imports(layout: Layout, found: list[InternalImport]) -> None:
    """One DeprecationWarning per import and process, reported at the extension's own file and line."""
    for f in found:
        key = (str(Path(layout.root).resolve()), f.line, f"{f.file}:{f.module}")
        if key in _warned:
            continue
        _warned.add(key)
        path = Path(f.file) if Path(f.file).is_absolute() else Path(layout.root) / f.file
        warnings.warn_explicit(f"{f.module} is a framework internal: extensions import supervisor.ext (and "
                               f"supervisor.testing in their tests) only. Deprecated since 1.11.0; a later major "
                               f"release refuses it (docs/EXTENSION_API.md)", DeprecationWarning, str(path), f.line)


def _report_optional_failure(layout: Layout, name: str, why: str) -> None:
    """A `required: false` extension that did not load is still a lane, a check or a screen that is now missing.
    Recording it only in `registry.errors` made it visible nowhere but `supervisor doctor`; it is logged once per
    process (`extension_error` in state/log.jsonl) and said on stderr, so the owner can tell a failed optional
    extension from one that was deliberately left out."""
    try:
        from . import tasks as tasks_mod                   # local: tasks imports policy, which reaches back here
        tasks_mod.log(layout, "extension_error", "extensions", extension=name, required=False, error=why[:500])
    except Exception:                                       # noqa: BLE001 - the log must never make a load fail
        pass
    print(f"extension {name!r} (required: false) did not load: {why}", file=sys.stderr)


def registry(layout: Layout) -> Registry:
    return load(layout)


def _owning_extension(module: str | None, file: str | None, names: list[str], ext_dir: Path) -> str | None:
    """The configured extension a module belongs to: by file under the project's extensions/ (as the loader imports
    it, or as a project's tests import it), else by dotted module name (an installed extension); None for a helper
    module that is not itself listed, and for everything else."""
    dotted = None
    if file:
        try:
            parts = list(Path(file).resolve().relative_to(ext_dir).with_suffix("").parts)
        except (ValueError, OSError):
            parts = []
        if parts:
            dotted = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    candidates = [dotted] if dotted is not None else ([module] if module else [])
    for name in sorted(names, key=len, reverse=True):
        if any(c == name or c.startswith(name + ".") for c in candidates):
            return name
    return None


def visible_registry(layout: Layout) -> Registry | None:
    """The registry this thread sees for `layout`, without loading: its own load in progress (or G5's staging
    registry), else the published one. Not another thread's unfinished load (review 1.12.0 R2)."""
    key = str(layout.root)
    return _loading.get((key, threading.get_ident())) or _registries.get(key)


def calling_extension(layout: Layout) -> str | None:
    """The configured extension whose code is running on this thread: the nearest caller whose module is one of the
    project's extensions. A helper module counts for the extension that calls it. Until phase 3 gives extensions
    an identity, this is how supervisor.ext knows who is asking (its config sections, the actor of its log lines)."""
    names = [name for name, _required in specs(layout)]
    ext_dir = Path(layout.extensions).resolve()
    reg = visible_registry(layout)
    origins = [(Path(f), n) for n, f in (reg.origins.items() if reg else ()) if n in names]
    frame = sys._getframe(1)
    while frame is not None:
        found = (_by_origin(frame.f_globals.get("__file__"), origins)
                 or _owning_extension(frame.f_globals.get("__name__"), frame.f_globals.get("__file__"), names, ext_dir))
        if found:
            return found
        frame = frame.f_back
    return None


@functools.lru_cache(maxsize=1024)
def _resolved(file: str) -> Path | None:
    try:
        return Path(file).resolve()
    except (OSError, ValueError):
        return None


def _by_origin(file: str | None, origins: list[tuple[Path, str]]) -> str | None:
    """The extension whose loaded module is this file (or, for a package, contains it): the same file imported
    under another name (a project's tests import `extensions.render` while the loader imports `render`) is the same
    extension."""
    path = _resolved(file) if file else None
    if path is None:
        return None
    for origin, name in origins:                       # the module itself first: `bundle.child`, not `bundle`
        if path == origin:
            return name
    packages = [(origin.parent, name) for origin, name in origins
                if origin.name == "__init__.py" and origin.parent in path.parents]
    if packages:                                       # then the innermost configured package that contains it
        return max(packages, key=lambda p: len(p[0].parts))[1]
    return None


def loaded(root) -> Registry | None:
    """The registry load() built for this project root in this process, if any — without loading anything."""
    return _registries.get(str(Layout(root).root))


def assert_loaded(layout: Layout) -> Registry:
    """The registry, or ExtensionError naming every required extension that did not load. Called before the loop
    dispatches and before any packet is cleared: a missing protection must stop work, not let it through."""
    reg = load(layout)
    reason = reg.blocking_reason()
    if reason:
        raise ExtensionError(reason)
    return reg


def reset() -> None:
    """Tests only: forget loaded registries, the per-project extension packages and the command kinds extensions
    registered. Lanes and python jobs extensions registered stay registered, as before 1.11.0 (a project's tests
    look them up after a reset); who owns their names is forgotten, so the next load (a fresh test project with
    the same extension) may register them again. `forget_registrations()` (supervisor.testing.reset) drops the
    lanes and jobs too, like a fresh process."""
    _registries.clear()
    _loading.clear()
    _refused_modules.clear()
    _warned.clear()
    _extension_adapters.clear()
    _extension_jobs.clear()
    from . import commands as commands_mod
    for kind in _extension_commands:
        commands_mod.HANDLERS.pop(kind, None)
    _extension_commands.clear()
    for key in [k for k in sys.modules if k == NAMESPACE or k.startswith(NAMESPACE + ".")]:
        del sys.modules[key]


def forget_registrations() -> None:
    """Tests only: drop the lanes and python jobs extensions registered, and whose names they were (built-in ones
    stay)."""
    from .adapters.base import REGISTRY
    from .adapters.python import JOBS
    for name in _ever_adapters:
        REGISTRY.pop(name, None)
    _ever_adapters.clear()
    _extension_adapters.clear()
    for name in _ever_jobs:
        JOBS.pop(name, None)
    _ever_jobs.clear()
    _extension_jobs.clear()


# Run by import_side_effects() in a fresh interpreter: import every framework module first (so what they register
# at their own import is not blamed on the extension), snapshot the process-wide registries, import the extension
# module the way the loader does, and print which entries the import added, replaced or removed.
_SIDE_EFFECTS_PROBE = """
import importlib, json, pkgutil, sys
import supervisor
for info in pkgutil.walk_packages(supervisor.__path__, "supervisor."):
    if info.name.rsplit(".", 1)[-1] == "__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
from supervisor import commands, extensions
from supervisor.adapters.base import REGISTRY
from supervisor.adapters.python import JOBS
from supervisor.paths import Layout

def snapshot():
    return {"adapters": {k: id(v) for k, v in REGISTRY.items()}, "python_jobs": {k: id(v) for k, v in JOBS.items()},
            "commands": {k: id(v) for k, v in commands.HANDLERS.items()}}

before = snapshot()
from supervisor import discovery
extensions._import(Layout(sys.argv[1]), sys.argv[2], discovery.find(Layout(sys.argv[1]), sys.argv[2]).module)
after = snapshot()
changed = {k: sorted(n for n in before[k].keys() | after[k].keys() if before[k].get(n) != after[k].get(n))
           for k in before}
print()
print(json.dumps(changed))
"""


def import_side_effects(layout: Layout, name: str, timeout_s: float = 120) -> dict[str, list[str]]:
    """What importing extension `name` (as the loader would) changes in the process-wide registries, measured in a
    fresh interpreter: {"adapters": [...], "python_jobs": [...], "commands": [...]}, each the names added, replaced
    or removed. All empty for an extension that registers only in setup(). Raises ExtensionError when the module
    does not import."""
    import json
    import os
    import subprocess
    framework = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": framework + (os.pathsep + os.environ["PYTHONPATH"]
                                                    if os.environ.get("PYTHONPATH") else "")}
    try:
        out = subprocess.run([sys.executable, "-c", _SIDE_EFFECTS_PROBE, str(layout.root), name], cwd=str(layout.root),
                             env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise ExtensionError(f"{name}: importing it in a fresh process took over {timeout_s:g} s") from None
    if out.returncode != 0:
        tail = (out.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise ExtensionError(f"{name}: does not import in a fresh process: {tail}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def scaffold_module(layout: Layout, name: str) -> Path:
    """Write a starter extension module under extensions/ and its manifest beside it (used by `supervisor extension
    new <name>`); neither is overwritten. As written, both pass `supervisor extension check <name>`."""
    from . import scaffold
    if not scaffold.IDENTITY.match(name):
        raise ExtensionError(f"invalid extension name {name!r}: lower case letters, digits and _, starting with a "
                             f"letter (it is the extension's identity)")
    layout.extensions.mkdir(parents=True, exist_ok=True)
    init = layout.extensions / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    p = layout.extensions / f"{name}.py"
    manifest_path = p.with_suffix(".toml")
    if not manifest_path.exists():
        manifest_path.write_text(scaffold.local_manifest(name), encoding="utf-8")
    if p.exists():
        return p
    p.write_text(f'''"""{name}: a project extension (see docs/FRAMEWORK_VS_EXTENSION.md in the framework repo).

Registered in project.yaml under `extensions: [{name}]`. Everything here is specific to THIS project.
Import only from `supervisor.ext` (the public extension API, docs/EXTENSION_API.md in the framework repo), and
register only inside setup(): importing this module must change nothing. Declare what setup() registers in
{name}.toml beside this file (`supervisor extension check {name}` proves the two agree).
"""
from __future__ import annotations


def setup(registry):
    # Examples — delete what you do not need:
    #
    # from supervisor.ext import Check
    # registry.doctor_check(lambda layout: Check("{name}", True, "ok"))
    #
    # registry.overview_panel(lambda layout: {{"title": "{name}", "rows": [("status", "ok")]}})
    #
    # from supervisor.ext import ProcessAdapter
    # class MyAdapter(ProcessAdapter):
    #     name = "my_lane"
    #     def prepare(self, layout, task, attempt_id): ...
    # registry.adapter(MyAdapter())
    pass
''', encoding="utf-8")
    return p
