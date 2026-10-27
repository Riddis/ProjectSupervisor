"""Staged, transactional loading (phase 3 step 3): an extension's setup() registers into a staging area, and one
commit applies everything it registered, or nothing. A failure anywhere leaves nothing behind, for required and
optional extensions alike:
- an exception midway through setup();
- a registration its manifest does not declare;
- a manifest discovery refuses;
- an import error.

The process-wide tables (lanes, python jobs, command kinds) are not touched before the commit, and the project's
registry is put back as it was. A module that failed is dropped from sys.modules, so the next load imports it
afresh. An extension without a manifest still loads (decision D1), staged the same way, and is listed as
unmanifested."""
from __future__ import annotations

import dataclasses
import re
import sys

import pytest

from supervisor import commands, extensions, testing
from supervisor.adapters import REGISTRY
from supervisor.adapters.python import JOBS

LANE = '''
import typer
from supervisor.ext import Adapter, Check, Result

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

def job(root, run_dir, **params):
    return {{"status": "COMPLETE", "artifacts": [], "notes": ""}}

def handle(layout, payload, actor):
    return {{}}

def check(layout):
    return Check("{name}", True, "ok")

def everything(registry, prefix):
    registry.adapter(Lane())
    registry.python_job(prefix + "_job", job)
    registry.command(prefix + ".do", handle)
    registry.inbox_kind(prefix + "_item", "Items")
    registry.doctor_check(check)
    registry.cli(typer.Typer(), prefix)
    registry.overview_panel(lambda layout: {{"title": prefix, "rows": []}})
'''

FAILS_MIDWAY = LANE.format(name="half") + '''
def setup(registry):
    everything(registry, "half")
    raise RuntimeError("setup failed after registering")
'''

WORKS = LANE.format(name="whole") + '''
def setup(registry):
    everything(registry, "whole")
'''

MANIFEST = '''identity = "{name}"
version = "1.0.0"
framework = ">=1.11"
[provides]
adapters = ["{name}"]
python_jobs = ["{name}_job"]
commands = ["{name}.do"]
inbox_kinds = ["{name}_item"]
cli = ["{name}"]
hooks = ["overview_panel"]
'''


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def registered(prefix: str) -> dict[str, bool]:
    return {"adapter": prefix in REGISTRY, "job": f"{prefix}_job" in JOBS, "command": f"{prefix}.do" in commands.HANDLERS}


def fields_of(reg) -> dict:
    """Every dataclass field of the registry, as plain data, except what records the outcome of a load."""
    skip = {"layout", "errors", "blocking", "loaded", "internal_imports", "unmanifested", "manifests"}
    return {f.name: re.sub(r" (object )?at 0x[0-9A-Fa-f]+", "", repr(getattr(reg, f.name)))   # two projects' objects
            for f in dataclasses.fields(reg) if f.name not in skip}


@pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
def test_an_exception_midway_through_setup_leaves_nothing_registered(tmp_path, required):
    layout = testing.project(tmp_path, modules={"half": FAILS_MIDWAY},
                             extensions=[{"name": "half", "required": required}])
    reg = extensions.load(layout)
    assert registered("half") == {"adapter": False, "job": False, "command": False}
    assert reg.inbox_kinds == {} and reg.doctor_checks == [] and reg.cli_apps == [] and reg.overview_panels == []
    assert reg.adapters == {} and reg.python_jobs == {} and reg.commands == {}
    assert any("setup failed after registering" in e for e in reg.errors)
    assert bool(reg.blocking) is required and "half" not in reg.loaded


def test_the_registry_is_put_back_exactly_after_a_failure(tmp_path):
    """Every field, not only the ones a test thinks of: a hook added later without staging fails here."""
    layout = testing.project(tmp_path, modules={"whole": WORKS, "half": FAILS_MIDWAY},
                             files={"extensions/whole.toml": MANIFEST.format(name="whole")})
    reg = extensions.load(layout)
    after_whole_only = testing.project(tmp_path / "b", modules={"whole": WORKS},
                                       files={"extensions/whole.toml": MANIFEST.format(name="whole")})
    clean = extensions.load(after_whole_only)
    mine, theirs = fields_of(reg), fields_of(clean)
    for name in mine:
        if name in ("origins", "_declared", "projection_paths"):
            continue                                       # hold paths, which differ between the two projects
        assert mine[name] == theirs[name], name


