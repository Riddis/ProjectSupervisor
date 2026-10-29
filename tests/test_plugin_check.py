"""G5, `supervisor plugin check <name>` (phase 3 step 9): whether one plugin conforms to the standard.

It checks conformance only, and never runs the plugin's own tests (the G5 workflow runs those beside it):
- `manifest`: read and valid, without importing anything;
- `framework`: the range includes the installed framework;
- `imports`: the plugin's source imports `supervisor.plugin` only (`supervisor.testing` too, in its tests);
- `import_side_effects`: importing the module in a fresh interpreter changes no shared registry;
- `registrations`: setup(), run against a staging registry that is never committed, registers exactly what
  `provides` declares, no more and no less;
- `idempotent`: a second setup() registers the same;
- `config`: the project's section validates against the manifest's schema.

A plugin without a manifest fails `manifest`, and the checks that need one are reported as not run."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from supervisor import commands, plugin_check, testing
from supervisor.adapters import REGISTRY
from supervisor.cli import app

runner = CliRunner()

GOOD = '''
from pydantic import BaseModel
from supervisor import plugin

class Settings(BaseModel):
    base_url: str

def job(root, run_dir, **params):
    return {"status": "COMPLETE", "artifacts": [], "notes": ""}

def handle(layout, payload, actor):
    return {}

def setup(registry):
    plugin.config(registry.layout, "notes")
    registry.python_job(registry.identity + "_sync", job)
    registry.command(registry.identity + ".refresh", handle)
'''

MANIFEST = '''identity = "notes"
version = "1.0.0"
framework = ">=1.11"
[provides]
python_jobs = ["notes_sync"]
commands = ["notes.refresh"]
[config]
section = "plugins.notes"
schema = "notes:Settings"
'''

ROUTING = {"plugins": {"notes": {"base_url": "http://127.0.0.1:9"}}}


def project(tmp_path, source: str = GOOD, manifest: str | None = MANIFEST, routing: dict = ROUTING, files=None):
    files = dict(files or {})
    if manifest is not None:
        files["plugins/notes.toml"] = manifest
    return testing.project(tmp_path, modules={"notes": source}, files=files, routing=routing)


def results(layout) -> dict[str, plugin_check.Finding]:
    return {f.name: f for f in plugin_check.run(layout, "notes")}


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


def test_a_conforming_plugin_passes_every_check_and_nothing_is_committed(tmp_path):
    layout = project(tmp_path)
    found = results(layout)
    assert [n for n, f in found.items() if not f.ok] == [], {n: f.detail for n, f in found.items()}
    assert set(found) == {"manifest", "framework", "imports", "import_side_effects", "registrations", "idempotent",
                          "config"}
    assert "notes.refresh" not in commands.HANDLERS        # staged twice, never committed


def test_the_command_prints_each_check_and_exits_by_the_result(tmp_path, monkeypatch):
    layout = project(tmp_path)
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    ok = runner.invoke(app, ["plugin", "check", "notes"])
    assert ok.exit_code == 0, ok.output
    assert "OK   registrations" in ok.output and "OK   manifest" in ok.output
    bad = project(tmp_path / "bad", manifest=MANIFEST.replace('commands = ["notes.refresh"]\n', ""))
    monkeypatch.setenv("SUPERVISOR_ROOT", str(bad.root))
    failed = runner.invoke(app, ["plugin", "check", "notes"])
    assert failed.exit_code == 1 and "FAIL registrations" in failed.output


def test_a_plugin_without_a_manifest_fails_and_the_rest_is_not_run(tmp_path):
    found = results(project(tmp_path, manifest=None))
    assert not found["manifest"].ok and "supervisor-plugin.toml" in found["manifest"].detail
    assert not found["registrations"].ok and "not run" in found["registrations"].detail


def test_an_incompatible_range_fails(tmp_path):
    found = results(project(tmp_path, manifest=MANIFEST.replace('">=1.11"', '">=99"')))
    assert not found["framework"].ok and ">=99" in found["framework"].detail


def test_an_internal_import_fails_with_its_file_and_line(tmp_path):
    source = "from supervisor import inbox  # noqa: F401\n" + GOOD
    found = results(project(tmp_path, source=source))
    assert not found["imports"].ok and "notes.py:1" in found["imports"].detail and "supervisor.inbox" in found[
        "imports"].detail


@pytest.mark.filterwarnings("ignore:supervisor.*framework internal:DeprecationWarning")
def test_a_registration_at_import_fails(tmp_path):
    source = ("from supervisor.adapters.python import JOBS\n"
              "JOBS['notes_at_import'] = lambda root, run_dir, **p: {}\n") + GOOD
    found = results(project(tmp_path, source=source))
    assert not found["import_side_effects"].ok and "notes_at_import" in found["import_side_effects"].detail


@pytest.mark.parametrize("manifest,expected", [
    (MANIFEST.replace('commands = ["notes.refresh"]\n', ""), "not declared: commands 'notes.refresh'"),
    (MANIFEST.replace('python_jobs = ["notes_sync"]', 'python_jobs = ["notes_sync", "notes_other"]'),
     "declared but not registered: python_jobs 'notes_other'"),
], ids=["undeclared", "declared-not-registered"])
def test_registrations_must_match_provides_exactly(tmp_path, manifest, expected):
    found = results(project(tmp_path, manifest=manifest))
    assert not found["registrations"].ok and expected in found["registrations"].detail


def test_a_setup_that_registers_differently_the_second_time_is_not_idempotent(tmp_path):
    source = GOOD.replace("def setup(registry):\n", "CALLS = []\n\ndef setup(registry):\n    CALLS.append(1)\n"
                          "    if len(CALLS) > 1:\n        return\n")
    found = results(project(tmp_path, source=source))
    assert found["registrations"].ok and not found["idempotent"].ok


def test_configuration_that_does_not_validate_fails(tmp_path):
    found = results(project(tmp_path, routing={"plugins": {"notes": {"base_url": 7}}}))
    assert not found["config"].ok and "base_url" in found["config"].detail


def test_the_check_never_runs_the_plugins_own_tests(tmp_path):
    marker = tmp_path / "tests_ran.txt"
    pkg_test = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\ndef test_it():\n    pass\n"
    layout = testing.project(tmp_path, routing=ROUTING,
                             files={"plugins/notes/__init__.py": GOOD,
                                    "plugins/notes/supervisor-plugin.toml": MANIFEST,
                                    "plugins/notes/tests/test_notes.py": pkg_test},
                             plugins=["notes"])
    found = results(layout)
    assert all(f.ok for f in found.values()), {n: f.detail for n, f in found.items()}
    assert not marker.exists() and "notes" not in REGISTRY
