"""Review 1.12.0 round 2, R2-2 (P2): an installed plugin's refusal belongs to the project that refused it. Approval
depends on a project's configuration: `client` requires `dep`, so a project listing only `client` refuses it, and a
project listing both accepts it. The refusal guard used to ban the installed module's name for the whole process, so
after project A refused `client`, project B, whose own approval accepts it, could not load it.

Imports made for a project (its load, G5, migrate) now follow that project's approval only. Outside any project, a
refused installed module stays closed unless a project in the process accepts it; a project-local module is named
under its project's namespace, so its refusal never reaches another project."""
from __future__ import annotations

import importlib
import sys

import pytest

from supervisor import plugins, testing

PKG = "supervisor_plugin_r2_scoped"


def manifest(name: str, extra: str = "") -> str:
    return f'identity = "{name}"\nversion = "1.0.0"\nframework = ">=1.12"\n' + extra


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    testing.reset()
    marker = tmp_path / "extra-ran"
    site = tmp_path / "site"
    pkg = site / PKG
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("SETUPS = []\n\ndef setup(r):\n    SETUPS.append(r.layout.root)\n",
                                     encoding="utf-8")
    (pkg / "extra.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
                                  encoding="utf-8")
    (pkg / "supervisor-plugin.toml").write_text(manifest("client", '[requires]\nplugins = ["dep"]\n'),
                                                   encoding="utf-8")
    info = site / f"{PKG}-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: supervisor-plugin-r2-scoped\nVersion: 1.0.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[supervisor.plugins]\nclient = {PKG}\n", encoding="utf-8")
    (info / "RECORD").write_text(f"{PKG}/__init__.py,,\n{PKG}/extra.py,,\n{PKG}/supervisor-plugin.toml,,\n",
                                 encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    for name in (PKG, f"{PKG}.extra"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield marker
    testing.reset()
    for name in (PKG, f"{PKG}.extra"):
        sys.modules.pop(name, None)


def refusing(tmp_path, *, user: bool = False):
    """Project A lists `client` without `dep`: its approval refuses `client`. With `user`, an accepted plugin of A
    imports a module of `client`'s package. testing.project() resets the loaded state, so a test makes its projects
    before it loads any."""
    modules = {"user": f"import {PKG}.extra  # noqa: F401\ndef setup(r):\n    pass\n"} if user else {}
    return testing.project(tmp_path / "a", plugins=["client"] + (["user"] if user else []), modules=modules)


def accepting(tmp_path):
    """Project B lists `dep` and `client`: its approval accepts both."""
    return testing.project(tmp_path / "b", plugins=["dep", "client"], modules={"dep": "def setup(r):\n    pass\n"},
                           files={"plugins/dep.toml": manifest("dep")})


@pytest.mark.parametrize("order", ["refusing-first", "accepting-first"])
def test_each_project_loads_as_it_would_alone(tmp_path, order):
    """Astra's probe, in both orders."""
    a, b = refusing(tmp_path), accepting(tmp_path)
    assert "client" in plugins.approval(a).refused and not plugins.approval(b).refused
    results = {}
    for name, layout in ([("a", a), ("b", b)] if order == "refusing-first" else [("b", b), ("a", a)]):
        reg = plugins.load(layout)
        results[name] = (reg.loaded, reg.errors)
    assert results["b"] == (["dep", "client"], [])
    assert results["a"][0] == [] and results["a"][1][0].startswith("client: it requires dep")
    assert importlib.import_module(PKG).SETUPS == [b.root]  # set up for B only


@pytest.mark.parametrize("order", ["refusing-first", "accepting-first"])
def test_the_refusing_project_still_cannot_import_the_package(tmp_path, _clean, order):
    """A module of the package not yet imported stays closed to project A, even with the package itself already
    imported for B."""
    a, b = refusing(tmp_path, user=True), accepting(tmp_path)
    if order == "accepting-first":
        assert plugins.load(b).loaded == ["dep", "client"]
    reg = plugins.load(a)
    assert "user" not in reg.loaded and any(e.startswith("user:") and "refused" in e for e in reg.errors)
    assert not _clean.exists()
    with plugins.importing_for(a), pytest.raises(ImportError, match="refused"):
        importlib.import_module(f"{PKG}.extra")
    assert not _clean.exists()


def test_outside_any_project_a_refused_package_stays_closed(tmp_path, _clean):
    a, _b = refusing(tmp_path), accepting(tmp_path)        # both projects exist, only A has loaded
    plugins.load(a)
    with pytest.raises(ImportError, match="refused"):
        importlib.import_module(f"{PKG}.extra")
    assert not _clean.exists()


def test_outside_any_project_a_package_another_project_accepts_imports(tmp_path, _clean):
    """B's own lazy imports, at run time, happen outside a load: B accepts the package, so they work."""
    a, b = refusing(tmp_path), accepting(tmp_path)
    plugins.load(a)
    assert plugins.load(b).loaded == ["dep", "client"]
    importlib.import_module(f"{PKG}.extra")
    assert _clean.exists()
