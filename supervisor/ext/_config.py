"""The config facade (docs/EXTENSION_API.md, Configuration).

An extension whose manifest has a `config` table owns `extensions.<identity>` in routing.yaml, validated by the
pydantic model the manifest names. `config(layout, <identity>)` is that whole section; `config(layout, <legacy
section>)` is its sub-section of that name. Until `supervisor migrate` has moved the values (1.12.0), they are read
from the old top-level sections the manifest lists in `config.legacy_sections`, with a DeprecationWarning. The section
named like the identity merges in at the top; any other nests under its own name.

An extension without a manifest (or without a `config` table) keeps the phase-2 facade: the top-level sections it
declared with `registry.config_sections()`. The shared resource policy comes from `resources(layout)`: `resources.gpu`,
else the old top-level `gpu` (warned). Everything is a copy, read now."""
from __future__ import annotations

import functools
import warnings
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


def _assembled(layout: Layout, m, doc: dict | None = None) -> dict:
    """The raw value of extension `m`'s section: `extensions.<identity>`, or, before the migration, its legacy
    sections (warned)."""
    doc = _routing(layout) if doc is None else doc
    owned = _mapping(doc, "extensions")
    if m.identity in owned:
        return _mapping(owned, m.identity)
    present = [s for s in m.config.legacy_sections if s in doc]
    if not present:
        return {}
    warnings.warn(f"extension {m.identity!r} reads its configuration from the routing.yaml sections "
                  f"{', '.join(present)}; since 1.12.0 it lives in extensions.{m.identity}. Run `supervisor migrate` "
                  f"to move it; a later major release reads only extensions.{m.identity}", DeprecationWarning,
                  stacklevel=3)
    out: dict = {}
    if m.identity in present:
        out.update(_mapping(doc, m.identity))
    for section in present:
        if section != m.identity:
            out[section] = _mapping(doc, section)
    return out


def validated(layout: Layout, m, schema) -> dict:
    """Extension `m`'s section checked against its pydantic `schema`, with the schema's defaults filled in.
    ExtensionError naming every field that does not fit."""
    from pydantic import ValidationError
    try:
        return schema.model_validate(_assembled(layout, m)).model_dump(mode="python")
    except ValidationError as exc:
        fields = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(section)'}: {e['msg']}" for e in exc.errors())
        raise ExtensionError(f"config section extensions.{m.identity} does not match {m.config.schema}: "
                             f"{fields}") from None


def config(layout: Layout, section: str) -> dict:
    """For the extension whose code calls this: with a manifest `config` table, its validated section
    (`section` = its identity) or a sub-section of it (`section` = one of its legacy sections); otherwise the
    routing.yaml section it declared with `registry.config_sections()`. A copy, read now ({} when absent). Raises
    ExtensionError when the caller is not an extension's code, or the section is not the calling extension's."""
    from .. import extensions
    reg = extensions.load(layout)
    who = extensions.calling_extension(layout)
    if who is None:
        raise ExtensionError(f"ext.config(layout, {section!r}) reads an extension's own config: call it from that "
                             f"extension's code (a module listed under extensions: in project.yaml, or code it calls)")
    m = reg.manifests.get(who)
    if m is not None and m.config is not None:
        if section != m.identity and section not in m.config.legacy_sections:
            raise ExtensionError(f"extension {who!r} reads only its own section extensions.{m.identity}: "
                                 f"ext.config(layout, {m.identity!r}), or a part of it named in its manifest's "
                                 f"config.legacy_sections, not {section!r}")
        value = validated(layout, m, reg.config_schemas[who])
        if section == m.identity:
            return value
        part = value.get(section)
        return dict(part) if isinstance(part, dict) else {}
    owner = reg.config_owners.get(section)
    if owner != who:
        whose = f"; the section is extension {owner!r}'s" if owner else ""
        raise ExtensionError(f"extension {who!r} did not declare the config section {section!r} with "
                             f"registry.config_sections() in its setup(){whose}")
    return _mapping(_routing(layout), section)


def resources(layout: Layout) -> dict:
    """The shared resource policy: {"gpu": {...}} from routing.yaml's `resources.gpu`, else the old top-level `gpu`
    (warned), {} inside when absent. An extension with a manifest sees only what it declares in `requires.resources`;
    one without sees all of it, as before. A copy, read now."""
    from .. import extensions
    doc = _routing(layout)
    shared = _mapping(doc, "resources")
    if "gpu" in shared:
        gpu = _mapping(shared, "gpu")
    elif "gpu" in doc:
        warnings.warn("routing.yaml's top-level `gpu` section is the shared resource policy resources.gpu since "
                      "1.12.0; run `supervisor migrate` to move it", DeprecationWarning, stacklevel=2)
        gpu = _mapping(doc, "gpu")
    else:
        gpu = {}
    policy = {"gpu": gpu}
    reg = extensions.load(layout)
    who = extensions.calling_extension(layout)
    m = reg.manifests.get(who) if who else None
    return {k: v for k, v in policy.items() if k in m.requires.resources} if m is not None else policy
