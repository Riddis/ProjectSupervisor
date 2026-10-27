"""Registration only in setup() (phase 2 step 3): `registry.adapter()` and `registry.python_job()` are how an
extension adds a lane or a python job; they refuse to take over a built-in lane or another extension's name, the
registry lists what each project's extensions added. A name stays its registering extension's across
`extensions.reset()`, which keeps registered lanes as it did before 1.11.0 (a project's test suite relies on that);
`supervisor.testing.reset()` forgets them like a fresh process. Importing an extension module must change nothing: `extensions.import_side_effects()` imports it in a fresh
process and names every process-wide registry entry the import added or replaced. Module-level `register()` and
`@job` keep working for old extensions. `supervisor.ext.Registry` documents the hooks, and the real registry
matches it."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from supervisor import ext, extensions, testing
from supervisor.adapters import REGISTRY
from supervisor.adapters.python import JOBS
from supervisor.paths import Layout


def configure(root: Path, modules: dict[str, str], entries: list | None = None) -> Layout:
    return testing.project(root, modules=modules, extensions=entries)


LANE = '''
from supervisor.ext import Adapter, Result

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
    reg = extensions.load(lay)
    assert reg.errors == [] and reg.loaded == ["ref"]
    assert REGISTRY["ref_lane"].name == "ref_lane" and "ref_job" in JOBS
    assert reg.adapters == {"ref_lane": "ref"} and reg.python_jobs == {"ref_job": "ref"}


def test_extensions_reset_keeps_registered_lanes_as_before(tmp_path):
    """A project's tests reset the loader and then look a lane up (`REGISTRY["comfy"]`): 1.10 kept it registered."""
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
    extensions.load(configure(tmp_path, {"ref": SETUP_ONLY, "old": old}))
    extensions.reset()
    assert {"ref_lane", "old_lane"} <= set(REGISTRY) and {"ref_job", "old_job"} <= set(JOBS)


def test_testing_reset_forgets_them_like_a_fresh_process(tmp_path):
    extensions.load(configure(tmp_path, {"ref": SETUP_ONLY}))
    testing.reset()
    assert "ref_lane" not in REGISTRY and "ref_job" not in JOBS
    assert {"python", "process", "human"} <= set(REGISTRY) and "noop" in JOBS


def test_a_name_stays_its_extensions_across_a_reset(tmp_path):
    extensions.load(configure(tmp_path / "a", {"ref": SETUP_ONLY}))
    extensions.reset()
    other = LANE.format(name="ref_lane") + "\ndef setup(registry):\n    registry.adapter(ADAPTER)\n"
    reg = extensions.load(configure(tmp_path / "b", {"other": other}))
    assert reg.loaded == [] and "ref_lane" in reg.errors[0] and "'ref'" in reg.errors[0]


def test_a_reloaded_extension_registers_its_lane_again(tmp_path):
    lay = configure(tmp_path, {"ref": SETUP_ONLY})
    extensions.load(lay)
    first = REGISTRY["ref_lane"]
    extensions.reset()
    reg = extensions.load(lay)
    assert reg.errors == [] and REGISTRY["ref_lane"] is not first   # a new module object, registered again


@pytest.mark.parametrize("taken", ["python", "process", "human"])
def test_an_extension_cannot_take_over_a_built_in_lane(tmp_path, taken):
    lay = configure(tmp_path, {"hijack": LANE.format(name=taken) + "\ndef setup(registry):\n    registry.adapter(ADAPTER)\n"})
    builtin = REGISTRY[taken]
    reg = extensions.load(lay)
    assert reg.loaded == [] and "already registered" in reg.errors[0] and taken in reg.errors[0]
    assert REGISTRY[taken] is builtin


def test_two_extensions_cannot_register_one_lane_name(tmp_path):
    other = LANE.format(name="ref_lane") + "\ndef setup(registry):\n    registry.adapter(ADAPTER)\n"
    lay = configure(tmp_path, {"ref": SETUP_ONLY, "other": other})
    reg = extensions.load(lay)
    assert reg.loaded == ["ref"]
    assert any(e.startswith("other:") and "ref_lane" in e and "ref" in e for e in reg.errors)


