"""The framework's own plugin-MIGRATIONS mechanism carries no ledger of completed entries; a package that
registers none discharges the obligation by having nothing for it to run (docs/PLUGIN_STANDARD.md's Migrations
section). This package owns no MIGRATIONS and declares no legacy_sections."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path

import tomllib

PACKAGE = "supervisor_plugin_llm_endpoint"
HERE = Path(__file__).resolve().parent


def _manifest_path() -> Path:
    sibling = HERE.parent / PACKAGE / "supervisor-plugin.toml"
    if sibling.is_file():
        return sibling
    return Path(import_module(PACKAGE).__file__).resolve().parent / "supervisor-plugin.toml"


def test_manifest_carries_no_migrations_key():
    doc = tomllib.loads(_manifest_path().read_text(encoding="utf-8"))
    assert "migrations" not in doc
    assert "legacy_sections" not in doc.get("config", {})
