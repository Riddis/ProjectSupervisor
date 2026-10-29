"""Review 1.12.0 round 2, R2-1 (P2): G5 runs no code of a refused plugin. It used to set the refusal guard up only
in its own process and to start the fresh interpreter for `import_side_effects` before looking at the approval, so:
- checking an accepted plugin that imports a refused sibling ran the sibling in that interpreter;
- checking a refused plugin ran it there before the framework-range finding;
- a refused plugin already imported for another project (the finder is not asked about a cached module) was set
  up, and passed every check.

G5 now refuses a refused target before any import or setup, and the fresh interpreter takes the project's approval
and guard before its first import."""
from __future__ import annotations

import pytest

from supervisor import plugin_check, plugins, testing


def manifest(name: str, extra: str = "", framework: str = ">=1.12") -> str:
    return f'identity = "{name}"\nversion = "1.0.0"\nframework = "{framework}"\n' + extra


def trap(marker) -> str:
    return f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\ndef setup(r):\n    pass\n"


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def siblings(tmp_path, marker):
    """`good` imports `bad` without declaring it; `bad` is refused (its range excludes this framework)."""
    good = "from . import bad  # noqa: F401\ndef setup(r):\n    pass\n"
    return testing.project(tmp_path / "p", modules={"good": good, "bad": trap(marker)},
                           files={"plugins/good.toml": manifest("good"),
                                  "plugins/bad.toml": manifest("bad", framework=">=99")},
                           plugins=["good", "bad"])


def test_g5_of_an_accepted_plugin_does_not_run_a_refused_sibling(tmp_path):
    """Astra's probe A, the indirect import: neither process runs `bad`."""
    marker = tmp_path / "refused-ran"
    found = {f.name: f for f in plugin_check.run(siblings(tmp_path, marker), "good")}
    assert not marker.exists()
    assert not found["import_side_effects"].ok and "refused" in found["import_side_effects"].detail
    assert not found["registrations"].ok


def test_g5_of_a_refused_plugin_runs_none_of_its_code(tmp_path):
    """Astra's probe A, the direct check: the range failure is reported without importing it anywhere."""
    marker = tmp_path / "refused-ran"
    found = {f.name: f for f in plugin_check.run(siblings(tmp_path, marker), "bad")}
    assert not marker.exists()
    assert not found["framework"].ok
    for check in ("import_side_effects", "registrations", "idempotent", "config"):
        assert not found[check].ok and found[check].detail.startswith("not run: refused before import"), check


def test_the_fresh_interpreter_refuses_what_the_project_refuses(tmp_path):
    marker = tmp_path / "refused-ran"
    with pytest.raises(plugins.PluginError, match="refused"):
        plugins.import_side_effects(siblings(tmp_path, marker), "good")
    assert not marker.exists()


def installed_client(tmp_path, monkeypatch):
    """An installed plugin `client` (module supervisor_plugin_r2_client) requiring `dep`. Its setup() counts calls."""
    site = tmp_path / "site"
    pkg = site / "supervisor_plugin_r2_client"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("SETUPS = []\n\ndef setup(r):\n    SETUPS.append(r.layout.root)\n",
                                     encoding="utf-8")
    (pkg / "supervisor-plugin.toml").write_text(manifest("client", '[requires]\nplugins = ["dep"]\n'),
                                                   encoding="utf-8")
    info = site / "supervisor_plugin_r2_client-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: supervisor-plugin-r2-client\nVersion: 1.0.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text("[supervisor.plugins]\nclient = supervisor_plugin_r2_client\n",
                                           encoding="utf-8")
    (info / "RECORD").write_text("supervisor_plugin_r2_client/__init__.py,,\n"
                                 "supervisor_plugin_r2_client/supervisor-plugin.toml,,\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setenv("PYTHONPATH", str(site))
    monkeypatch.delitem(__import__("sys").modules, "supervisor_plugin_r2_client", raising=False)
    return site


def test_g5_refuses_a_module_already_imported_for_another_project(tmp_path, monkeypatch):
    """Astra's probe B: `client` loaded in project B, which lists `dep`, is refused in project A, which does not.
    G5 in A must not set the cached module up."""
    installed_client(tmp_path, monkeypatch)
    b = testing.project(tmp_path / "b", plugins=["dep", "client"], modules={"dep": "def setup(r):\n    pass\n"},
                        files={"plugins/dep.toml": manifest("dep")})
    a = testing.project(tmp_path / "a", plugins=["client"])
    assert plugins.load(b).loaded == ["dep", "client"]
    module = __import__("supervisor_plugin_r2_client")
    assert len(module.SETUPS) == 1
    assert "client" in plugins.approval(a).refused
    found = {f.name: f for f in plugin_check.run(a, "client")}
    assert len(module.SETUPS) == 1                          # not set up again for A
    assert not all(f.ok for f in found.values())
    assert found["registrations"].detail.startswith("not run: refused before import: it requires dep")
