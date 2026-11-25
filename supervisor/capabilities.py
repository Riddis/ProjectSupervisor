"""Lane capability data: what a lane can do, framework-known
keys only (`durable`, `network`, `max_duration_min`), read by `supervisor.needs`'s matcher and nothing else. The
framework states every value for its own five lanes; a plugin lane declares its own through the additive
`registry.capabilities(adapter, mapping)` plugin API entry (`supervisor.plugins.Registry.capabilities`), and one
that declares nothing at all gets {} here, which the matcher turns into a warning rather than a refusal
(`needs_lane_unknown`).

The table's contents, every value stated, so no reader has a judgement call left: `durable: false` on the three
model lanes (a continuation re-spends a session, which is not a computation resuming); absent on `process`/
`python`, where durability is the recipe's own (a template declares `resumable`). `network: true` on the four
lanes that run a command on this machine (the framework sandboxes nothing); `false` on `claude_api` alone, whose
tool-less Messages API turn runs no command. `max_duration_min` absent everywhere except `claude_api`, whose
limit is composed from its own configured `claude_api.timeout_s` -- a line of configuration, not a lane property.
"""
from __future__ import annotations

from .paths import Layout

_TABLE: dict[str, dict] = {
    "claude_cli": {"durable": False, "network": True},
    "codex_cli": {"durable": False, "network": True},
    "claude_api": {"durable": False, "network": False},
    "process": {"network": True},
    "python": {"network": True},
}

FRAMEWORK_LANES = frozenset(_TABLE)   # the five lanes whose capability data is the framework's, never a plugin's


def _claude_api_max_duration_min(layout: Layout) -> int:
    from .adapters import claude_api
    cfg = claude_api.api_config(layout)
    seconds = cfg.get("timeout_s", claude_api.DEFAULT_TIMEOUT_S)
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = claude_api.DEFAULT_TIMEOUT_S
    return max(1, seconds // 60)


def for_lane(layout: Layout, adapter: str | None) -> dict:
    """What `adapter` can do: a plugin lane's own `registry.capabilities` declaration when it has one, else the
    framework's table entry for one of its five lanes (with `claude_api`'s composed `max_duration_min`), else {}
    -- a lane that declares nothing at all, matched as `needs_lane_unknown` rather than refused."""
    if not adapter:
        return {}
    declared = _plugin_declared(layout, adapter)
    if declared is not None:
        return dict(declared)
    if adapter not in _TABLE:
        return {}
    out = dict(_TABLE[adapter])
    if adapter == "claude_api":
        out["max_duration_min"] = _claude_api_max_duration_min(layout)
    return out


def _plugin_declared(layout: Layout, adapter: str) -> dict | None:
    """A plugin lane's own capability mapping, when its setup() declared one for `adapter` -- None otherwise
    (never for one of the framework's own five lane names, which this table alone answers for: `registry.
    capabilities` itself refuses a plugin that names one of them, section 3.2's "not a shape to be filled in
    later")."""
    if adapter in FRAMEWORK_LANES:
        return None
    try:
        from . import plugins as plugins_mod
        reg = plugins_mod.load(layout)
    except Exception:                                   # noqa: BLE001 - a matcher must never fail on a bad config
        return None
    table = getattr(reg, "capabilities_table", None)
    return table.get(adapter) if isinstance(table, dict) else None
