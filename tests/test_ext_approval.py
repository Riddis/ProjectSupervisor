"""Approval before import (phase 3 step 4): every configured extension is discovered first, and what can be decided
from the manifests is decided before any extension module is imported. A refused extension never executes; each
refusal is recorded like a load failure (a required one blocks the loop). Refused:
- an extension whose framework range excludes the installed framework;
- a manifest that claims a built-in identifier (a lane, python job, command kind or inbox kind of the framework's);
- an identifier two manifests claim: both are refused, since which one is meant is the owner's call;
- a `requires.extensions` entry that project.yaml does not list, and a dependency cycle.

Extensions load after the ones they require, whatever order project.yaml lists them in (decision D6). One whose
dependency did not load is refused before its import."""
from __future__ import annotations

from pathlib import Path

import pytest

from supervisor import __version__, extensions, testing

TRAP = '''import pathlib
pathlib.Path({marker!r}).write_text("imported")
ORDER = {order!r}
def setup(registry):
    pathlib.Path(ORDER).open("a").write("{name}\\n")
'''

MANIFEST = 'identity = "{name}"\nversion = "1.0.0"\nframework = "{framework}"\n{extra}'


def project(tmp_path, specs: dict[str, dict], required=True):
    """`specs`: name -> {"framework", "extra" (manifest text after the header), "manifest" (False: none)}."""
    order = tmp_path / "order.txt"
    modules, files = {}, {}
    for name, spec in specs.items():
        modules[name] = TRAP.format(marker=str(tmp_path / f"{name}.imported"), order=str(order), name=name)
        if spec.get("manifest", True):
            files[f"extensions/{name}.toml"] = MANIFEST.format(name=name, framework=spec.get("framework", ">=1.11"),
                                                               extra=spec.get("extra", ""))
    layout = testing.project(tmp_path / "proj", modules=modules, files=files,
                             extensions=[{"name": n, "required": required} for n in specs])
    return layout, order


def imported(tmp_path, name: str) -> bool:
    return (tmp_path / f"{name}.imported").exists()


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def test_an_incompatible_extension_is_refused_and_never_runs(tmp_path):
    layout, _order = project(tmp_path, {"future": {"framework": ">=99"}, "fine": {}})
    reg = extensions.load(layout)
    assert not imported(tmp_path, "future") and reg.loaded == ["fine"]
    error = next(e for e in reg.errors if e.startswith("future:"))
    assert ">=99" in error and __version__ in error and any(b.startswith("future:") for b in reg.blocking)


@pytest.mark.parametrize("kind,identifier", [
    ("adapters", "claude_cli"), ("commands", "loop.pause"), ("inbox_kinds", "gate"),
], ids=["built-in-lane", "built-in-command", "built-in-inbox-kind"])
def test_a_manifest_claiming_a_built_in_identifier_is_refused_before_import(tmp_path, kind, identifier):
    extra = f'[provides]\n{kind} = ["{identifier}"]\n[legacy_ids]\n{kind} = ["{identifier}"]\n'
    layout, _order = project(tmp_path, {"grabber": {"extra": extra}})
    reg = extensions.load(layout)
    assert not imported(tmp_path, "grabber") and not reg.loaded
    assert any(identifier in e and "built in" in e for e in reg.errors)


def test_an_identifier_two_manifests_claim_refuses_both(tmp_path):
    extra = '[provides]\nadapters = ["shared_lane"]\n[legacy_ids]\nadapters = ["shared_lane"]\n'
    layout, _order = project(tmp_path, {"one": {"extra": extra}, "two": {"extra": extra}, "other": {}})
    reg = extensions.load(layout)
    assert reg.loaded == ["other"] and not imported(tmp_path, "one") and not imported(tmp_path, "two")
    for name, rival in (("one", "two"), ("two", "one")):
        error = next(e for e in reg.errors if e.startswith(f"{name}:"))
        assert "shared_lane" in error and rival in error


def test_extensions_load_after_what_they_require_whatever_the_listed_order(tmp_path):
    layout, order = project(tmp_path, {"bench": {"extra": '[requires]\nextensions = ["comfy"]\n'}, "comfy": {},
                                       "notes": {"manifest": False}})
    reg = extensions.load(layout)
    assert reg.loaded == ["comfy", "bench", "notes"]
    assert order.read_text().split() == ["comfy", "bench", "notes"]


def test_a_requirement_project_yaml_does_not_list_is_refused(tmp_path):
    layout, _order = project(tmp_path, {"bench": {"extra": '[requires]\nextensions = ["comfy"]\n'}})
    reg = extensions.load(layout)
    assert not imported(tmp_path, "bench") and any("comfy" in e and "project.yaml" in e for e in reg.errors)


def test_a_dependency_that_did_not_load_refuses_its_dependants(tmp_path):
    layout, _order = project(tmp_path, {"comfy": {"framework": ">=99"},
                                        "bench": {"extra": '[requires]\nextensions = ["comfy"]\n'}})
    reg = extensions.load(layout)
    assert not reg.loaded and not imported(tmp_path, "bench")
    assert any(e.startswith("bench:") and "comfy" in e and "refused" in e for e in reg.errors)   # decided before import


def test_a_dependency_cycle_is_refused(tmp_path):
    layout, _order = project(tmp_path, {"a": {"extra": '[requires]\nextensions = ["b"]\n'},
                                        "b": {"extra": '[requires]\nextensions = ["a"]\n'}, "c": {}})
    reg = extensions.load(layout)
    assert reg.loaded == ["c"] and not imported(tmp_path, "a") and not imported(tmp_path, "b")
    assert any("cycle" in e for e in reg.errors)


def test_an_optional_extension_refused_at_approval_does_not_block(tmp_path):
    layout, _order = project(tmp_path, {"future": {"framework": ">=99"}}, required=False)
    reg = extensions.load(layout)
    assert reg.errors and not reg.blocking and not imported(tmp_path, "future")


def test_extensions_without_a_manifest_are_not_held_up_by_approval(tmp_path):
    layout, order = project(tmp_path, {"old": {"manifest": False}, "new": {}})
    reg = extensions.load(layout)
    assert reg.loaded == ["old", "new"] and reg.unmanifested == ["old"]
    assert Path(order).read_text().split() == ["old", "new"]
