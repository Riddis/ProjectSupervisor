"""Registration only in setup() (phase 2 step 3): `registry.adapter()` and `registry.python_job()` are how an
plugin adds a lane or a python job; they refuse to take over a built-in lane or another plugin's name, the
registry lists what each project's plugins added. A name stays its registering plugin's across
`plugins.reset()`, which keeps registered lanes as it did before 1.11.0 (a project's test suite relies on that);
`supervisor.testing.reset()` forgets them like a fresh process. Importing a plugin module must change nothing: `plugins.import_side_effects()` imports it in a fresh
process and names every process-wide registry entry the import added or replaced. Module-level `register()` and
`@job` keep working for old plugins. `supervisor.plugin.Registry` documents the hooks, and the real registry
matches it."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from supervisor import plugin, plugins, testing
from supervisor.adapters import REGISTRY
from supervisor.adapters.python import JOBS
from supervisor.paths import Layout


def configure(root: Path, modules: dict[str, str], entries: list | None = None) -> Layout:
    return testing.project(root, modules=modules, plugins=entries)


LANE = '''
from supervisor.plugin import Adapter, Result

class Lane(Adapter):
    name = "{name}"
    def prepare(self, layout, task, attempt_id):
        return {{"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(layout.runs)}}
    def submit(self, spec):
        return {{}}
    def alive(self, handle):
        return False
    def collect(self, spec, handle):
        return Result(status="COMPLETE")

ADAPTER = Lane()
'''

SETUP_ONLY = LANE.format(name="ref_lane") + '''
def job(root, run_dir, **params):
    return {"status": "COMPLETE", "artifacts": [], "notes": ""}

def setup(registry):
    registry.adapter(ADAPTER)
    registry.python_job("ref_job", job)
'''


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


# ---------------------------------------------------------------- the documented path

def test_setup_registers_lanes_and_jobs_and_the_registry_lists_them(tmp_path):
    lay = configure(tmp_path, {"ref": SETUP_ONLY})
    reg = plugins.load(lay)
    assert reg.errors == [] and reg.loaded == ["ref"]
    assert REGISTRY["ref_lane"].name == "ref_lane" and "ref_job" in JOBS
    assert reg.adapters == {"ref_lane": "ref"} and reg.python_jobs == {"ref_job": "ref"}


def test_plugins_reset_keeps_registered_lanes_as_before(tmp_path):
    """A project's tests reset the loader and then look a lane up (`REGISTRY["render"]`): 1.10 kept it registered."""
    old = LANE.format(name="old_lane") + '''
from supervisor.adapters.base import register
from supervisor.adapters.python import job
register(ADAPTER)

@job("old_job")
def old_job(root, run_dir, **params):
    return {"status": "COMPLETE"}

def setup(registry):
    registry.adapter(ADAPTER)
'''
    plugins.load(configure(tmp_path, {"ref": SETUP_ONLY, "old": old}))
    plugins.reset()
    assert {"ref_lane", "old_lane"} <= set(REGISTRY) and {"ref_job", "old_job"} <= set(JOBS)


def test_testing_reset_forgets_them_like_a_fresh_process(tmp_path):
    plugins.load(configure(tmp_path, {"ref": SETUP_ONLY}))
    testing.reset()
    assert "ref_lane" not in REGISTRY and "ref_job" not in JOBS
    assert {"python", "process", "human"} <= set(REGISTRY) and "noop" in JOBS


def test_after_a_reset_a_fresh_project_may_register_the_same_lane(tmp_path):
    """The pattern of a project's tests: a fresh temporary project per test, each with its own copy of a plugin
    (another file), loaded after plugins.reset()."""
    plugins.load(configure(tmp_path / "a", {"ref": SETUP_ONLY}))
    plugins.reset()
    reg = plugins.load(configure(tmp_path / "b", {"ref": SETUP_ONLY}))
    assert reg.errors == [] and reg.loaded == ["ref"]


def test_a_lane_registered_only_at_import_is_not_taken_over_either(tmp_path):
    """An old-style plugin's lane, registered at import and never through the registry, is protected like a
    built-in one."""
    eager = LANE.format(name="eager_lane") + "\nfrom supervisor.adapters.base import register\nregister(ADAPTER)\n"
    eager += "\ndef setup(registry):\n    pass\n"
    taker = LANE.format(name="eager_lane") + "\ndef setup(registry):\n    registry.adapter(ADAPTER)\n"
    reg = plugins.load(configure(tmp_path, {"eager": eager, "taker": taker}))
    assert reg.loaded == ["eager"] and any(e.startswith("taker:") and "eager_lane" in e for e in reg.errors)


def test_a_reloaded_plugin_registers_its_lane_again(tmp_path):
    lay = configure(tmp_path, {"ref": SETUP_ONLY})
    plugins.load(lay)
    first = REGISTRY["ref_lane"]
    plugins.reset()
    reg = plugins.load(lay)
    assert reg.errors == [] and REGISTRY["ref_lane"] is not first   # a new module object, registered again


