"""Review 1.12.0 R4 (P2): G5 runs an extension's setup() with its declared dependencies' context. When `client`
requires `dep` (decision D6) and its setup() calls a public function of `dep` that reads dep's own configuration
or resources, normal loading succeeds, since dep loads first. G5 used to replace the project's registry with one
knowing only `client`, so dep's read failed as undeclared. G5's staging registry now also holds each declared
dependency's manifest, origin and schema, from an unloaded project as from a loaded one, and still commits nothing.

Also here: an installed extension, whose module name is not its identity, reading its configuration inside
setup(). That broke when 10b475e stopped publishing the registry before a load finished: calling_extension()
looked only at the published registries."""
from __future__ import annotations

import pytest

from supervisor import extension_check, extensions, testing
from supervisor.adapters.python import JOBS

DEP = '''from pydantic import BaseModel
from supervisor.ext import config, resources

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
    return testing.project(tmp_path, modules={"dep": DEP, "client": CLIENT}, extensions=["dep", "client"],
                           routing={"extensions": {"dep": {"value": 7}}, "resources": {"gpu": {"floor": 6500}}},
                           files={"extensions/dep.toml": manifest(
                                      "dep", '[config]\nsection = "extensions.dep"\nschema = "dep:Settings"\n'
                                             '[requires]\nresources = ["gpu"]\n'),
                                  "extensions/client.toml": manifest(
                                      "client", '[provides]\npython_jobs = ["client_job"]\n'
                                                '[requires]\nextensions = ["dep"]\n')})


@pytest.mark.parametrize("loaded_first", [False, True], ids=["unloaded-project", "loaded-project"])
def test_g5_runs_setup_with_its_dependencies_configuration(tmp_path, loaded_first):
    """Astra's probe, with the dependency's resources too."""
    layout = project(tmp_path)
    if loaded_first:
        assert extensions.load(layout).loaded == ["dep", "client"]
        before = dict(JOBS)
    found = {f.name: f for f in extension_check.run(layout, "client")}
    assert all(f.ok for f in found.values()), {n: f.detail for n, f in found.items() if not f.ok}
    if loaded_first:
        assert JOBS == before                              # nothing committed by the check
    else:
        assert "client_job" not in JOBS


def test_normal_loading_still_gives_the_dependency_its_configuration(tmp_path):
    reg = extensions.load(project(tmp_path))
    assert reg.loaded == ["dep", "client"] and not reg.errors
    assert JOBS["client_job"](".", ".")["notes"] == "(7, 6500)"


def test_an_installed_extension_reads_its_configuration_inside_setup(tmp_path, monkeypatch):
    """Its module (supervisor_ext_notes) is not its identity (notes): identity comes from the in-progress registry."""
    site = tmp_path / "site"
    pkg = site / "supervisor_ext_notes"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "from pydantic import BaseModel\nfrom supervisor import ext\n\n"
        "class Settings(BaseModel):\n    greeting: str = 'hi'\n\n"
        "def setup(r):\n    r.python_job('notes_hello', lambda root, run_dir, **p: "
        "{'status': 'COMPLETE', 'notes': ext.config(r.layout, 'notes')['greeting']})\n"
        "    SEEN.append(ext.config(r.layout, 'notes')['greeting'])\n\nSEEN = []\n", encoding="utf-8")
    (pkg / "supervisor-extension.toml").write_text(manifest(
        "notes", '[provides]\npython_jobs = ["notes_hello"]\n[config]\nsection = "extensions.notes"\n'
                 'schema = "supervisor_ext_notes:Settings"\n'), encoding="utf-8")
    info = site / "supervisor_ext_notes-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: supervisor-ext-notes\nVersion: 1.0.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text("[supervisor.extensions]\nnotes = supervisor_ext_notes\n", encoding="utf-8")
    (info / "RECORD").write_text("supervisor_ext_notes/__init__.py,,\n"
                                 "supervisor_ext_notes/supervisor-extension.toml,,\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    layout = testing.project(tmp_path / "p", extensions=["notes"],
                             routing={"extensions": {"notes": {"greeting": "hey"}}})
    reg = extensions.load(layout)
    assert reg.loaded == ["notes"], reg.errors
    assert __import__("supervisor_ext_notes").SEEN == ["hey"]
