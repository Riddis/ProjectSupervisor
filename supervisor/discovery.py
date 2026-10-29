"""Finding a plugin and reading its manifest without executing anything it ships (boundary plan phase 3;
docs/PLUGIN_STANDARD.md, "Where the manifest lives").

For a name in project.yaml `plugins:`, `find()` looks in two places:
- **Project-local:** `plugins/<name>.py` with `plugins/<name>.toml` beside it, or the package folder
  `plugins/<name>/` with `supervisor-plugin.toml` inside.
- **Installed:** a distribution whose entry point in the group `supervisor.plugins` has that name. Its manifest is
  read from the distribution's file list. For an editable install, whose file list shows only its finder, it is read
  beside the origin `importlib.util.find_spec` reports for the entry point's top-level package; finding a top-level
  package's spec does not execute it.

A project-local plugin wins only when nothing is installed under the same name. The same identity from two
sources is refused, not chosen silently (owner decision D5). A plugin found without a manifest is `legacy`: 1.12.x
still loads it, with a warning (D1). Nothing here imports a plugin, and nothing raises for a bad plugin:
problems are returned, so the loader and doctor can report each plugin on its own."""
from __future__ import annotations

import dataclasses
import importlib.machinery
import importlib.metadata
import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path

from . import manifest as manifest_mod
from .paths import Layout

GROUP = "supervisor.plugins"


@dataclass(frozen=True)
class Found:
    name: str
    source: str | None                       # "local", "installed", "module" (installed, no entry point), or None
    location: Path | None = None             # the module file or package folder
    manifest: manifest_mod.Manifest | None = None
    problems: tuple[str, ...] = ()
    module: str | None = None                # what to import for an installed one: its entry point's module

    @property
    def legacy(self) -> bool:
        """Found, usable, and without a manifest: loads in 1.12.x with a warning (D1)."""
        return self.source is not None and self.manifest is None and not self.problems


def find(layout: Layout, name: str) -> Found:
    """Where the plugin `name` is and what its manifest says. Executes nothing the plugin ships."""
    if not name or not all(part.isidentifier() for part in name.split(".")):
        return Found(name, None, problems=(f"invalid plugin name {name!r}: use identifiers, dotted for packages",))
    local = _local(layout, name)
    points = _entry_points(name)
    if local is not None and points:
        others = ", ".join(sorted({p.value for p in points}))
        return Found(name, None, problems=(
            (f"plugin {name!r} is found in two places: {local[0]} and the installed package {others}; remove "
             f"one or rename it (one identity, one source)"),))
    if len(points) > 1:
        return Found(name, None, problems=(
            f"plugin {name!r} is installed twice: {', '.join(sorted(p.value for p in points))}",))
    if local is not None:
        location, manifest_path = local
        return _with_manifest(Found(name, "local", location), manifest_path)
    if points:
        return _installed(name, points[0])
    spec = _top_level_spec(name.split(".")[0])
    if spec is not None:
        return Found(name, "module", Path(spec.origin) if spec.origin and "." not in name else None, module=name)
    expected = Path(layout.plugins) / Path(*name.split(".")).with_suffix(".py")
    return Found(name, None, problems=(
        (f"plugin {name!r} not found: no {expected}, no package folder there, no entry point {name!r} in the "
         f"group {GROUP!r} and no installed module"),))


def _local(layout: Layout, name: str) -> tuple[Path, Path] | None:
    """(module file or package folder, where its manifest would be) for a project-local plugin, else None."""
    ext_dir = Path(layout.plugins)
    rel = Path(*name.split("."))
    module, package = (ext_dir / rel).with_suffix(".py"), ext_dir / rel
    if module.is_file():
        return module, module.with_suffix(".toml")
    if (package / "__init__.py").is_file():
        return package, package / manifest_mod.FILENAME
    return None


def _entry_points(name: str) -> list[importlib.metadata.EntryPoint]:
    """Installed entry points named `name` in GROUP. Listing entry points reads metadata only; nothing is loaded."""
    return [ep for ep in importlib.metadata.entry_points(group=GROUP) if ep.name == name]


def _installed(name: str, ep: importlib.metadata.EntryPoint) -> Found:
    module = ep.value.split(":", 1)[0].strip()
    parts = module.split(".")
    wanted = "/".join(parts + [manifest_mod.FILENAME])
    dist = ep.dist
    for entry in (dist.files or []) if dist is not None else []:
        if str(entry).replace("\\", "/") == wanted:
            path = Path(dist.locate_file(entry))
            return _with_manifest(Found(name, "installed", path.parent, module=module), path)
    spec = _top_level_spec(parts[0])                      # an editable install: the file list shows only its finder
    if spec is not None and spec.submodule_search_locations:
        package = Path(next(iter(spec.submodule_search_locations))).joinpath(*parts[1:])
        return _with_manifest(Found(name, "installed", package, module=module), package / manifest_mod.FILENAME)
    return Found(name, "installed", module=module, problems=(
        (f"plugin {name!r}: the entry point names {module!r}, but neither the distribution's file list nor the "
         f"installed package shows its {manifest_mod.FILENAME}"),))


# Set while _top_level_spec() asks the import system for a spec: the loader's refusal guard stands aside for it, since
# finding a spec executes nothing the package ships. Approval is decided from what discovery finds, so one project's
# refusal must not hide a package's manifest from another project (review 1.12.0 R3-1). Importing stays guarded.
spec_lookup = threading.local()


def _top_level_spec(top: str) -> importlib.machinery.ModuleSpec | None:
    """The spec of top-level module `top`, found without executing it (a dotted name's spec would import its
    parent, so only top-level names are asked for)."""
    importlib.invalidate_caches()
    spec_lookup.active = True
    try:
        return importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None
    finally:
        spec_lookup.active = False


def _with_manifest(found: Found, manifest_path: Path) -> Found:
    if not manifest_path.is_file():
        if found.source == "installed":
            return dataclasses.replace(found, problems=(
                f"plugin {found.name!r}: no {manifest_mod.FILENAME} in {found.location}",))
        return found                                      # a project-local plugin without one: legacy (D1)
    try:
        m = manifest_mod.load(manifest_path)
    except manifest_mod.ManifestError as exc:
        return dataclasses.replace(found, problems=tuple(f"{exc.source}: {p}" for p in exc.problems))
    if m.identity != found.name:
        return dataclasses.replace(found, problems=(
            f"{manifest_path}: its identity is {m.identity!r}, but project.yaml names {found.name!r}",))
    return dataclasses.replace(found, manifest=m)
