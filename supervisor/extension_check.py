"""Gate G5: `supervisor extension check <name>`, whether one extension conforms to the standard
(docs/EXTENSION_STANDARD.md). It checks conformance only, and never runs the extension's own tests: the G5 workflow
runs those beside this command, against the oldest and newest framework in the extension's range.

| Check | What it proves |
| --- | --- |
| `manifest` | the manifest is found, read and valid, without importing anything |
| `framework` | the framework range includes the installed framework |
| `imports` | the extension's source imports `supervisor.ext` only (`supervisor.testing` too, in its tests) |
| `import_side_effects` | importing the module in a fresh interpreter changes no shared registry |
| `registrations` | setup() registers exactly what `provides` declares, no more and no less |
| `idempotent` | a second setup() registers the same |
| `config` | the project's section validates against the manifest's schema |

setup() runs against a staging registry that is never committed, so the process-wide tables are untouched. Doctor
check names are known only when a check runs, so `supervisor doctor` compares those with `provides`."""
from __future__ import annotations

import ast
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import discovery, extensions
from .manifest import FILENAME
from .paths import Layout

CHECKS = ("manifest", "framework", "imports", "import_side_effects", "registrations", "idempotent", "config")


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str


def run(layout: Layout, name: str) -> list[Finding]:
    """Every check, in CHECKS order, for extension `name` of the project at `layout`."""
    out: dict[str, Finding] = {}
    found = discovery.find(layout, name)
    m = found.manifest
    if found.problems:
        out["manifest"] = Finding("manifest", False, "; ".join(found.problems))
    elif m is None:
        out["manifest"] = Finding("manifest", False, f"no {FILENAME}: G5 checks an extension against its manifest "
                                                     f"(docs/EXTENSION_STANDARD.md)")
    else:
        out["manifest"] = Finding("manifest", True, f"{m.identity} {m.version}, read without import ({m.source})")
    if m is None:
        return [out["manifest"]] + [Finding(n, False, "not run: needs a valid manifest") for n in CHECKS[1:]]
    why = m.unsupported_reason()
    out["framework"] = Finding("framework", why is None, why or f"{m.framework} includes this framework")
    out["imports"] = _imports(layout, found)
    out["import_side_effects"] = _side_effects(layout, name)
    if why:
        for check in ("registrations", "idempotent", "config"):
            out[check] = Finding(check, False, "not run: the framework range excludes this framework")
        return [out[n] for n in CHECKS]
    first = _staged(layout, name, m)
    out["config"] = Finding("config", first[2] is None, first[2] or (
        f"extensions.{m.identity} validates against {m.config.schema}" if m.config else "no config table"))
    if first[1] is not None:
        out["registrations"] = Finding("registrations", False, f"setup() raised: {first[1]}")
        out["idempotent"] = Finding("idempotent", False, "not run: setup() did not complete")
        return [out[n] for n in CHECKS]
    out["registrations"] = _compare(m, first[0])
    second = _staged(layout, name, m)
    same = second[1] is None and second[0] == first[0]
    out["idempotent"] = Finding("idempotent", same, "a second setup() registers the same" if same else (
        f"a second setup() raised: {second[1]}" if second[1] else
        f"a second setup() registers differently: {_describe(first[0])} the first time, {_describe(second[0])} then"))
    return [out[n] for n in CHECKS]


def _files(found: discovery.Found) -> list[Path]:
    location = found.location
    if location is None:
        return []
    if location.is_dir():
        return sorted(p for p in location.rglob("*.py") if "__pycache__" not in p.parts)
    return [location]