@pytest.mark.parametrize("taken", ["python", "process", "human"])
def test_a_plugin_cannot_take_over_a_built_in_lane(tmp_path, taken):
    lay = configure(tmp_path, {"hijack": LANE.format(name=taken) + "\ndef setup(registry):\n    registry.adapter(ADAPTER)\n"})
    builtin = REGISTRY[taken]
    reg = plugins.load(lay)
    assert reg.loaded == [] and "already registered" in reg.errors[0] and taken in reg.errors[0]
    assert REGISTRY[taken] is builtin


def test_two_plugins_cannot_register_one_lane_name(tmp_path):
    other = LANE.format(name="ref_lane") + "\ndef setup(registry):\n    registry.adapter(ADAPTER)\n"
    lay = configure(tmp_path, {"ref": SETUP_ONLY, "other": other})
    reg = plugins.load(lay)
    assert reg.loaded == ["ref"]
    assert any(e.startswith("other:") and "ref_lane" in e and "ref" in e for e in reg.errors)


def test_python_job_names_are_not_taken_over_either(tmp_path):
    code = "def setup(registry):\n    registry.python_job('noop', lambda root, run_dir, **p: {})\n"
    reg = plugins.load(configure(tmp_path, {"jobs": code}))
    assert reg.loaded == [] and "noop" in reg.errors[0]
    assert JOBS["noop"].__module__ == "supervisor.adapters.python"


@pytest.mark.parametrize("call,error", [
    ("registry.adapter(object())", "name"),
    ("registry.adapter(None)", "name"),
    ("registry.python_job('', lambda root, run_dir: {})", "name"),
    ("registry.python_job('j', 'not callable')", "callable"),
])
def test_registration_refuses_what_is_not_a_lane_or_a_job(tmp_path, call, error):
    reg = plugins.load(configure(tmp_path, {"bad": f"def setup(registry):\n    {call}\n"}))
    assert reg.loaded == [] and error in reg.errors[0]


def test_old_module_level_registration_keeps_working(tmp_path):
    """A 1.10 plugin: register(ADAPTER) at import and again in setup(), @job at import."""
    old = LANE.format(name="old_lane") + '''
from supervisor.adapters.base import register
from supervisor.adapters.python import job
register(ADAPTER)

@job("old_job")
def old_job(root, run_dir, **params):
    return {"status": "COMPLETE"}

def setup(registry):
    registry.adapter(ADAPTER)
'''
    reg = plugins.load(configure(tmp_path, {"old": old}))
    assert reg.errors == [] and "old_lane" in REGISTRY and "old_job" in JOBS


# ---------------------------------------------------------------- importing changes nothing

def test_importing_a_setup_only_plugin_changes_no_registry(tmp_path):
    lay = configure(tmp_path, {"ref": SETUP_ONLY})
    assert plugins.import_side_effects(lay, "ref") == {"adapters": [], "python_jobs": [], "commands": []}


def test_import_time_registrations_are_named(tmp_path):
    lay = configure(tmp_path, {
        "lane_at_import": LANE.format(name="eager_lane") + "\nfrom supervisor.adapters.base import register\n"
                                                           "register(ADAPTER)\ndef setup(registry):\n    pass\n",
        "job_at_import": "from supervisor.adapters.python import job\n@job('eager_job')\n"
                         "def eager(root, run_dir):\n    return {}\ndef setup(registry):\n    pass\n",
        "command_at_import": "from supervisor import commands\n@commands.handler('eager.cmd')\n"
                             "def cmd(layout, payload, actor):\n    return {}\ndef setup(registry):\n    pass\n",
    })
    assert plugins.import_side_effects(lay, "lane_at_import")["adapters"] == ["eager_lane"]
    assert plugins.import_side_effects(lay, "job_at_import")["python_jobs"] == ["eager_job"]
    assert plugins.import_side_effects(lay, "command_at_import")["commands"] == ["eager.cmd"]


def test_a_registration_hidden_in_a_helper_module_is_found(tmp_path):
    """The risk the plan names: bench imports probes, whose @job decorators register at import."""
    lay = configure(tmp_path, {
        "helper": "from supervisor.adapters.python import job\n@job('hidden_job')\ndef hidden(root, run_dir):\n"
                  "    return {}\n",
        "entry": "from . import helper\ndef setup(registry):\n    pass\n",
    }, entries=["entry"])
    assert plugins.import_side_effects(lay, "entry")["python_jobs"] == ["hidden_job"]


def test_a_plugin_that_does_not_import_is_an_error_not_a_clean_result(tmp_path):
    lay = configure(tmp_path, {"broken": "raise RuntimeError('boom at import')\n"})
    with pytest.raises(plugins.PluginError, match="boom at import"):
        plugins.import_side_effects(lay, "broken")


