"""The config facade (docs/PLUGIN_API.md, Configuration).

A plugin whose manifest has a `config` table owns `plugins.<identity>` in routing.yaml, validated by the
pydantic model the manifest names. `config(layout, <identity>)` is that whole section; `config(layout, <legacy
section>)` is its sub-section of that name. Until `supervisor migrate` has moved the values (1.12.0), they are read
from the old top-level sections the manifest lists in `config.legacy_sections`, with a DeprecationWarning. The section
named like the identity merges in at the top; any other nests under its own name.

A plugin without a manifest (or without a `config` table) keeps the phase-2 facade: the top-level sections it
declared with `registry.config_sections()`. The shared resource policy comes from `resources(layout)`: `resources.gpu`,
else the old top-level `gpu` (warned). Everything is a copy, read now."""
from __future__ import annotations

import functools
import warnings
from pathlib import Path

import yaml

from ..plugins import PluginError
from ..paths import Layout

# Shared resource policy every plugin may read (plugin.resources), and phase 3's namespaces: never a plugin's.
SHARED_SECTIONS = frozenset({"gpu", "resources", "plugins"})
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
        raise PluginError(f"config/routing.yaml cannot be read: {exc}") from None
    return doc if isinstance(doc, dict) else {}


def _mapping(doc: dict, section: str) -> dict:
    value = doc.get(section)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginError(f"config/routing.yaml: `{section}` must be a mapping, not {type(value).__name__}")
    return value                                   # parsed for this call: a copy nobody else holds


def _assembled(layout: Layout, m, doc: dict | None = None) -> dict:
    """The raw value of plugin `m`'s section: `plugins.<identity>`, or, before the migration, its legacy
    sections (warned)."""
    doc = _routing(layout) if doc is None else doc
    owned = _mapping(doc, "plugins")
    if m.identity in owned:
        return _mapping(owned, m.identity)
    present = [s for s in m.config.legacy_sections if s in doc]
    if not present:
        return {}
    warnings.warn(f"plugin {m.identity!r} reads its configuration from the routing.yaml sections "
                  f"{', '.join(present)}; since 1.12.0 it lives in plugins.{m.identity}. Run `supervisor migrate` "
                  f"to move it; a later major release reads only plugins.{m.identity}", DeprecationWarning,
                  stacklevel=3)
    out: dict = {}
    if m.identity in present:
        out.update(_mapping(doc, m.identity))
    for section in present:
        if section != m.identity:
            out[section] = _mapping(doc, section)
    return out


def validated(layout: Layout, m, schema) -> dict:
    """Plugin `m`'s section checked against its pydantic `schema`, with the schema's defaults filled in.
    PluginError naming every field that does not fit."""
    from pydantic import ValidationError
    try:
        return schema.model_validate(_assembled(layout, m)).model_dump(mode="python")
    except ValidationError as exc:
        fields = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(section)'}: {e['msg']}" for e in exc.errors())
        raise PluginError(f"config section plugins.{m.identity} does not match {m.config.schema}: "
                             f"{fields}") from None


def config(layout: Layout, section: str) -> dict:
    """For the plugin whose code calls this: with a manifest `config` table, its validated section
    (`section` = its identity) or a sub-section of it (`section` = one of its legacy sections); otherwise the
    routing.yaml section it declared with `registry.config_sections()`. A copy, read now ({} when absent). Raises
    PluginError when the caller is not a plugin's code, or the section is not the calling plugin's."""
    from .. import plugins
    reg = plugins.load(layout)
    who = plugins.calling_plugin(layout)
    if who is None:
        raise PluginError(f"plugin.config(layout, {section!r}) reads a plugin's own config: call it from that "
                             f"plugin's code (a module listed under plugins: in project.yaml, or code it calls)")
    m = reg.manifests.get(who)
    if m is not None and m.config is not None:
        if section != m.identity and section not in m.config.legacy_sections:
            raise PluginError(f"plugin {who!r} reads only its own section plugins.{m.identity}: "
                                 f"plugin.config(layout, {m.identity!r}), or a part of it named in its manifest's "
                                 f"config.legacy_sections, not {section!r}")
        value = validated(layout, m, reg.config_schemas[who])
        if section == m.identity:
            return value
        part = value.get(section)
        return dict(part) if isinstance(part, dict) else {}
    owner = reg.config_owners.get(section)
    if owner != who:
        whose = f"; the section is plugin {owner!r}'s" if owner else ""
        raise PluginError(f"plugin {who!r} did not declare the config section {section!r} with "
                             f"registry.config_sections() in its setup(){whose}")
    return _mapping(_routing(layout), section)


def gpu_device(policy: dict, device: str | int | None) -> dict | None:
    """The card a plugin's `device` setting names, resolved against the shared GPU resource policy.

    `policy` is `resources(layout)["gpu"]`. `device` is a key of its `devices` mapping (a project names its own
    cards), or a bare integer index for a project that configures none, or None.

    Returns {"name": str | None, "index": int, "vram_floor_mb": int | None}: the entry's key and index and the
    entry's own floor when it declares one, or None for a bare index. Returns None when `device` is None - which
    is every project that sets nothing, and the behaviour every 2.0..2.2 caller has.

    Raises PluginError when the policy's `devices` entry itself is not a mapping, when `device` names an entry
    it does not hold, when that entry is not a mapping, when its `index` is missing or is not a non-negative
    integer, or when its declared floor is not an integer: a misconfigured device is a named refusal, never a
    silent fall back to the first card.
    """
    if device is None:
        return None
    if isinstance(device, bool) or not isinstance(device, (str, int)):
        raise PluginError(f"device {device!r} must be a device name or a non-negative integer index")
    if isinstance(device, int):
        if device < 0:
            raise PluginError(f"device {device!r} must be a non-negative integer index")
        return {"name": None, "index": device, "vram_floor_mb": None}
    devices = (policy or {}).get("devices") or {}
    if not isinstance(devices, dict):
        raise PluginError(f"resources.gpu.devices must be a mapping, not {type(devices).__name__}")
    entry = devices.get(device)
    if entry is None:
        raise PluginError(f"device {device!r} is not in resources.gpu.devices")
    if not isinstance(entry, dict):
        raise PluginError(f"resources.gpu.devices.{device} must be a mapping, not {type(entry).__name__}")
    index = entry.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise PluginError(f"resources.gpu.devices.{device}.index must be a non-negative integer")
    floor = entry.get("usable_vram_floor_mb")
    if floor is not None and (isinstance(floor, bool) or not isinstance(floor, int)):
        raise PluginError(f"resources.gpu.devices.{device}.usable_vram_floor_mb must be an integer")
    return {"name": device, "index": index, "vram_floor_mb": floor}


def resources(layout: Layout) -> dict:
    """The shared resource policy: {"gpu": {...}} from routing.yaml's `resources.gpu`, else the old top-level `gpu`
    (warned), {} inside when absent. A plugin with a manifest sees only what it declares in `requires.resources`;
    one without sees all of it, as before. A copy, read now."""
    from .. import plugins
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
    reg = plugins.load(layout)
    who = plugins.calling_plugin(layout)
    m = reg.manifests.get(who) if who else None
    return {k: v for k, v in policy.items() if k in m.requires.resources} if m is not None else policy
