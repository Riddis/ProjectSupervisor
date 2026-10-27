"""The transitional config facade (docs/EXTENSION_API.md, Configuration): an extension reads the routing.yaml
sections it declared with `registry.config_sections()`, and the shared resource policy, as copies. Phase 3 keeps
these signatures and changes only where the values come from (`extensions.<identity>`, `resources.gpu`)."""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

from ..extensions import ExtensionError
from ..paths import Layout

# Shared resource policy every extension may read (ext.resources), and phase 3's namespaces: never an extension's.
SHARED_SECTIONS = frozenset({"gpu", "resources", "extensions"})
_TEMPLATE_ROUTING = Path(__file__).resolve().parents[1] / "template" / "config" / "routing.yaml"


@functools.lru_cache(maxsize=1)
def framework_sections() -> frozenset[str]:
    """The routing.yaml sections the framework owns: every top-level key of the template's routing.yaml (the engine
    settings), plus the shared resource policy and the namespaces phase 3 adds."""
    doc = yaml.safe_load(_TEMPLATE_ROUTING.read_text(encoding="utf-8")) or {}
    return frozenset(doc) | SHARED_SECTIONS


def _routing(layout: Layout) -> dict:
    path = layout.config / "routing.yaml"
    if not path.exists():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExtensionError(f"config/routing.yaml cannot be read: {exc}") from None
    return doc if isinstance(doc, dict) else {}


def _mapping(doc: dict, section: str) -> dict:
    value = doc.get(section)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError(f"config/routing.yaml: `{section}` must be a mapping, not {type(value).__name__}")
    return value                                   # parsed for this call: a copy nobody else holds


def config(layout: Layout, section: str) -> dict:
    """The routing.yaml section `section`, for the extension whose code calls this: a copy, read now ({} when the
    file has no such section). Raises ExtensionError when the caller is not an extension's code, or when the calling
    extension did not declare the section with `registry.config_sections()` in its setup()."""
    from .. import extensions
    reg = extensions.load(layout)
    who = extensions.calling_extension(layout)
    if who is None:
        raise ExtensionError(f"ext.config(layout, {section!r}) reads an extension's own config: call it from that "
                             f"extension's code (a module listed under extensions: in project.yaml, or code it calls)")
    owner = reg.config_owners.get(section)
    if owner != who:
        whose = f"; the section is extension {owner!r}'s" if owner else ""
        raise ExtensionError(f"extension {who!r} did not declare the config section {section!r} with "
                             f"registry.config_sections() in its setup(){whose}")
    return _mapping(_routing(layout), section)


def resources(layout: Layout) -> dict:
    """The shared resource policy, open to every extension: {"gpu": {...}}, a copy of routing.yaml's `gpu` section
    ({} when absent), read now. Phase 3 moves it to `resources.gpu`; this shape stays."""
    return {"gpu": _mapping(_routing(layout), "gpu")}
