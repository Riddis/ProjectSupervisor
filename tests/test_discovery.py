"""Discovery without import (phase 3 step 2): for each name in project.yaml `extensions:`, find the extension and read
its manifest, and execute nothing the extension ships. A project-local extension's manifest sits beside its module
(`extensions/<name>.toml`) or inside its package folder; an installed one is found through the entry point group
`supervisor.extensions` and its manifest read from the distribution's file list, or, for an editable install whose
file list does not show it, beside the origin `importlib.util.find_spec` reports for its top-level package. Decision
D5: a project-local manifest wins; the same identity from two sources is refused, not chosen silently. Decision D1:
an extension without a manifest is still found (and loads with a warning, later steps)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from supervisor import discovery

MANIFEST = 'identity = "{identity}"\nversion = "1.0.0"\nframework = ">=1.11"\n'
TRAP = "import pathlib\npathlib.Path({marker!r}).write_text('imported')\ndef setup(registry):\n    pass\n"


def local_module(layout, name: str, marker: Path, manifest: str | None = MANIFEST) -> Path:
    layout.extensions.mkdir(parents=True, exist_ok=True)
    module = layout.extensions / f"{name}.py"
    module.write_text(TRAP.format(marker=str(marker)), encoding="utf-8")
    if manifest is not None:
        (layout.extensions / f"{name}.toml").write_text(manifest.format(identity=name), encoding="utf-8")
    return module


def installed(site: Path, identity: str, package: str, marker: Path, *, record_manifest: bool = True,
              manifest_identity: str | None = None) -> Path:
    """A distribution as pip leaves it: the package with its manifest as package data, and a .dist-info whose
    entry_points.txt names the package and whose RECORD lists the files (an editable install lists only its finder)."""
    pkg = site / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(TRAP.format(marker=str(marker)), encoding="utf-8")
    (pkg / "supervisor-extension.toml").write_text(MANIFEST.format(identity=manifest_identity or identity),
                                                   encoding="utf-8")
    info = site / f"{package}-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {package}\nVersion: 1.0.0\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[supervisor.extensions]\n{identity} = {package}\n", encoding="utf-8")
    listed = [f"{package}/__init__.py"] + ([f"{package}/supervisor-extension.toml"] if record_manifest
                                           else [f"__editable__.{package}_finder.py"])
    (info / "RECORD").write_text("".join(f"{f},,\n" for f in listed), encoding="utf-8")
    return pkg


@pytest.fixture
def site(tmp_path, monkeypatch):
    path = tmp_path / "site"
    path.mkdir()
    monkeypatch.syspath_prepend(str(path))
    return path


def test_a_local_module_and_its_manifest_are_found_without_running_it(layout, tmp_path):
    marker = tmp_path / "imported.txt"
    module = local_module(layout, "notes", marker)
    found = discovery.find(layout, "notes")
    assert found.source == "local" and found.location == module and not found.problems
    assert found.manifest.identity == "notes" and found.manifest.source == str(layout.extensions / "notes.toml")
    assert not marker.exists()


def test_a_local_package_keeps_its_manifest_inside(layout, tmp_path):
    marker = tmp_path / "imported.txt"
    pkg = layout.extensions / "notes"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(TRAP.format(marker=str(marker)), encoding="utf-8")
    (pkg / "supervisor-extension.toml").write_text(MANIFEST.format(identity="notes"), encoding="utf-8")
    found = discovery.find(layout, "notes")
    assert found.source == "local" and found.location == pkg and found.manifest.identity == "notes"
    assert not marker.exists()


def test_an_installed_extension_is_found_through_its_entry_point_without_import(layout, tmp_path, site):
    marker = tmp_path / "imported.txt"
    pkg = installed(site, "notes", "notes_ext", marker)
    found = discovery.find(layout, "notes")
    assert found.source == "installed" and found.location == pkg and not found.problems
    assert found.manifest.identity == "notes" and Path(found.manifest.source) == pkg / "supervisor-extension.toml"
    assert not marker.exists() and "notes_ext" not in sys.modules


def test_an_editable_install_is_found_beside_its_origin_without_import(layout, tmp_path, site):
    """The file list shows only the editable finder, not the manifest: find_spec locates the package, and finding a
    top-level package's spec does not execute it."""
    marker = tmp_path / "imported.txt"
    pkg = installed(site, "notes", "notes_ext", marker, record_manifest=False)
    found = discovery.find(layout, "notes")
    assert found.source == "installed" and found.location == pkg and found.manifest.identity == "notes"
    assert not marker.exists() and "notes_ext" not in sys.modules


def test_the_same_identity_from_two_sources_is_refused(layout, tmp_path, site):
    local_module(layout, "notes", tmp_path / "a.txt")
    installed(site, "notes", "notes_ext", tmp_path / "b.txt")
    found = discovery.find(layout, "notes")
    assert found.manifest is None and any("two" in p and "notes_ext" in p for p in found.problems)


def test_a_manifest_naming_another_identity_is_refused(layout, tmp_path, site):
    installed(site, "notes", "notes_ext", tmp_path / "b.txt", manifest_identity="journal")
    found = discovery.find(layout, "notes")
    assert any("journal" in p and "notes" in p for p in found.problems)
    local_module(layout, "diary", tmp_path / "c.txt", manifest=MANIFEST.replace("{identity}", "ledger"))
    assert any("ledger" in p for p in discovery.find(layout, "diary").problems)


def test_a_broken_manifest_is_reported_not_raised(layout, tmp_path):
    local_module(layout, "notes", tmp_path / "a.txt", manifest='identity = "notes"\n')
    found = discovery.find(layout, "notes")
    assert found.manifest is None and any("version is required" in p for p in found.problems)


def test_an_extension_without_a_manifest_is_still_found_as_legacy(layout, tmp_path):
    """D1: 1.12.x keeps loading extensions that have no manifest yet (with a doctor WARN, later steps)."""
    module = local_module(layout, "notes", tmp_path / "a.txt", manifest=None)
    found = discovery.find(layout, "notes")
    assert found.source == "local" and found.location == module and found.manifest is None and not found.problems
    assert found.legacy


def test_an_installed_module_without_an_entry_point_is_legacy_and_not_imported(layout, tmp_path, site):
    marker = tmp_path / "imported.txt"
    (site / "plain_ext.py").write_text(TRAP.format(marker=str(marker)), encoding="utf-8")
    found = discovery.find(layout, "plain_ext")
    assert found.source == "module" and found.legacy and found.manifest is None and not marker.exists()


def test_a_name_found_nowhere_says_where_it_looked(layout):
    found = discovery.find(layout, "nowhere")
    assert found.source is None and any("extensions" in p and "supervisor.extensions" in p for p in found.problems)
