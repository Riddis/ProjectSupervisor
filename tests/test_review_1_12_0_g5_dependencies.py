"""Review 1.12.0 R4 (P2): G5 runs a plugin's setup() with its declared dependencies' context. When `client`
requires `dep` (decision D6) and its setup() calls a public function of `dep` that reads dep's own configuration
or resources, normal loading succeeds, since dep loads first. G5 used to replace the project's registry with one
knowing only `client`, so dep's read failed as undeclared. G5's staging registry now also holds each declared
dependency's manifest, origin and schema, from an unloaded project as from a loaded one, and still commits nothing.

Also here: an installed plugin, whose module name is not its identity, reading its configuration inside
setup(). That broke when 10b475e stopped publishing the registry before a load finished: calling_plugin()
looked only at the published registries."""
from __future__ import annotations

import pytest

from supervisor import plugin_check, plugins, testing
from supervisor.adapters.python import JOBS

DEP = '''from pydantic import BaseModel
from supervisor.plugin import config, resources

class Settings(BaseModel):
    value: int = 4

def read(layout):
    return config(layout, "dep")["value"], resources(layout)["gpu"].get("floor")

def setup(r):
    pass
'''
CLIENT = '''from . import dep

def setup(r):
    value = dep.read(r.layout)
    r.python_job("client_job", lambda root, run_dir, **p: {"status": "COMPLETE", "notes": str(value)})
'''


def manifest(name: str, extra: str = "") -> str:
    return f'identity = "{name}"\nversion = "1.0.0"\nframework = ">=1.12"\n' + extra


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def project(tmp_path):
    return testing.project(tmp_path, modules={"dep": DEP, "client": CLIENT}, plugins=["dep", "client"],
                           routing={"plugins": {"dep": {"value": 7}}, "resources": {"gpu": {"floor": 6500}}},
                           files={"plugins/dep.toml": manifest(
                                      "dep", '[config]\nsection = "plugins.dep"\nschema = "dep:Settings"\n'
                                             '[requires]\nresources = ["gpu"]\n'),
                                  "plugins/client.toml": manifest(
                                      "client", '[provides]\npython_jobs = ["client_job"]\n'
                                                '[requires]\nplugins = ["dep"]\n')})


@pytest.mark.parametrize("loaded_first", [False, True], ids=["unloaded-project", "loaded-project"])
def test_g5_runs_setup_with_its_dependencies_configuration(tmp_path, loaded_first):
    """Astra's probe, with the dependency's resources too."""
    layout = project(tmp_path)
    if loaded_first:
        assert plugins.load(layout).loaded == ["dep", "client"]
        before = dict(JOBS)
    found = {f.name: f for f in plugin_check.run(layout, "client")}
    assert all(f.ok for f in found.values()), {n: f.detail for n, f in found.items() if not f.ok}
    if loaded_first:
        assert JOBS == before                              # nothing committed by the check
    else:
        assert "client_job" not in JOBS


def test_normal_loading_still_gives_the_dependency_its_configuration(tmp_path):
    reg = plugins.load(project(tmp_path))
    assert reg.loaded == ["dep", "client"] and not reg.errors
    assert JOBS["client_job"](".", ".")["notes"] == "(7, 6500)"


def test_an_installed_plugin_reads_its_configuration_inside_setup(tmp_path, monkeypatch):
    """Its module (supervisor_plugin_r4_notes) is not its identity (notes): identity comes from the in-progress registry.
    The module name is this test's own: test_plugin_scaffold leaves a generated supervisor_plugin_notes imported."""
    site = tmp_path / "site"
    pkg = site / "supervisor_plugin_r4_notes"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "from pydantic import BaseModel\nfrom supervisor import plugin\n\n"
        "class Settings(BaseModel):\n    greeting: str = 'hi'\n\n"
        "def setup(r):\n    r.python_job('notes_hello', lambda root, run_dir, **p: "
        "{'status': 'COMPLETE', 'notes': plugin.config(r.layout, 'notes')['greeting']})\n"
        "    SEEN.append(plugin.config(r.layout, 'notes')['greeting'])\n\nSEEN = []\n", encoding="utf-8")
    (pkg / "supervisor-plugin.toml").write_text(manifest(
        "notes", '[provides]\npython_jobs = ["notes_hello"]\n[config]\nsection = "plugins.notes"\n'
                 'schema = "supervisor_plugin_r4_notes:Settings"\n'), encoding="utf-8")
    info = site / "supervisor_plugin_r4_notes-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: supervisor-plugin-r4-notes\nVersion: 1.0.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text("[supervisor.plugins]\nnotes = supervisor_plugin_r4_notes\n",
                                           encoding="utf-8")
    (info / "RECORD").write_text("supervisor_plugin_r4_notes/__init__.py,,\n"
                                 "supervisor_plugin_r4_notes/supervisor-plugin.toml,,\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    layout = testing.project(tmp_path / "p", plugins=["notes"],
                             routing={"plugins": {"notes": {"greeting": "hey"}}})
    reg = plugins.load(layout)
    assert reg.loaded == ["notes"], reg.errors
    assert __import__("supervisor_plugin_r4_notes").SEEN == ["hey"]
