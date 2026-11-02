"""The framework's own plugin-`MIGRATIONS` mechanism (`supervisor/migrations.py`, driven by a manifest's
`migrations` key naming a module with a `MIGRATIONS` list) carries no ledger of completed entries: a package
that registers one is expected to prove, on a disposable project, that an interrupted run can simply be
re-run to completion - or to discharge the obligation outright by showing its manifest names no such module,
which `docs/PLUGIN_STANDARD.md`'s Migrations section treats as a complete answer, not a gap. This package
owns no `MIGRATIONS`; the assertion below puts that proof in the package's own tests, the same evidence
`supervisor plugin check`'s `manifest` step reads, so `tools/ci_package.py` step 4 re-runs it on every
framework ref instead of a manifest change passing CI silently. Read the same way `test_version.py` already
does in this file's own package: straight from the source tree when `tests/` ships beside it (no import, no
install needed), falling back to the installed module's own path only once `ci_package.py` has copied
`tests/` next to a wheel with no sibling package folder left to read."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path

import tomllib

PACKAGE = "supervisor_plugin_local_llm"
HERE = Path(__file__).resolve().parent


def _manifest_path() -> Path:
    sibling = HERE.parent / PACKAGE / "supervisor-plugin.toml"
    if sibling.is_file():
        return sibling
    return Path(import_module(PACKAGE).__file__).resolve().parent / "supervisor-plugin.toml"


def test_manifest_carries_no_migrations_key():
    """Read without importing anything, the same evidence `supervisor plugin check`'s own `manifest` step
    uses - this package discharges the generic plugin-`MIGRATIONS` obligation by having nothing for it to
    run."""
    doc = tomllib.loads(_manifest_path().read_text(encoding="utf-8"))
    assert "migrations" not in doc
    assert "legacy_sections" not in doc.get("config", {})