# ---------------------------------------------------------------- the documented registry

def test_the_documented_registry_matches_the_real_one():
    """plugin.Registry is what plugin authors read and what G2 records; the loader's Registry must accept every
    call it documents (same parameter names, kinds and defaults), so the two cannot drift apart."""
    def params(fn):
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]
    hooks = [n for n, v in vars(plugin.Registry).items() if not n.startswith("_") and callable(v)]
    assert {"adapter", "python_job", "doctor_check", "command", "inbox_kind", "ui_router"} <= set(hooks)
    for name in hooks:
        assert params(getattr(plugin.Registry, name)) == params(getattr(plugins.Registry, name)), name
    assert "layout" in plugin.Registry.__annotations__


def test_one_source_file_is_one_plugin_whatever_it_is_configured_as(tmp_path, monkeypatch):
    """A project's own tests import a plugin under its installed name (`plugins.render`) while the loader
    imports the same file from the project folder (`render`): the same plugin, so it may register its lane under
    either configuration, and code in that file is that plugin's."""
    shared = tmp_path / "shared"
    (shared / "plugins").mkdir(parents=True)
    (shared / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    source = SETUP_ONLY + "\nfrom supervisor import plugin\n\ndef settings(layout):\n    return plugin.config(layout, 'ref')\n"
    source = source.replace("def setup(registry):\n", "def setup(registry):\n    registry.config_sections('ref')\n")
    (shared / "plugins" / "ref.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(shared))
    local = testing.project(shared, plugins=["ref"])                                # loaded from its folder
    assert plugins.load(local).errors == []
    plugins.reset()
    installed = testing.project(tmp_path / "installed", plugins=["plugins.ref"])   # imported by dotted name
    reg = plugins.load(installed)
    assert reg.errors == [] and reg.loaded == ["plugins.ref"]
    import plugins.ref as plain  # the test's own import
    assert plain.settings(installed) == {}


BUNDLE = '''
from supervisor import plugin

def setup(registry):
    registry.config_sections("parent")
'''
CHILD = '''
from supervisor import plugin

def setup(registry):
    registry.config_sections("child")
    registry.inbox_kind("child_note", "Child notes")

def read(layout):
    return plugin.config(layout, "child")

def log(layout):
    plugin.log_event(layout, "child_event", "owner")

def items(layout):
    return plugin.owned_items(layout)
'''


@pytest.mark.parametrize("order", [["bundle", "bundle.child"], ["bundle.child", "bundle"]])
def test_the_most_specific_plugin_owns_its_module(tmp_path, order):
    """Review 1.11.0 F4: with a package `bundle` and its module `bundle.child` both configured, code in child.py is
    `bundle.child`'s, whatever the order: its config section, its inbox kind and its log lines."""
    lay = testing.project(tmp_path, plugins=order, routing={"child": {"x": 1}},
                          files={"plugins/bundle/__init__.py": BUNDLE, "plugins/bundle/child.py": CHILD})
    reg = plugins.load(lay)
    assert reg.errors == [] and sorted(reg.loaded) == ["bundle", "bundle.child"]
    child = plugins._import(lay, "bundle.child")
    assert child.read(lay) == {"x": 1}
    child.log(lay)
    import json
    events = [json.loads(line) for line in lay.log.read_text(encoding="utf-8").splitlines()]
    assert next(e for e in events if e["event"] == "child_event")["actor"] == "bundle.child:owner"
    assert child.items(lay) == []


def test_one_source_file_may_register_its_command_kinds_from_both_of_its_imports(tmp_path, monkeypatch):
    """Found rehearsing the 1.11.0 window: a project's test fixture loads its plugins by dotted name
    (`plugins.ref`) and a test then loads the project itself (`ref`), in one process and without a reset. The
    same file registers its command kind from two module objects; like its lanes, that is one plugin, not a
    takeover. Another file still may not take the kind."""
    shared = tmp_path / "shared"
    (shared / "plugins").mkdir(parents=True)
    (shared / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    source = "def handle(layout, payload, actor):\n    return {}\n\ndef setup(registry):\n    registry.command('ref.do', handle)\n"
    (shared / "plugins" / "ref.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(shared))
    taker = source.replace("def handle", "def handle2").replace(", handle)", ", handle2)")
    installed = testing.project(tmp_path / "installed", plugins=["plugins.ref"])
    local = testing.project(shared, plugins=["ref"])
    other = testing.project(tmp_path / "other", modules={"taker": taker})          # each of these resets: build first
    assert plugins.load(installed).errors == []
    assert plugins.load(local).errors == []                                   # no reset in between
    reg = plugins.load(other)
    assert reg.loaded == [] and "ref.do" in reg.errors[0]
