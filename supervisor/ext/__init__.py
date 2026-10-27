"""The public extension API (docs/EXTENSION_API.md): the only part of `supervisor` an extension imports, besides the
explicitly unstable `supervisor.testing`. `__all__` is the contract: gate G2 (`python -m supervisor.api_contract
check`) holds it to docs/extension_api.json and to the last release's contract.

Modules in this package may import framework internals; nothing outside it imports from it, so dependencies point one
way. A name is exported only when an extension needs it (docs/BOUNDARY.md); everything else stays internal."""
from __future__ import annotations

from .. import gpu
from .._vendor.contract import CapacityDeferred, atomic_json, atomic_write_text, contained, read_json
from ..adapters.base import Adapter, Result
from ..adapters.process import ProcessAdapter
from ..doctor import Check
from ..extensions import ExtensionError
from ..paths import Layout
from ..store import now_iso
from ._config import config, resources
from ._registry import Registry

# Grouped by purpose in docs/EXTENSION_API.md: registration (Registry, ExtensionError); configuration (config,
# resources); project paths (Layout); files (atomic_json, atomic_write_text, contained, read_json); job lanes
# (Adapter, CapacityDeferred, ProcessAdapter, Result); doctor checks (Check); the shared GPU probe (gpu, a module);
# time (now_iso).
__all__ = [
    "Adapter",
    "CapacityDeferred",
    "Check",
    "ExtensionError",
    "Layout",
    "ProcessAdapter",
    "Registry",
    "Result",
    "atomic_json",
    "atomic_write_text",
    "config",
    "contained",
    "gpu",
    "now_iso",
    "read_json",
    "resources",
]
