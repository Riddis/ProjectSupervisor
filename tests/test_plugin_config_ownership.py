"""Configuration ownership (phase 3 step 6): a plugin with a manifest owns `plugins.<identity>` in
config/routing.yaml, validated by the pydantic model its manifest names (`config.schema`) before setup() runs, so
invalid configuration is a load failure and leaves nothing registered. `plugin.config()` and `plugin.resources()` keep
their phase-2 signatures:
- `plugin.config(layout, <identity>)` is the whole validated section;
- `plugin.config(layout, <legacy section>)` is the sub-section of that name (a plugin `render` reads `encoder`
  this way).

Until the migration has run (step 7), the values still come from the old top-level sections the manifest lists in
`config.legacy_sections`, with a DeprecationWarning: the section named like the identity merges in at the top, any
other nests under its own name. `plugin.resources()` reads `resources.gpu`, else the old top-level `gpu` (warned); an
plugin with a manifest sees only the resources it declares in `requires.resources`. Plugins without a manifest
keep the phase-2 facade unchanged (tests/test_plugin_config.py)."""
from __future__ import annotations

import sys
import warnings

import pytest

from supervisor import plugins, testing

EXT = '''
from pydantic import BaseModel
from supervisor import plugin

class Media(BaseModel):
    binary: str = "encoder"
    threads: int = 2

class Settings(BaseModel):
    base_url: str
    timeout_s: int = 30
    media: Media = Media()

READ = {}

def setup(registry):
    READ["whole"] = plugin.config(registry.layout, "notes")
    READ["media"] = plugin.config(registry.layout, "media")
    READ["resources"] = plugin.resources(registry.layout)
    registry.python_job("notes_sync", lambda root, run_dir, **p: {"status": "COMPLETE"})
'''

MANIFEST = '''identity = "notes"
version = "1.0.0"
framework = ">=1.11"
[provides]
python_jobs = ["notes_sync"]
[config]
section = "plugins.notes"
schema = "notes:Settings"
legacy_sections = ["notes", "media"]
{requires}'''


def load(tmp_path, routing: dict, *, requires: str = '[requires]\nresources = ["gpu"]\n', source: str = EXT,
         required: bool = True):
    layout = testing.project(tmp_path, modules={"notes": source}, routing=routing,
                             files={"plugins/notes.toml": MANIFEST.format(requires=requires)},
                             plugins=[{"name": "notes", "required": required}])
    reg = plugins.load(layout)
    module = next((m for n, m in sys.modules.items() if n.endswith(".notes")), None)
    return reg, (module.READ if module is not None else None)


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def test_the_plugins_own_section_is_read_validated_and_completed_by_its_schema(tmp_path):
    reg, read = load(tmp_path, {"plugins": {"notes": {"base_url": "http://127.0.0.1:9", "media": {"threads": 4}}},
                                "resources": {"gpu": {"usable_vram_floor_mb": 6500}}})
    assert reg.loaded == ["notes"] and not reg.errors
    assert read["whole"] == {"base_url": "http://127.0.0.1:9", "timeout_s": 30,
                             "media": {"binary": "encoder", "threads": 4}}
    assert read["media"] == {"binary": "encoder", "threads": 4}
    assert read["resources"] == {"gpu": {"usable_vram_floor_mb": 6500}}


def test_before_the_migration_the_legacy_sections_are_read_with_a_warning(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reg, read = load(tmp_path, {"notes": {"base_url": "http://127.0.0.1:9"}, "media": {"binary": "avconv"},
                                    "gpu": {"usable_vram_floor_mb": 6000}})
    assert reg.loaded == ["notes"]
    assert read["whole"]["base_url"] == "http://127.0.0.1:9" and read["media"]["binary"] == "avconv"
    assert read["resources"] == {"gpu": {"usable_vram_floor_mb": 6000}}
    texts = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any("plugins.notes" in t and "supervisor migrate" in t for t in texts)
    assert any("resources.gpu" in t for t in texts)


@pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
def test_invalid_configuration_is_a_load_failure_and_leaves_nothing_registered(tmp_path, required):
    from supervisor.adapters.python import JOBS
    reg, _read = load(tmp_path, {"plugins": {"notes": {"timeout_s": "soon"}}}, required=required)
    assert not reg.loaded and "notes_sync" not in JOBS
    error = next(e for e in reg.errors if e.startswith("notes:"))
    assert "plugins.notes" in error and "base_url" in error and "timeout_s" in error
    assert bool(reg.blocking) is required


def test_a_section_that_is_not_its_own_is_refused(tmp_path):
    source = EXT.replace('plugin.config(registry.layout, "media")', 'plugin.config(registry.layout, "capacity")')
    reg, _read = load(tmp_path, {"plugins": {"notes": {"base_url": "x"}}}, source=source)
    assert any("capacity" in e and "plugins.notes" in e for e in reg.errors) and not reg.loaded


def test_a_manifest_sees_only_the_resources_it_declares(tmp_path):
    reg, read = load(tmp_path, {"plugins": {"notes": {"base_url": "x"}}, "resources": {"gpu": {"a": 1}}},
                     requires="")
    assert reg.loaded == ["notes"] and read["resources"] == {}


def test_a_legacy_section_owned_by_the_framework_is_refused_before_import(tmp_path):
    marker = tmp_path / "imported.txt"
    trap = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n" + EXT
    layout = testing.project(tmp_path, modules={"notes": trap}, routing={"limits": {}},
                             files={"plugins/notes.toml": MANIFEST.format(requires="").replace(
                                 'legacy_sections = ["notes", "media"]', 'legacy_sections = ["notes", "limits"]')})
    reg = plugins.load(layout)
    assert not marker.exists() and any("limits" in e and "framework" in e for e in reg.errors)


def test_a_legacy_section_two_manifests_claim_refuses_both(tmp_path):
    other = MANIFEST.format(requires="").replace('"notes"', '"diary"').replace("notes:Settings", "diary:Settings")
    other = other.replace('python_jobs = ["notes_sync"]', 'python_jobs = ["diary_sync"]')
    other = other.replace('"plugins.notes"', '"plugins.diary"')      # both still claim `media`
    layout = testing.project(tmp_path, modules={"notes": EXT, "diary": EXT.replace("notes", "diary")},
                             routing={"plugins": {}},
                             files={"plugins/notes.toml": MANIFEST.format(requires=""),
                                    "plugins/diary.toml": other})
    reg = plugins.load(layout)
    assert not reg.loaded
    assert any(e.startswith("notes:") and "media" in e and "diary" in e for e in reg.errors)


def test_a_schema_in_a_package_module_is_found(tmp_path):
    pkg_init = EXT.replace("from pydantic import BaseModel\n", "from .config import Settings  # noqa: F401\n")
    pkg_init = pkg_init[:pkg_init.index("class Media")] + pkg_init[pkg_init.index("READ = {}"):]
    config_py = EXT[:EXT.index("READ = {}")].replace("from supervisor import plugin\n", "")
    layout = testing.project(tmp_path, routing={"plugins": {"notes": {"base_url": "x"}}},
                             files={"plugins/notes/__init__.py": pkg_init, "plugins/notes/config.py": config_py,
                                    "plugins/notes/supervisor-plugin.toml": MANIFEST.format(requires="").replace(
                                        "notes:Settings", "notes.config:Settings")},
                             plugins=["notes"])
    reg = plugins.load(layout)
    assert reg.loaded == ["notes"], reg.errors
