"""Finding an extension and reading its manifest without executing anything it ships (boundary plan phase 3;
docs/EXTENSION_STANDARD.md, "Where the manifest lives").

For a name in project.yaml `extensions:`, `find()` looks in two places:
- **Project-local:** `extensions/<name>.py` with `extensions/<name>.toml` beside it, or the package folder
  `extensions/<name>/` with `supervisor-extension.toml` inside.
- **Installed:** a distribution whose entry point in the group `supervisor.extensions` has that name. Its manifest is
  read from the distribution's file list. For an editable install, whose file list shows only its finder, it is read
  beside the origin `importlib.util.find_spec` reports for the entry point's top-level package; finding a top-level
  package's spec does not execute it.

A project-local extension wins only when nothing is installed under the same name. The same identity from two
sources is refused, not chosen silently (owner decision D5). An extension found without a manifest is `legacy`: 1.12.x
still loads it, with a warning (D1). Nothing here imports an extension, and nothing raises for a bad extension:
problems are returned, so the loader and doctor can report each extension on its own."""
from __future__ import annotations

import importlib.machinery
import importlib.metadata
import importlib.util
from dataclasses import dataclass
from pathlib import Path

from . import manifest as manifest_mod
from .paths import Layout

GROUP = "supervisor.extensions"


@dataclass(frozen=True)
class Found:
    name: str
    source: str | None                       # "local", "installed", "module" (installed, no entry point), or None
    location: Path | None = None             # the module file or package folder
    manifest: manifest_mod.Manifest | None = None
    problems: tuple[str, ...] = ()

    @property
    def legacy(self) -> bool:
        """Found, usable, and without a manifest: loads in 1.12.x with a warning (D1)."""
        return self.source is not None and self.manifest is None and not self.problems


def find(layout: Layout, name: str) -> Found:
    """Where the extension `name` is and what its manifest says. Executes nothing the extension ships."""
    if not name or not all(part.isidentifier() for part in name.split(".")):
        return Found(name, None, problems=(f"invalid extension name {name!r}: use identifiers, dotted for packages",))
    local = _local(layout, name)
    points = _entry_points(name)
    if local is not None and points:
        others = ", ".join(sorted({p.value for p in points}))
        return Found(name, None, problems=(
            (f"extension {name!r} is found in two places: {local[0]} and the installed package {others}; remove "
             f"one or rename it (one identity, one source)"),))
    if len(points) > 1:
        return Found(name, None, problems=(
            f"extension {name!r} is installed twice: {', '.join(sorted(p.value for p in points))}",))
    if local is not None:
        location, manifest_path = local
        return _with_manifest(Found(name, "local", location), manifest_path)
    if points:
        return _installed(name, points[0])
    spec = _top_level_spec(name.split(".")[0])
    if spec is not None:
        return Found(name, "module", Path(spec.origin) if spec.origin and "." not in name else None)
    expected = Path(layout.extensions) / Path(*name.split(".")).with_suffix(".py")
    return Found(name, None, problems=(
        (f"extension {name!r} not found: no {expected}, no package folder there, no entry point {name!r} in the "
         f"group {GROUP!r} and no installed module"),))


def _local(layout: Layout, name: str) -> tuple[Path, Path] | None:
    """(module file or package folder, where its manifest would be) for a project-local extension, else None."""
    ext_dir = Path(layout.extensions)
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
            return _with_manifest(Found(name, "installed", path.parent), path)
    spec = _top_level_spec(parts[0])                      # an editable install: the file list shows only its finder
    if spec is not None and spec.submodule_search_locations:
        package = Path(next(iter(spec.submodule_search_locations))).joinpath(*parts[1:])
        return _with_manifest(Found(name, "installed", package), package / manifest_mod.FILENAME)
    return Found(name, "installed", problems=(
        (f"extension {name!r}: the entry point names {module!r}, but neither the distribution's file list nor the "
         f"installed package shows its {manifest_mod.FILENAME}"),))


def _top_level_spec(top: str) -> importlib.machinery.ModuleSpec | None:
    """The spec of top-level module `top`, found without executing it (a dotted name's spec would import its
    parent, so only top-level names are asked for)."""
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None


def _with_manifest(found: Found, manifest_path: Path) -> Found:
    if not manifest_path.is_file():
        if found.source == "installed":
            return Found(found.name, found.source, found.location,
                         problems=(f"extension {found.name!r}: no {manifest_mod.FILENAME} in {found.location}",))
        return found                                      # a project-local extension without one: legacy (D1)
    try:
        m = manifest_mod.load(manifest_path)
    except manifest_mod.ManifestError as exc:
        problems = tuple(f"{exc.source}: {p}" for p in exc.problems)
        return Found(found.name, found.source, found.location, problems=problems)
    if m.identity != found.name:
        return Found(found.name, found.source, found.location, problems=(
            f"{manifest_path}: its identity is {m.identity!r}, but project.yaml names {found.name!r}",))
    return Found(found.name, found.source, found.location, manifest=m)
