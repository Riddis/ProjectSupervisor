"""The public extension API (docs/EXTENSION_API.md): the only part of `supervisor` an extension imports, besides the
explicitly unstable `supervisor.testing`. `__all__` is the contract: gate G2 (`python -m supervisor.api_contract
check`) holds it to docs/extension_api.json and to the last release's contract.

Modules in this package may import framework internals; nothing outside it imports from it, so dependencies point one
way. A name is exported only when an extension needs it (docs/BOUNDARY.md); everything else stays internal."""
from __future__ import annotations

__all__: list[str] = []