def test_a_failed_extension_does_not_stop_the_next_one_and_its_names_stay_free(tmp_path):
    second = FAILS_MIDWAY.replace('raise RuntimeError("setup failed after registering")', "pass")
    layout = testing.project(tmp_path, modules={"half": FAILS_MIDWAY, "again": second},
                             extensions=[{"name": "half", "required": False}, {"name": "again", "required": False}])
    reg = extensions.load(layout)
    assert reg.loaded == ["again"] and all(registered("half").values())    # "again" took the names "half" left


@pytest.mark.filterwarnings("ignore:supervisor.*framework internal:DeprecationWarning")   # the probe reads them
def test_the_process_wide_tables_are_untouched_until_the_commit(tmp_path):
    seen = {}
    probe = WORKS.replace('    everything(registry, "whole")',
                          '    everything(registry, "whole")\n'
                          '    from supervisor.adapters import REGISTRY\n'
                          '    from supervisor.adapters.python import JOBS\n'
                          '    from supervisor import commands\n'
                          '    SEEN.update(adapter="whole" in REGISTRY, job="whole_job" in JOBS, '
                          'command="whole.do" in commands.HANDLERS)')
    layout = testing.project(tmp_path, modules={"whole": "SEEN = {}\n" + probe})
    sys.modules.pop("probe_seen", None)
    reg = extensions.load(layout)
    module = next(m for name, m in sys.modules.items() if name.endswith(".whole"))
    seen.update(module.SEEN)
    assert seen == {"adapter": False, "job": False, "command": False}          # staged while setup() ran
    assert reg.loaded == ["whole"] and all(registered("whole").values())       # committed after it


def test_a_registration_the_manifest_does_not_declare_is_refused_and_nothing_is_kept(tmp_path):
    narrow = MANIFEST.format(name="whole").replace('python_jobs = ["whole_job"]\n', "")
    layout = testing.project(tmp_path, modules={"whole": WORKS}, files={"extensions/whole.toml": narrow})
    reg = extensions.load(layout)
    assert registered("whole") == {"adapter": False, "job": False, "command": False}
    assert any("whole_job" in e and "python_jobs" in e for e in reg.errors) and reg.blocking


def test_a_hook_without_an_identifier_must_be_declared_too(tmp_path):
    no_hooks = MANIFEST.format(name="whole").replace('hooks = ["overview_panel"]\n', "")
    layout = testing.project(tmp_path, modules={"whole": WORKS}, files={"extensions/whole.toml": no_hooks})
    reg = extensions.load(layout)
    assert any("overview_panel" in e and "hooks" in e for e in reg.errors) and not reg.overview_panels


def test_an_extension_whose_registrations_match_its_manifest_loads(tmp_path):
    layout = testing.project(tmp_path, modules={"whole": WORKS},
                             files={"extensions/whole.toml": MANIFEST.format(name="whole")})
    reg = extensions.load(layout)
    assert reg.loaded == ["whole"] and not reg.errors and all(registered("whole").values())
    assert reg.manifests["whole"].identity == "whole" and reg.unmanifested == []


def test_an_extension_without_a_manifest_still_loads_and_is_listed(tmp_path):
    layout = testing.project(tmp_path, modules={"whole": WORKS})
    reg = extensions.load(layout)
    assert reg.loaded == ["whole"] and reg.unmanifested == ["whole"] and all(registered("whole").values())


def test_a_manifest_discovery_refuses_stops_the_extension_before_its_import(tmp_path):
    marker = tmp_path / "imported.txt"
    trap = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\ndef setup(registry):\n    pass\n"
    layout = testing.project(tmp_path, modules={"trap": trap},
                             files={"extensions/trap.toml": 'identity = "other"\nversion = "1"\nframework = ">=1"\n'})
    reg = extensions.load(layout)
    assert any("other" in e for e in reg.errors) and reg.blocking and not marker.exists()


def test_a_module_that_failed_is_dropped_and_imported_afresh_next_time(tmp_path):
    broken = "raise ImportError('broken at import')\n"
    layout = testing.project(tmp_path, modules={"broken": broken},
                             extensions=[{"name": "broken", "required": False}])
    reg = extensions.load(layout)
    assert any("broken at import" in e for e in reg.errors)
    assert not [name for name in sys.modules if name.endswith(".broken")]
    half = testing.project(tmp_path / "b", modules={"half": FAILS_MIDWAY},
                           extensions=[{"name": "half", "required": False}])
    extensions.load(half)
    assert not [name for name in sys.modules if name.endswith(".half")]