def _imports(layout: Layout, found: discovery.Found) -> Finding:
    root = Path(layout.root).resolve()
    bad = []
    for path in _files(found):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            bad.append(f"{path}: does not parse ({exc})")
            continue
        base = found.location if found.location.is_dir() else found.location.parent
        in_tests = "tests" in path.relative_to(base).parts[:-1]
        names = list(extensions._internal_names(tree))
        if not in_tests:
            names += [(node.lineno, n) for node in ast.walk(tree) for n in _testing_imports(node)]
        shown = path.resolve().relative_to(root).as_posix() if root in path.resolve().parents else str(path)
        bad += [f"{shown}:{line} {module}" for line, module in sorted(set(names))]
    if bad:
        return Finding("imports", False, "imports of framework internals (import supervisor.ext only): "
                                         + "; ".join(bad[:8]) + (f" (+{len(bad) - 8} more)" if len(bad) > 8 else ""))
    return Finding("imports", True, "supervisor.ext only")


def _testing_imports(node) -> list[str]:
    """supervisor.testing imported outside an extension's tests."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names
                if a.name == "supervisor.testing" or a.name.startswith("supervisor.testing.")]
    if isinstance(node, ast.ImportFrom) and not node.level and node.module:
        if node.module == "supervisor" and any(a.name == "testing" for a in node.names):
            return ["supervisor.testing"]
        if node.module == "supervisor.testing" or node.module.startswith("supervisor.testing."):
            return [node.module]
    return []


def _side_effects(layout: Layout, name: str) -> Finding:
    try:
        effects = extensions.import_side_effects(layout, name)
    except extensions.ExtensionError as exc:
        return Finding("import_side_effects", False, str(exc))
    changed = {k: v for k, v in effects.items() if v}
    if changed:
        return Finding("import_side_effects", False, "importing the module registers, outside setup(): " + "; ".join(
            f"{kind} {', '.join(names)}" for kind, names in changed.items()))
    return Finding("import_side_effects", True, "importing the module changes no shared registry")


def _staged(layout: Layout, name: str, m) -> tuple[Counter, str | None, str | None]:
    """(what setup() registered, why setup() failed or None, why the config failed or None), against a staging
    registry that is never committed. It stands in for the project's registry for the length of the call, and only on
    this thread (extensions._loading): any other thread keeps the published registry (review 1.12.0 R2)."""
    from .ext import _config
    reg = extensions.Registry(layout)
    slot = (str(layout.root), threading.get_ident())
    extensions._loading[slot] = reg
    config_problem = None
    try:
        mod = extensions._import(layout, name, discovery.find(layout, name).module)
        if getattr(mod, "__file__", None):
            reg.origins[name] = str(Path(mod.__file__).resolve())
        reg.manifests[name] = m
        if m.config is not None:
            schema = extensions._import_schema(layout, m.config.schema)
            reg.config_schemas[name] = schema
            try:
                _config.validated(layout, m, schema)
            except extensions.ExtensionError as exc:
                config_problem = str(exc)
        setup = getattr(mod, "setup", None)
        if setup is None:
            return Counter(), "no setup(registry) function", config_problem
        reg._pending, reg._registered = {}, []
        reg._setting_up = name
        try:
            setup(reg)
        finally:
            reg._setting_up = None
        return Counter(reg._registered), None, config_problem
    except Exception as exc:                                # noqa: BLE001 - reported as the check's finding
        return Counter(), f"{type(exc).__name__}: {exc}", config_problem
    finally:
        reg._pending = None
        extensions._loading.pop(slot, None)


def _compare(m, registered: Counter) -> Finding:
    declared = {(kind, i) for kind, ids in m.provides.items() if kind != "doctor_checks" for i in ids}
    made = set(registered)
    extra, missing = sorted(made - declared), sorted(declared - made)
    if not extra and not missing:
        return Finding("registrations", True, f"exactly what provides declares ({_describe(registered)})")
    parts = []
    if extra:
        parts.append("not declared: " + ", ".join(f"{k} {i!r}" for k, i in extra))
    if missing:
        parts.append("declared but not registered: " + ", ".join(f"{k} {i!r}" for k, i in missing))
    return Finding("registrations", False, "; ".join(parts))


def _describe(registered: Counter) -> str:
    return ", ".join(f"{k} {i}" + (f" x{n}" if n > 1 else "") for (k, i), n in sorted(registered.items())) or "nothing"
