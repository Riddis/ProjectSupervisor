"""Doctor and migrate do not depend on loading extensions (phase 3 step 8).

`supervisor migrate` never sets an extension up. Its migrations read manifests through discovery and import only
the migration modules manifests name, so a project whose extensions fail to load can still be migrated.

`supervisor doctor` gets the static half of G6 from the manifests alone, in the check `extension_manifests`, so it is
there even when loading fails:
- FAIL: a manifest discovery refuses, or a framework range that excludes this framework.
- WARN: an extension without a manifest (required by a later major release), and configuration still in the old
  top-level sections (run `supervisor migrate`).

The dynamic checks still come from setup(). A check an extension with a manifest registers must report a name its
manifest declares in `provides.doctor_checks`; any other name is a FAIL."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from supervisor import doctor, extensions, testing
from supervisor.cli import app

runner = CliRunner()

TRAP = '''import pathlib
def setup(registry):
    pathlib.Path({marker!r}).write_text("setup ran")
    raise RuntimeError("this extension never loads")
'''


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def manifest(name: str, framework: str = ">=1.11", extra: str = "") -> str:
    return f'identity = "{name}"\nversion = "2.0.0"\nframework = "{framework}"\n{extra}'


def by_name(checks, name):
    return next(c for c in checks if c.name == name)


def test_migrate_runs_without_setting_any_extension_up(tmp_path, monkeypatch):
    marker = tmp_path / "setup.txt"
    layout = testing.project(tmp_path / "p", modules={"broken": TRAP.format(marker=str(marker))})
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert not marker.exists()


def test_doctor_reports_the_manifests_even_when_loading_fails(tmp_path, monkeypatch):
    marker = tmp_path / "setup.txt"
    layout = testing.project(tmp_path / "p", modules={"broken": TRAP.format(marker=str(marker))},
                             files={"extensions/broken.toml": manifest("broken")})
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "extension_manifests: broken 2.0.0 (>=1.11)" in result.output and "this extension never loads" in result.output


def test_the_manifest_check_fails_a_refused_or_incompatible_manifest(tmp_path):
    layout = testing.project(tmp_path, modules={"future": "def setup(r):\n    pass\n",
                                                "odd": "def setup(r):\n    pass\n"},
                             files={"extensions/future.toml": manifest("future", ">=99"),
                                    "extensions/odd.toml": manifest("someone_else")})
    check = doctor.check_extension_manifests(layout)
    assert not check.ok and ">=99" in check.detail and "someone_else" in check.detail


def test_the_manifest_check_warns_about_extensions_without_one(tmp_path):
    layout = testing.project(tmp_path, modules={"old": "def setup(r):\n    pass\n",
                                                "new": "def setup(r):\n    pass\n"},
                             files={"extensions/new.toml": manifest("new")})
    check = doctor.check_extension_manifests(layout)
    assert check.ok and check.warn and "old" in check.detail and "supervisor-extension.toml" in check.detail


def test_the_manifest_check_warns_about_configuration_not_yet_moved(tmp_path):
    extra = ('[config]\nsection = "extensions.lane"\nschema = "lane:Settings"\nlegacy_sections = ["lane"]\n')
    layout = testing.project(tmp_path, modules={"lane": "def setup(r):\n    pass\n"},
                             files={"extensions/lane.toml": manifest("lane", extra=extra)},
                             routing={"lane": {"a": 1}, "gpu": {"usable_vram_floor_mb": 6500}})
    check = doctor.check_extension_manifests(layout)
    assert check.ok and check.warn and "supervisor migrate" in check.detail
    assert "lane" in check.detail and "gpu" in check.detail


def test_a_clean_project_has_a_quiet_manifest_check(tmp_path):
    layout = testing.project(tmp_path, modules={"new": "def setup(r):\n    pass\n"},
                             files={"extensions/new.toml": manifest("new")})
    check = doctor.check_extension_manifests(layout)
    assert check.ok and not check.warn and check.detail == "new 2.0.0 (>=1.11)"


CHECKER = '''from supervisor.ext import Check
def check(layout):
    return Check("{reported}", True, "fine")
def setup(registry):
    registry.doctor_check(check)
'''


@pytest.mark.parametrize("reported,ok", [("lane_health", True), ("someone_elses", False)])
def test_a_doctor_check_must_report_a_name_its_manifest_declares(tmp_path, reported, ok):
    layout = testing.project(tmp_path, modules={"lane": CHECKER.format(reported=reported)},
                             files={"extensions/lane.toml": manifest(
                                 "lane", extra='[provides]\ndoctor_checks = ["lane_health"]\n')})
    assert extensions.load(layout).loaded == ["lane"]
    check = by_name(doctor.run(layout), reported)
    assert check.ok is ok
    if not ok:
        assert "provides.doctor_checks" in check.detail and "lane" in check.detail
