"""The public plugin API (docs/PLUGIN_API.md): the only part of `supervisor` a plugin imports, besides the
explicitly unstable `supervisor.testing`. `__all__` is the contract: gate G2 (`python -m supervisor.api_contract
check`) holds it to docs/plugin_api.json and to the last release's contract.

Modules in this package may import framework internals; nothing outside it imports from it, so dependencies point one
way. A name is exported only when a plugin needs it (docs/BOUNDARY.md); everything else stays internal. `ui`
needs the framework's `ui` extra and is loaded on first use."""
from __future__ import annotations

from .. import gpu
from .._vendor.contract import CapacityDeferred, atomic_json, atomic_write_text, contained, read_json
from ..adapters.base import Adapter, Result
from ..adapters.process import ProcessAdapter, job_spec
from ..doctor import Check
from ..plugins import PluginError
from ..inbox import InboxError
from ..paths import Layout
from ..store import now_iso
from . import policy
from ._config import config, resources
from ._inbox import close_owned_item, owned_item, owned_items, update_owned_item
from ._lanes import AgentLane, agent_lane
from ._ops import log_event, submit_command
from ._registry import Registry

# Grouped by purpose in docs/PLUGIN_API.md: registration (Registry, PluginError); configuration (config,
# resources); project paths (Layout); files (atomic_json, atomic_write_text, contained, read_json); job lanes
# (Adapter, CapacityDeferred, ProcessAdapter, Result, job_spec); agent lanes (agent_lane, AgentLane); inbox items
# (owned_items, owned_item, update_owned_item, close_owned_item, InboxError); commands and the log (submit_command,
# log_event); policy (a module); Control Center pages (ui, a module); doctor checks (Check); the shared GPU probe
# (gpu, a module); time (now_iso).
__all__ = [
    "Adapter",
    "AgentLane",
    "CapacityDeferred",
    "Check",
    "PluginError",
    "InboxError",
    "Layout",
    "ProcessAdapter",
    "Registry",
    "Result",
    "agent_lane",
    "atomic_json",
    "atomic_write_text",
    "close_owned_item",
    "config",
    "contained",
    "gpu",
    "job_spec",
    "log_event",
    "now_iso",
    "owned_item",
    "owned_items",
    "policy",
    "read_json",
    "resources",
    "submit_command",
    "ui",
    "update_owned_item",
]


def __getattr__(name: str):
    if name == "ui":                         # the UI kit loads FastAPI: only when a plugin page asks for it
        import supervisor.plugin.ui as ui  # noqa: PLR0402 - `from . import ui` would ask this __getattr__ for `ui` first
        return ui
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
