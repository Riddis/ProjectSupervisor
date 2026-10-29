"""Review 1.12.0 round 3, R3-1 (P2): a refusal in one project does not hide an editable install's manifest from
another. For an editable install the distribution's file list does not show the manifest, so discovery asks the
import system for the package's spec (no import). That lookup reached the refusal guard: after project A refused
the package, project B could not even read its manifest to decide that it accepts it, and was refused with "no
manifest".

Discovery's spec lookup now passes the guard by; importing stays guarded. Discovery still executes nothing the
package ships, and project A's protection holds after B accepts the package."""
from __future__ import annotations

import importlib
import sys

import pytest

from supervisor import discovery, plugins, testing

PKG = "supervisor_plugin_r3_editable"


def manifest(name: str, extra: str = "") -> str:
    return f'identity = "{name}"\nversion = "1.0.0"\nframework = ">=1.12"\n' + extra


@pytest.fixture(autouse=True)
def markers(tmp_path, monkeypatch):
    """An editable-style install: the package on sys.path, its distribution's file list without the package files."""
    testing.reset()
    imported, extra_ran = tmp_path / "package-imported", tmp_path / "extra-ran"
    site = tmp_path / "site"
    pkg = site / PKG
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"from pathlib import Path\nPath({str(imported)!r}).write_text('ran')\n"
                                     "SETUPS = []\n\ndef setup(r):\n    SETUPS.append(r.layout.root)\n",
                                     encoding="utf-8")
    (pkg / "extra.py").write_text(f"from pathlib import Path\nPath({str(extra_ran)!r}).write_text('ran')\n",
                                  encoding="utf-8")
    (pkg / "supervisor-plugin.toml").write_text(manifest("client", '[requires]\nplugins = ["dep"]\n'),
                                                   encoding="utf-8")
    info = site / f"{PKG}-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: supervisor-plugin-r3-editable\nVersion: 1.0.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[supervisor.plugins]\nclient = {PKG}\n", encoding="utf-8")
    (info / "RECORD").write_text(f"__editable__.{PKG}-1.0.0.pth,,\n", encoding="utf-8")   # no package files
    monkeypatch.syspath_prepend(str(site))
    for name in (PKG, f"{PKG}.extra"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield imported, extra_ran
    testing.reset()
    for name in (PKG, f"{PKG}.extra"):
        sys.modules.pop(name, None)


def projects(tmp_path):
    """A lists `client` without `dep` (refused); B lists both (accepted). Both are made before either loads, since
    testing.project() resets the loaded state."""
    a = testing.project(tmp_path / "a", plugins=["client"])
    b = testing.project(tmp_path / "b", plugins=["dep", "client"], modules={"dep": "def setup(r):\n    pass\n"},
                        files={"plugins/dep.toml": manifest("dep")})
    return a, b


def test_a_refusal_elsewhere_does_not_hide_the_manifest(tmp_path, markers):
    """Astra's probe: B's discovery after A's refusal finds the manifest, without importing the package."""
    imported, _extra = markers
    a, b = projects(tmp_path)
    assert plugins.load(a).errors[0].startswith("client: it requires dep")
    found = discovery.find(b, "client")
    assert found.manifest is not None and not found.problems, found.problems
    assert not imported.exists()                           # discovery ran nothing the package ships
    assert plugins.load(b).loaded == ["dep", "client"]


@pytest.mark.parametrize("order", ["refusing-first", "accepting-first"])
def test_each_project_loads_as_it_would_alone(tmp_path, order):
    a, b = projects(tmp_path)
    results = {}
    for name, layout in ([("a", a), ("b", b)] if order == "refusing-first" else [("b", b), ("a", a)]):
        reg = plugins.load(layout)
        results[name] = (reg.loaded, reg.errors)
    assert results["b"] == (["dep", "client"], [])
    assert results["a"][0] == [] and results["a"][1][0].startswith("client: it requires dep")
    assert importlib.import_module(PKG).SETUPS == [b.root]


def test_the_refusing_project_keeps_its_protection_after_another_accepts(tmp_path, markers):
    _imported, extra_ran = markers
    a, b = projects(tmp_path)
    plugins.load(a)
    assert plugins.load(b).loaded == ["dep", "client"]
    with plugins.importing_for(a), pytest.raises(ImportError, match="refused"):
        importlib.import_module(f"{PKG}.extra")
    assert not extra_ran.exists()


def test_a_spec_lookup_passes_the_guard_but_an_import_does_not(tmp_path, markers):
    imported, _extra = markers
    a, _b = projects(tmp_path)
    plugins.load(a)                                     # A refuses the package; no project accepts it
    assert discovery.find(a, "client").manifest is not None
    with pytest.raises(ImportError, match="refused"):
        importlib.import_module(PKG)
    assert not imported.exists()