def test_python_job_names_are_not_taken_over_either(tmp_path):
    code = "def setup(registry):\n    registry.python_job('noop', lambda root, run_dir, **p: {})\n"
    reg = extensions.load(configure(tmp_path, {"jobs": code}))
    assert reg.loaded == [] and "noop" in reg.errors[0]
    assert JOBS["noop"].__module__ == "supervisor.adapters.python"


@pytest.mark.parametrize("call,error", [
    ("registry.adapter(object())", "name"),
    ("registry.adapter(None)", "name"),
    ("registry.python_job('', lambda root, run_dir: {})", "name"),
    ("registry.python_job('j', 'not callable')", "callable"),
])
def test_registration_refuses_what_is_not_a_lane_or_a_job(tmp_path, call, error):
    reg = extensions.load(configure(tmp_path, {"bad": f"def setup(registry):\n    {call}\n"}))
    assert reg.loaded == [] and error in reg.errors[0]


def test_old_module_level_registration_keeps_working(tmp_path):
    """A 1.10 extension: register(ADAPTER) at import and again in setup(), @job at import."""
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
    reg = extensions.load(configure(tmp_path, {"old": old}))
    assert reg.errors == [] and "old_lane" in REGISTRY and "old_job" in JOBS


# ---------------------------------------------------------------- importing changes nothing

def test_importing_a_setup_only_extension_changes_no_registry(tmp_path):
    lay = configure(tmp_path, {"ref": SETUP_ONLY})
    assert extensions.import_side_effects(lay, "ref") == {"adapters": [], "python_jobs": [], "commands": []}


def test_import_time_registrations_are_named(tmp_path):
    lay = configure(tmp_path, {
        "lane_at_import": LANE.format(name="eager_lane") + "\nfrom supervisor.adapters.base import register\n"
                                                           "register(ADAPTER)\ndef setup(registry):\n    pass\n",
        "job_at_import": "from supervisor.adapters.python import job\n@job('eager_job')\n"
                         "def eager(root, run_dir):\n    return {}\ndef setup(registry):\n    pass\n",
        "command_at_import": "from supervisor import commands\n@commands.handler('eager.cmd')\n"
                             "def cmd(layout, payload, actor):\n    return {}\ndef setup(registry):\n    pass\n",
    })
    assert extensions.import_side_effects(lay, "lane_at_import")["adapters"] == ["eager_lane"]
    assert extensions.import_side_effects(lay, "job_at_import")["python_jobs"] == ["eager_job"]
    assert extensions.import_side_effects(lay, "command_at_import")["commands"] == ["eager.cmd"]


def test_a_registration_hidden_in_a_helper_module_is_found(tmp_path):
    """The risk the plan names: bench imports probes, whose @job decorators register at import."""
    lay = configure(tmp_path, {
        "helper": "from supervisor.adapters.python import job\n@job('hidden_job')\ndef hidden(root, run_dir):\n"
                  "    return {}\n",
        "entry": "from . import helper\ndef setup(registry):\n    pass\n",
    }, entries=["entry"])
    assert extensions.import_side_effects(lay, "entry")["python_jobs"] == ["hidden_job"]


def test_an_extension_that_does_not_import_is_an_error_not_a_clean_result(tmp_path):
    lay = configure(tmp_path, {"broken": "raise RuntimeError('boom at import')\n"})
    with pytest.raises(extensions.ExtensionError, match="boom at import"):
        extensions.import_side_effects(lay, "broken")


# ---------------------------------------------------------------- the documented registry

def test_the_documented_registry_matches_the_real_one():
    """ext.Registry is what extension authors read and what G2 records; the loader's Registry must accept every
    call it documents (same parameter names, kinds and defaults), so the two cannot drift apart."""
    def params(fn):
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]
    hooks = [n for n, v in vars(ext.Registry).items() if not n.startswith("_") and callable(v)]
    assert {"adapter", "python_job", "doctor_check", "command", "inbox_kind", "ui_router"} <= set(hooks)
    for name in hooks:
        assert params(getattr(ext.Registry, name)) == params(getattr(extensions.Registry, name)), name
    assert "layout" in ext.Registry.__annotations__
