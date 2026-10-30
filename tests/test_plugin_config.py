"""The transitional config facade (phase 2 step 4): a plugin declares the routing.yaml sections it reads with
`registry.config_sections(...)` in setup(), reads them with `plugin.config(layout, section)` and the shared resource
policy with `plugin.resources(layout)`. Both return copies: changing one changes nothing. A section the calling
plugin did not declare is refused, as is a framework-owned section or one another plugin declared. Phase 3
keeps these signatures and changes only where the values come from (plugins.<identity>, resources.gpu)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from supervisor import plugin, plugins, testing
from supervisor.plugin import _config
from supervisor.paths import Layout

ROUTING = """\
version: 1
alpha:                     # lane_a's section
  a: 1
  nested: {x: [1, 2]}
gamma: {g: 1}
scalar: 7
gpu: {usable_vram_floor_mb: 2000}
limits: {max_resource_jobs: 1}
"""

READER = '''
from supervisor import plugin

def read(layout, section):
    return plugin.config(layout, section)

def resources(layout):
    return plugin.resources(layout)
'''


def configure(root: Path, modules: dict[str, str], entries: list[str], routing: str = ROUTING) -> Layout:
    return testing.project(root, modules=modules, plugins=entries, files={"config/routing.yaml": routing})


MODULES = {
    "lane_a": READER + "\ndef setup(registry):\n    registry.config_sections('alpha', 'beta', 'scalar')\n",
    "lane_b": READER + "\ndef setup(registry):\n    registry.config_sections('gamma')\n",
    "helper": "from supervisor import plugin\n\ndef read(layout, section):\n    return plugin.config(layout, section)\n",
    "uses_helper": "from . import helper\n\ndef read(layout, section):\n    return helper.read(layout, section)\n\n"
                   "def setup(registry):\n    registry.config_sections('delta')\n",
}


@pytest.fixture
def lay(tmp_path):
    lay = configure(tmp_path, MODULES, ["lane_a", "lane_b", "uses_helper"])
    reg = plugins.load(lay)
    assert reg.errors == [], reg.errors
    yield lay
    plugins.reset()


def mod(lay, name):
    return plugins._import(lay, name)


def test_a_plugin_reads_its_declared_section_as_a_copy(lay):
    before = (lay.config / "routing.yaml").read_bytes()
    alpha = mod(lay, "lane_a").read(lay, "alpha")
    assert alpha == {"a": 1, "nested": {"x": [1, 2]}}
    alpha["a"] = 99
    alpha["nested"]["x"].append(3)
    assert mod(lay, "lane_a").read(lay, "alpha") == {"a": 1, "nested": {"x": [1, 2]}}
    assert (lay.config / "routing.yaml").read_bytes() == before


def test_a_declared_section_the_config_does_not_have_reads_as_empty(lay):
    assert mod(lay, "lane_a").read(lay, "beta") == {}


def test_the_config_is_read_when_asked_not_when_set_up(lay):
    (lay.config / "routing.yaml").write_text(ROUTING.replace("a: 1", "a: 2"), encoding="utf-8")
    assert mod(lay, "lane_a").read(lay, "alpha")["a"] == 2


@pytest.mark.parametrize("section,why", [
    ("gamma", "lane_b"),              # another plugin's
    ("delta", "uses_helper"),         # another plugin's, read through nobody's helper
    ("omega", "did not declare"),     # nobody's
    ("limits", "did not declare"),    # the framework's
])
def test_a_section_the_plugin_did_not_declare_is_refused(lay, section, why):
    with pytest.raises(plugins.PluginError, match=why):
        mod(lay, "lane_a").read(lay, section)


def test_a_section_that_is_not_a_mapping_is_an_error(lay):
    with pytest.raises(plugins.PluginError, match="scalar"):
        mod(lay, "lane_a").read(lay, "scalar")


def test_only_plugin_code_reads_plugin_config(lay):
    with pytest.raises(plugins.PluginError, match="call it from that plugin's code"):
        plugin.config(lay, "alpha")
    with pytest.raises(plugins.PluginError):
        mod(lay, "helper").read(lay, "delta")           # a helper module is not a plugin of its own


def test_a_helper_module_reads_for_the_plugin_that_calls_it(lay):
    assert mod(lay, "uses_helper").read(lay, "delta") == {}
    with pytest.raises(plugins.PluginError, match="lane_a"):
        mod(lay, "uses_helper").read(lay, "alpha")


def test_every_plugin_reads_the_shared_resource_policy_as_a_copy(lay):
    for name in ("lane_a", "lane_b"):
        res = mod(lay, name).resources(lay)
        assert res == {"gpu": {"usable_vram_floor_mb": 2000}}
        res["gpu"]["usable_vram_floor_mb"] = 0
    assert mod(lay, "lane_a").resources(lay)["gpu"]["usable_vram_floor_mb"] == 2000


def test_no_resource_policy_reads_as_empty(tmp_path):
    lay = configure(tmp_path, {"lane_a": MODULES["lane_a"]}, ["lane_a"], routing="version: 1\n")
    plugins.load(lay)
    assert mod(lay, "lane_a").resources(lay) == {"gpu": {}}
    plugins.reset()


@pytest.mark.parametrize("section", ["limits", "roles", "capacity", "gpu", "resources", "plugins"])
def test_a_plugin_cannot_declare_a_framework_section(tmp_path, section):
    code = f"def setup(registry):\n    registry.config_sections({section!r})\n"
    reg = plugins.load(configure(tmp_path, {"grabby": code}, ["grabby"]))
    assert reg.loaded == [] and section in reg.errors[0] and "framework" in reg.errors[0]
    plugins.reset()


def test_two_plugins_cannot_declare_one_section(tmp_path):
    lay = configure(tmp_path, {"lane_a": MODULES["lane_a"],
                               "copycat": "def setup(registry):\n    registry.config_sections('alpha')\n"},
                    ["lane_a", "copycat"])
    reg = plugins.load(lay)
    assert reg.loaded == ["lane_a"] and reg.config_owners["alpha"] == "lane_a"
    assert any(e.startswith("copycat:") and "lane_a" in e for e in reg.errors)
    plugins.reset()


@pytest.mark.parametrize("names", [("",), (3,), ("two words",)])
def test_a_section_name_must_be_a_routing_key(tmp_path, names):
    code = f"def setup(registry):\n    registry.config_sections(*{names!r})\n"
    reg = plugins.load(configure(tmp_path, {"odd": code}, ["odd"]))
    assert reg.loaded == [] and "section" in reg.errors[0]
    plugins.reset()


def test_the_framework_sections_are_the_template_config_plus_the_shared_namespaces():
    """Every top-level key the template's routing.yaml carries is the framework's, so no plugin can declare it;
    so are the shared resource policy and phase 3's namespaces."""
    template = Path(__file__).resolve().parents[1] / "supervisor" / "template" / "config" / "routing.yaml"
    keys = set(yaml.safe_load(template.read_text(encoding="utf-8")))
    assert _config.framework_sections() == keys | {"gpu", "resources", "plugins"}
    assert {"limits", "roles", "capacity", "timeouts", "verification"} <= keys
